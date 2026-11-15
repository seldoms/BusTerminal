import os
import time
from typing import Any, Callable, Dict, List, Optional

import magnet_availability
import media_policy


# 阈值只有一处定义（media_policy），这里保留旧名字给既有调用/测试用
MIN_JAV_VIDEO_FILE_SIZE_BYTES = media_policy.MIN_PLAYABLE_VIDEO_BYTES
JAV_FILE_FILTER_TAG = "smart_filter_400mb"
SEEDING_LIMIT_TAG = "seeding_limit_set"
DOWNLOAD_RECORDED_TAG = "download_recorded"
VIDEO_FILE_EXTS = media_policy.VIDEO_FILE_EXTS

# 队列对账宽限期：提交后不足 24h 的种子可能还在排队/刚加种，
# 不能因为 qB 暂时查不到就判成孤儿。
QUEUE_RECONCILE_GRACE_SECONDS = 24 * 3600
# 明确拿不到元数据的状态。stalledDL 不算 —— 它可能只是暂时没有速度，
# 而且 qB 与 MoviePilot 共用，不能据此批量取消别人的种子。
DEAD_TORRENT_STATES = {"metaDL", "error"}


class DownloadService:
    def __init__(
        self,
        *,
        db: Any,
        qb_api: Any,
        config: Any,
        task_registry: Any,
        logger: Any,
        stop_event: Any,
        normalize_magnet_url: Callable[[Any], Optional[str]],
        get_bool_config: Callable[[str, bool], bool],
        get_automation_mode: Callable[[], str],
        get_auto_download_daily_limit: Callable[[], int],
        get_today_start_ts: Callable[[], float],
        get_qb_session: Callable[[], Any],
        get_disk_threshold_bytes: Callable[[], float],
        safe_exception_label: Callable[[Exception], str],
        display_value_for_log: Callable[[Any], str],
        display_path_for_log: Callable[[Any], str],
        safe_number: Callable[..., float],
        parse_int_value: Callable[..., int],
        extract_movie_id_from_text: Callable[[str], Optional[str]],
        get_qb_delivery_folder: Callable[[], Optional[str]],
        on_download_completed: Optional[Callable[[str, Dict], None]] = None,
        check_library_presence: Optional[Callable[[str], Dict[str, Any]]] = None,
        is_blocked: Optional[Callable[..., Dict[str, Any]]] = None,
        invalidate_qb_session: Optional[Callable[[], None]] = None,
        delivery_path_probe: Optional[Callable[[str], bool]] = None,
        # 静默窗口查询（qb_quiet_hours.is_active_now）。None = 不启用，保持老行为。
        is_quiet_now: Optional[Callable[[], bool]] = None,
    ):
        self.db = db
        self.qb_api = qb_api
        self.config = config
        self.task_registry = task_registry
        self.logger = logger
        self.stop_event = stop_event
        self.normalize_magnet_url = normalize_magnet_url
        self.get_bool_config = get_bool_config
        self.get_automation_mode = get_automation_mode
        self.get_auto_download_daily_limit = get_auto_download_daily_limit
        self.get_today_start_ts = get_today_start_ts
        self.get_qb_session = get_qb_session
        self.get_disk_threshold_bytes = get_disk_threshold_bytes
        self.safe_exception_label = safe_exception_label
        self.display_value_for_log = display_value_for_log
        self.display_path_for_log = display_path_for_log
        self.safe_number = safe_number
        self.parse_int_value = parse_int_value
        self.extract_movie_id_from_text = extract_movie_id_from_text
        self.get_qb_delivery_folder = get_qb_delivery_folder
        self.on_download_completed = on_download_completed
        self.check_library_presence = check_library_presence
        self.is_blocked = is_blocked
        self.invalidate_qb_session = invalidate_qb_session
        self.delivery_path_probe = delivery_path_probe
        self.is_quiet_now = is_quiet_now
        # 投递后回查落位的上限：同设备（storage 内两个目录）relocate 是 rename，
        # 秒级完成；留足余量给跨设备/大目录的极端情况。
        self.delivery_verify_timeout_seconds = 60.0
        self.delivery_verify_poll_seconds = 2.0
        # 加种暂停后等元数据的上限。磁力拿元数据通常是秒级，30s 足够；
        # 拿不到就恢复下载，绝不把种子长期挂在暂停态。
        self.post_add_metadata_timeout_seconds = 30.0
        self.post_add_metadata_poll_seconds = 2.0
        self._dispatcher_last_warn = 0.0

    def _quiet_active(self) -> bool:
        """此刻是否处于 qB 静默窗口内（周末全天策略落地处）。

        静默期三件事一起停：新下载不再入队、dispatcher 不再往 qB 加种、
        groom 不再投递/搬文件。第三件尤其关键 —— groom 把「已完成 + 已暂停」
        视作做种时限达成，若不拦，静默一暂停种子就会触发一串 relocate，
        大文件搬运几分钟，反而比不静默更吵。
        """
        # getattr 兜底：有些老测试用 __new__ 手工拼 service，没有这个属性
        probe = getattr(self, "is_quiet_now", None)
        if not probe:
            return False
        try:
            return bool(probe())
        except Exception as exc:
            # 闸门自身出错时放行，与屏蔽词闸门同一口径
            label = getattr(self, "safe_exception_label", None) or (lambda e: type(e).__name__)
            logger = getattr(self, "logger", None)
            if logger is not None:
                logger.warning("Quiet hours gate failed: %s", label(exc))
            return False

    def _notify_download_completed(self, movie_id: Optional[str], torrent: Dict) -> bool:
        """下载完成钩子：交给 amane 侧入库（方案 C 下 BusTerminal 不碰文件）。

        返回 True 表示入库闭环确实被触发（调用方据此决定是否打 Delivered 标签）。
        """
        if not movie_id or not self.on_download_completed:
            return False
        try:
            return bool(self.on_download_completed(movie_id, torrent))
        except Exception as exc:
            self.logger.warning(
                "Download completion hook failed for %s: %s",
                self.display_value_for_log(movie_id),
                self.safe_exception_label(exc),
            )
            return False

    # ---------- 投递落位确认 / 会话自愈 ----------

    @staticmethod
    def _is_session_expired(exc: Exception) -> bool:
        """判断异常是不是 qB 会话失效。

        按类名判断而不是 isinstance：qb_api 是注入进来的模块，测试里是桩，
        引真类做 isinstance 既耦合又会在桩上失效。
        """
        return type(exc).__name__ == "QBSessionExpired"

    def fetch_torrents(self, tag: Optional[str] = None):
        """读种子列表，会话失效时重建一次再试。

        get_torrents 在 401/403 上抛 QBSessionExpired（不再返回空列表），这里是
        唯一能把「qB 重启导致旧 SID 全废」自动修好的地方。以前这条路径静默拿到
        空列表，groom 以为没有种子，不打日志不报错地空转了一整天（2026-10-27）。
        """
        session, base_url = self.get_qb_session()
        if not session:
            return None, None, []
        try:
            return session, base_url, self.qb_api.get_torrents(session, base_url, tag=tag) or []
        except Exception as exc:
            if not self._is_session_expired(exc):
                raise
            self.logger.warning("QB 会话失效，重建后重试一轮: %s", self.safe_exception_label(exc))

        if self.invalidate_qb_session:
            try:
                self.invalidate_qb_session()
            except Exception as exc:
                self.logger.warning("重建 QB 会话失败: %s", self.safe_exception_label(exc))
        session, base_url = self.get_qb_session()
        if not session:
            return None, None, []
        return session, base_url, self.qb_api.get_torrents(session, base_url, tag=tag) or []

    @staticmethod
    def _path_under(path: Any, folder: str) -> bool:
        value = str(path or "").rstrip("/")
        return bool(value) and (value == folder or value.startswith(folder + "/"))

    # qB 在 relocate 期间会占用的状态。这段时间 save_path 已经指向目标目录，
    # 但文件还在旧位置 —— 光看路径会误判成「已落位」。
    RELOCATE_PENDING_STATES = ("moving", "checkingup", "checkingdl", "checkingresume")

    def delivered_really_landed(self, torrent: Dict[str, Any], delivery_folder: str) -> bool:
        """判断打了 Delivered 的种子，文件是不是**真的**在投递目录下。

        两道判据缺一不可：

        1. **路径**：qB 自己回报的 save_path / content_path 要指向投递目录。
        2. **状态**：不能还处于 moving/checking。

        ⚠️ 第 2 条是踩出来的：qB 在 relocate **一开始**就把 save_path 改成目标路径，
        此时文件一个字节都还没过去（实测 JUR-836：save_path 已是 `/hp/_incoming`，
        而 `/hp/_incoming/JUR-836_4K60FPS` 根本不存在、源目录里那个 23.9 GB 文件
        还在写）。只看路径的话，会把「正在搬」误判成「已经搬完」。

        ⚠️ delivery_path_probe 默认不开：bus_terminal 容器看不到 qB 的下载目录
        （`/hp`、`/downloads/88888` 在它里面都不存在），硬做实检会把所有种子判成
        「原位置已空」从而全部跳过。只有给 bus 容器挂上同一份存储后才可启用。
        """
        folder = str(delivery_folder or "").rstrip("/")
        if not folder:
            # 没配投递目录就无从判断，保持旧行为，不误摘标签
            return True
        if str(torrent.get("state") or "").lower() in self.RELOCATE_PENDING_STATES:
            return False
        if self._path_under(torrent.get("save_path"), folder) or self._path_under(
            torrent.get("content_path"), folder
        ):
            return True
        if self.delivery_path_probe:
            # qB 说还在原目录。若原路径已空，说明文件是被外部搬走的（手工救火等），
            # 不是 relocate 假成功，此时不该摘标签重投。
            actual = str(torrent.get("content_path") or "")
            if actual:
                try:
                    if not self.delivery_path_probe(actual):
                        return True
                except Exception:
                    return False
        return False

    def _reread_torrent(self, session, base_url, torrent_hash: str) -> Optional[Dict[str, Any]]:
        """按 hash 重新读单个种子，拿它的真实 save_path / state。"""
        try:
            items = self.qb_api.get_torrents_by_hashes(session, base_url, [torrent_hash]) or []
        except Exception as exc:
            self.logger.warning(
                "回查种子失败: %s (%s)",
                self.display_value_for_log(torrent_hash),
                self.safe_exception_label(exc),
            )
            return None
        for item in items:
            if isinstance(item, dict):
                return item
        return None

    def _wait_unless_stopped(self, seconds: float) -> bool:
        """短暂等待；收到停止信号返回 False。

        不用 stop_event.wait() 做轮询间隔：groom 主循环拿它当整轮间隔，中止语义
        是「这一轮结束、整体退出」，而这里只是秒级的轮询间隔，混用会让两者互相干扰。
        """
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self.stop_event is not None and self.stop_event.is_set():
                return False
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        return self.stop_event is None or not self.stop_event.is_set()

    def deliver_torrent(self, session, base_url, torrent: Dict[str, Any], target_delivery: str) -> bool:
        """把种子文件搬到投递目录，并确认**真的搬到了**才返回 True。

        qB 的 relocate 是异步任务，setLocation 的 200 只代表「请求已接受」。这里投递
        后轮询回查，直到 save_path/content_path 落在目标目录下；超时或 relocate 已结束
        仍未落位一律判失败，让调用方下个周期重试 —— 绝不能带着「已投递」标签上墙。
        """
        torrent_hash = torrent.get("hash")
        if not torrent_hash:
            return False
        if not self.qb_api.set_torrent_location(session, base_url, [torrent_hash], target_delivery):
            return False

        # 先给 qB 一个排队/rename 的窗口，避免第一次查询还没开始搬就判失败。
        # 同设备（storage 内两个目录）是 rename，秒级完成。
        if not self._wait_unless_stopped(self.delivery_verify_poll_seconds):
            return False

        deadline = time.monotonic() + self.delivery_verify_timeout_seconds
        while time.monotonic() < deadline:
            fresh = self._reread_torrent(session, base_url, torrent_hash)
            if fresh is None:
                return False
            if self.delivered_really_landed(fresh, target_delivery):
                return True
            if str(fresh.get("state") or "").lower() != "moving":
                # 已不在 moving 却没落位 = relocate 结束且失败，再等也没用
                self.logger.warning(
                    "qB relocate 结束但文件未落位: %s (state=%s, save_path=%s)",
                    self.display_value_for_log(fresh.get("name")),
                    fresh.get("state"),
                    self.display_path_for_log(fresh.get("save_path")),
                )
                return False
            if not self._wait_unless_stopped(self.delivery_verify_poll_seconds):
                return False

        self.logger.warning(
            "投递确认超时（%.0fs）: %s",
            self.delivery_verify_timeout_seconds,
            self.display_value_for_log(torrent.get("name")),
        )
        return False

    # ---------- 加种即选片（一个字节垃圾都不下） ----------

    @staticmethod
    def magnet_infohash(magnet: Any) -> Optional[str]:
        """从磁力链接抠出 btih。拿不到就返回 None（调用方退回周期过滤）。"""
        import re

        match = re.search(r"urn:btih:([A-Za-z0-9]{32,40})", str(magnet or ""))
        return match.group(1) if match else None

    def apply_post_add_file_policy(self, session, base_url, torrent_hash: str) -> Dict[str, Any]:
        """暂停状态下等元数据 → 只留最大的那个文件 → 恢复下载。

        这是「小文件根本不下」的唯一正确时机：``filter_jav_torrent_files`` 只改
        file priority，改之前已经下掉的字节不会退回去。加种时先暂停、拿到文件列表
        立刻把所有非最大文件置 0、正片置 1，再恢复，残渣从源头上就不存在。

        元数据迟迟不到（磁力还没连上 peer）时**必须恢复**，否则种子永远卡在暂停态；
        恢复后交给既有的周期过滤器兜底，最多损失几秒垃圾流量。
        """
        if not torrent_hash:
            return {"filtered": False, "reason": "missing_hash"}

        deadline = time.monotonic() + self.post_add_metadata_timeout_seconds
        files: List[Dict[str, Any]] = []
        read_failed = False
        while time.monotonic() < deadline:
            try:
                files = self.qb_api.get_torrent_files(session, base_url, torrent_hash) or []
            except Exception as exc:
                self.logger.warning(
                    "等元数据时读文件列表失败: %s (%s)",
                    self.display_value_for_log(torrent_hash),
                    self.safe_exception_label(exc),
                )
                # 读文件列表本身报错就别再干等 30 秒了，直接恢复下载
                read_failed = True
                break
            if files:
                break
            if not self._wait_unless_stopped(self.post_add_metadata_poll_seconds):
                break

        result: Dict[str, Any] = {"filtered": False}
        if files:
            try:
                result = self.filter_jav_torrent_files(
                    session, base_url, {"hash": torrent_hash}
                ) or {"filtered": False}
            except Exception as exc:
                self.logger.warning(
                    "加种选片失败: %s (%s)",
                    self.display_value_for_log(torrent_hash),
                    self.safe_exception_label(exc),
                )
                result = {"filtered": False, "reason": "filter_error"}
        else:
            result = {
                "filtered": False,
                "reason": "metadata_read_failed" if read_failed else "metadata_timeout",
            }
            self.logger.warning(
                "加种后没拿到文件列表（%s），先恢复下载交给周期过滤: %s",
                result["reason"],
                self.display_value_for_log(torrent_hash),
            )

        # 无论选片成功与否都要恢复，否则种子永远不动
        try:
            self.qb_api.resume_torrents(session, base_url, [torrent_hash])
        except Exception as exc:
            self.logger.warning(
                "恢复下载失败: %s (%s)",
                self.display_value_for_log(torrent_hash),
                self.safe_exception_label(exc),
            )
        return result

    def _check_library_presence(self, movie_id: str) -> Dict[str, Any]:
        """问 amane/Jellyfin 这部片是不是已经在库里。查不动就当没查过，不拦下载。"""
        if not self.check_library_presence:
            return {}
        try:
            return self.check_library_presence(movie_id) or {}
        except Exception as exc:
            self.logger.warning(
                "Library presence check failed for %s: %s",
                self.display_value_for_log(movie_id),
                self.safe_exception_label(exc),
            )
            return {}

    def _check_block_keyword(
        self,
        movie_id: Optional[str],
        title: str,
        source_url: Optional[str],
    ) -> Dict[str, Any]:
        """查屏蔽词。没配 is_blocked 或没命中都返回不拦。"""
        if not self.is_blocked:
            return {"blocked": False, "keyword": None}
        try:
            return self.is_blocked(movie_id, title, source_url) or {"blocked": False, "keyword": None}
        except Exception as exc:
            # 闸门自身出错时放行：宁可少挡一次，也不要因为配置问题让下载全线瘫掉
            self.logger.warning("Block keyword check failed: %s", self.safe_exception_label(exc))
            return {"blocked": False, "keyword": None}

    def evaluate_download_permission(
        self,
        movie_id: Optional[str],
        magnet: Optional[str],
        title: str,
        source_url: Optional[str],
        *,
        automated: bool = True,
    ) -> Dict[str, Any]:
        denied = []
        warnings = []
        presence: Dict[str, Any] = {}

        # qB 静默窗口（如「周末全天」）：静默期一个 qB 任务都不开。
        # 放在所有闸门最前面，命中后下面的 amane / qB / 磁盘检查全都不会跑。
        if self._quiet_active():
            denied.append("qb_quiet_hours")

        # 全链路屏蔽词闸门：放在最前面，纯内存判断，命中就直接拒，
        # 不去打 amane / qB / 库。手动下载、订阅、发现流、自动化都走这一个闸门。
        blocked = self._check_block_keyword(movie_id, title, source_url)
        if blocked.get("blocked"):
            denied.append("blocked_keyword")

        normalized_magnet = self.normalize_magnet_url(magnet)
        if not normalized_magnet:
            denied.append("invalid_magnet_url")

        if automated:
            if self.get_automation_mode() != "auto_approve_low_risk":
                denied.append("automation_mode_not_auto_approve")
            elif not self.get_bool_config("auto_download_enabled", False):
                denied.append("auto_download_disabled")

        if movie_id:
            if self.db.is_movie_downloaded(movie_id):
                denied.append("already_downloaded")
            if self.db.get_local_file(movie_id):
                denied.append("local_file_exists")
            if self.db.is_movie_pending(movie_id):
                denied.append("active_queue_exists")
        if normalized_magnet and self.db.get_active_queue_entry(movie_id=movie_id, magnet=normalized_magnet):
            denied.append("duplicate_queue_or_magnet")

        # 前面这些是库内判断，零成本；只有全过了才去问 amane/Jellyfin，避免白打接口
        if movie_id and not denied:
            presence = self._check_library_presence(movie_id)
            if presence.get("present"):
                denied.append("already_in_library")

        if automated:
            daily_limit = self.get_auto_download_daily_limit()
            queued_today = self.db.count_download_queue_since(self.get_today_start_ts(), automated_only=True)
            if daily_limit >= 0 and queued_today >= daily_limit:
                denied.append("daily_limit_reached")

        if not denied:
            session, base_url = self.get_qb_session()
            if not session:
                warnings.append("qb_unavailable")
            else:
                try:
                    state = self.qb_api.get_server_state(session, base_url)
                    free_space = state.get("free_space_on_disk", 0)
                    threshold_bytes = self.get_disk_threshold_bytes()
                    if free_space and free_space < threshold_bytes:
                        denied.append("disk_below_threshold")
                except Exception as e:
                    warnings.append("qb_state_unavailable")
                    warnings.append(self.safe_exception_label(e))

        allowed = not denied
        payload = {
            "movie_id": movie_id,
            "title": title,
            "source_url": source_url,
            "automated": automated,
            "denied": denied,
            "warnings": warnings,
            "library_presence": presence,
        }
        if blocked.get("blocked"):
            payload["blocked_keyword"] = blocked.get("keyword")
        # 静默期被挡是策略预期、不是异常，不往生命周期事件表里灌「拒绝下载」
        if movie_id and not allowed and "qb_quiet_hours" not in denied:
            self.db.record_lifecycle_event(
                movie_id,
                "download_permission_denied",
                title=title,
                download_status="blocked",
                source=source_url,
                reason=", ".join(denied),
                decision_status="blocked",
                execution_result="blocked",
                payload=payload,
                # 订阅/发现循环每轮都会重新评估同一批片子，被每日限额挡掉的
                # 结果一天内不会变；同片同原因同 UTC 日只记一条，避免事件表
                # 被重复评估刷爆（生产实测 7 天 22,370 条 / 仅 1,468 部片子）。
                dedupe_repeat_within_day=True,
            )
        return {
            "allowed": allowed,
            "reasons": denied,
            "warnings": warnings,
            "blocked_keyword": blocked.get("keyword") if blocked.get("blocked") else None,
        }

    def queue_download_if_absent(
        self,
        magnet: str,
        movie_id: str,
        title: str,
        source_url: Optional[str],
        save_path: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        existing = self.db.get_active_queue_entry(movie_id=movie_id, magnet=magnet)
        if existing:
            return {
                "queued": False,
                "duplicate": True,
                "task_id": existing.get("id"),
                "status": existing.get("status"),
            }

        task_id = self.db.add_to_queue(
            magnet=magnet,
            movie_id=movie_id,
            title=title,
            source_url=source_url,
            save_path=save_path,
            category=category,
        )
        return {
            "queued": True,
            "duplicate": False,
            "task_id": task_id,
            "status": "pending",
        }

    def normalize_qb_tags(self, raw_tags: Any) -> List[str]:
        if raw_tags is None:
            return []
        if isinstance(raw_tags, (list, tuple, set)):
            items = raw_tags
        else:
            items = str(raw_tags).split(",")
        tags = []
        for item in items:
            tag = str(item or "").strip().lower()
            if tag and tag not in tags:
                tags.append(tag)
        return tags

    def get_qb_file_index(self, file_entry: Dict[str, Any], fallback_index: int) -> int:
        try:
            return int(file_entry.get("index", fallback_index))
        except (TypeError, ValueError):
            return fallback_index

    def is_groomable_torrent(self, torrent: Dict[str, Any]) -> bool:
        if not isinstance(torrent, dict):
            return False
        state = str(torrent.get("state") or "")
        if state in {"metaDL", "allocating", "error"}:
            return False
        if self.safe_number(torrent.get("progress")) >= 1:
            return False
        return self.safe_number(torrent.get("total_size") or torrent.get("size")) > 0

    def is_completed_torrent(self, torrent: Dict[str, Any]) -> bool:
        """已完成（progress=100）的种子。

        这一档原先被 is_groomable_torrent 排除，于是**小种子几秒下完就再没人看**
        —— 400MB 过滤永远跑不到它，种子自带的广告 mp4 就这么落到盘上了。
        补扫这一档只为「判定整种是不是纯垃圾」，不参与投递。
        """
        if not isinstance(torrent, dict):
            return False
        return (
            self.safe_number(torrent.get("progress")) >= 1
            and self.safe_number(torrent.get("total_size") or torrent.get("size")) > 0
        )

    def should_delete_junk_torrents(self) -> bool:
        """整种没有任何 ≥400MB 正片时，删种+删文件（默认开）。

        原来的行为只是把文件优先级全设 0：种子留在列表里、目录也留着，
        下次人工还得清一遍。策略上这就是纯垃圾，直接删干净。
        """
        fallback = bool(getattr(self.config, "JUNK_DELETE_TORRENTS", True))
        return bool(self.get_bool_config("junk_delete_torrents", fallback))

    def delete_junk_torrent(self, session: Any, base_url: str, torrent: Dict[str, Any]) -> bool:
        torrent_hash = str(torrent.get("hash") or "").strip()
        if not torrent_hash:
            return False
        if not self.should_delete_junk_torrents():
            return False
        try:
            ok = bool(self.qb_api.delete_torrents(session, base_url, [torrent_hash], delete_files=True))
        except Exception as exc:
            self.logger.warning(
                "Junk torrent delete failed: %s (%s)",
                self.display_value_for_log(torrent.get("name")),
                self.safe_exception_label(exc),
            )
            return False
        if ok:
            self.logger.warning(
                "Junk torrent deleted (no file >= %sMB): %s (hash=%s)",
                MIN_JAV_VIDEO_FILE_SIZE_BYTES // (1024 * 1024),
                self.display_value_for_log(torrent.get("name")),
                self.display_value_for_log(torrent_hash),
            )
        return ok

    def filter_jav_torrent_files(self, session: Any, base_url: str, torrent: Dict[str, Any]) -> Dict[str, Any]:
        torrent_hash = torrent.get("hash")
        if not torrent_hash:
            return {"filtered": False, "reason": "missing_hash"}

        files = self.qb_api.get_torrent_files(session, base_url, torrent_hash)
        if not files:
            return {"filtered": False, "reason": "no_files"}

        from lifecycle_manager import AD_EXTS, AD_KEYWORDS

        candidates = []
        for idx, file_entry in enumerate(files):
            if not isinstance(file_entry, dict):
                continue
            name = str(file_entry.get("name") or "")
            lowered_name = name.lower()
            ext = os.path.splitext(lowered_name)[1]
            size = int(max(self.safe_number(file_entry.get("size")), 0))
            is_ad = (ext in AD_EXTS) or any(keyword in lowered_name for keyword in AD_KEYWORDS)

            if ext in VIDEO_FILE_EXTS and not is_ad and size >= MIN_JAV_VIDEO_FILE_SIZE_BYTES:
                candidates.append({
                    "index": self.get_qb_file_index(file_entry, idx),
                    "name": name,
                    "size": size,
                })

        if not candidates:
            disabled_file_ids = [
                self.get_qb_file_index(file_entry, idx)
                for idx, file_entry in enumerate(files)
                if isinstance(file_entry, dict)
            ]
            small_files_disabled = sum(
                1
                for file_entry in files
                if isinstance(file_entry, dict)
                and int(max(self.safe_number(file_entry.get("size")), 0)) < MIN_JAV_VIDEO_FILE_SIZE_BYTES
            )
            if disabled_file_ids:
                if not self.qb_api.set_file_priority(session, base_url, torrent_hash, disabled_file_ids, 0):
                    return {"filtered": False, "reason": "file_priority_failed"}
                if not self.qb_api.add_tags(session, base_url, [torrent_hash], JAV_FILE_FILTER_TAG):
                    return {"filtered": False, "reason": "tag_update_failed"}
            return {
                "filtered": False,
                "reason": "no_video_over_400mb",
                "disabled_files": len(disabled_file_ids),
                "small_files_disabled": small_files_disabled,
            }

        keep_file = max(candidates, key=lambda item: item["size"])
        all_file_ids = [
            self.get_qb_file_index(file_entry, idx)
            for idx, file_entry in enumerate(files)
            if isinstance(file_entry, dict)
        ]
        disabled_file_ids = [file_id for file_id in all_file_ids if file_id != keep_file["index"]]

        if disabled_file_ids and not self.qb_api.set_file_priority(
            session, base_url, torrent_hash, disabled_file_ids, 0
        ):
            return {"filtered": False, "reason": "file_priority_failed"}
        if not self.qb_api.set_file_priority(session, base_url, torrent_hash, [keep_file["index"]], 1):
            return {"filtered": False, "reason": "file_priority_failed"}
        if not self.qb_api.add_tags(session, base_url, [torrent_hash], JAV_FILE_FILTER_TAG):
            return {"filtered": False, "reason": "tag_update_failed"}

        return {
            "filtered": True,
            "kept_file": keep_file["name"],
            "kept_size": keep_file["size"],
            "disabled_files": len(disabled_file_ids),
        }

    def run_jav_file_filter_once(
        self,
        filter_torrent_files: Optional[Callable[[Any, str, Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        session, base_url = self.get_qb_session()
        if not session:
            return {"success": False, "message": "QB is not configured or unavailable"}

        torrents = self.qb_api.get_torrents(session, base_url, tag="jav")
        scanned = 0
        filtered = 0
        skipped = 0
        small_files_disabled = 0
        deleted_junk = 0
        filter_torrent_files = filter_torrent_files or self.filter_jav_torrent_files

        for torrent in torrents or []:
            if not isinstance(torrent, dict):
                skipped += 1
                continue
            tags = set(self.normalize_qb_tags(torrent.get("tags")))
            # 已完成的也要补扫：小种子几秒下完，is_groomable_torrent 早就 False 了
            if JAV_FILE_FILTER_TAG in tags or not (
                self.is_groomable_torrent(torrent) or self.is_completed_torrent(torrent)
            ):
                skipped += 1
                continue

            scanned += 1
            result = filter_torrent_files(session, base_url, torrent)
            if result.get("filtered"):
                filtered += 1
                continue
            small_files_disabled += int(result.get("small_files_disabled") or 0)
            if result.get("reason") == "no_video_over_400mb" and self.delete_junk_torrent(
                session, base_url, torrent
            ):
                deleted_junk += 1

        return {
            "success": True,
            "scanned": scanned,
            "filtered": filtered,
            "skipped": skipped,
            "small_files_disabled": small_files_disabled,
            "deleted_junk": deleted_junk,
            "min_file_size_mb": MIN_JAV_VIDEO_FILE_SIZE_BYTES // (1024 * 1024),
        }

    def delete_junk_torrents_once(
        self,
        filter_torrent_files: Optional[Callable[[Any, str, Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """补扫「已经下完、但 400MB 过滤没跑过」的种子，纯垃圾就删种+删文件。

        groom 循环只处理未完成的种子（is_groomable_torrent），小种子几秒下完，
        过滤根本轮不到它——这就是广告 mp4 落盘的真正入口。这里专门补这一档：
        只判定、不投递，发现整种没有任何 ≥ 阈值的正片就直接删掉。
        """
        session, base_url = self.get_qb_session()
        if not session:
            return {"success": False, "message": "QB is not configured or unavailable"}

        filter_torrent_files = filter_torrent_files or self.filter_jav_torrent_files
        scanned = 0
        deleted = 0
        errors = 0
        for torrent in self.qb_api.get_torrents(session, base_url, tag="jav") or []:
            if not isinstance(torrent, dict):
                continue
            tags = set(self.normalize_qb_tags(torrent.get("tags")))
            if JAV_FILE_FILTER_TAG in tags or not self.is_completed_torrent(torrent):
                continue
            scanned += 1
            try:
                result = filter_torrent_files(session, base_url, torrent)
            except Exception as exc:
                errors += 1
                self.logger.warning(
                    "Junk torrent scan failed: %s (%s)",
                    self.display_value_for_log(torrent.get("name")),
                    self.safe_exception_label(exc),
                )
                continue
            if result.get("reason") != "no_video_over_400mb":
                continue
            if self.delete_junk_torrent(session, base_url, torrent):
                deleted += 1

        return {
            "success": True,
            "scanned": scanned,
            "deleted": deleted,
            "errors": errors,
            "min_file_size_mb": MIN_JAV_VIDEO_FILE_SIZE_BYTES // (1024 * 1024),
        }

    def groom_torrents_task(
        self,
        filter_torrent_files: Optional[Callable[[Any, str, Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> None:
        filter_torrent_files = filter_torrent_files or self.filter_jav_torrent_files
        while not self.stop_event.is_set():
            # 静默期整轮跳过：暂停种子会被误判成「做种时限达成」而触发 relocate，
            # 大文件搬运是周末最大的噪音源。仍然 mark_success，别让健康看板误报卡死。
            if self._quiet_active():
                self.task_registry.mark_success(
                    "groom_torrents_task",
                    summary={"quiet_hours": True, "torrent_count": 0},
                )
                if self.stop_event.wait(self.config.GROOM_TASK_INTERVAL):
                    break
                continue
            torrent_count = 0
            delivered_count = 0
            downloaded_count = 0
            filtered_count = 0
            seeding_wait_count = 0
            try:
                # fetch_torrents 内含会话自愈：403 会重建 SID 再试一次，而不是
                # 拿一个空列表回去让 groom 以为「qB 里没种子」然后静默空转。
                session, base_url, torrents = self.fetch_torrents(tag="jav")
                if not session:
                    if self.stop_event.wait(self.config.GROOM_TASK_INTERVAL):
                        break
                    continue

                # 只处理 BusTerminal 自己发起的种子（add_magnet 打的 "jav" tag）。
                # 这个 qB 与 MoviePilot 共用（辅种 / 刷流-自动 等共 600+ 个种子），
                # 投递目录现在落在 amane 库内，误搬别人的种子会堆成待人工垃圾。
                torrents = torrents or []
                torrent_count = len(torrents)
                # 一次读齐，投递判定和落位校验都用同一个值
                delivery_folder = self.get_qb_delivery_folder()
                # qB 的 relocate 是串行队列，且大文件真会搬上几分钟（实测 13.9 GB
                # 约 3 分钟）。整批发请求只会让队列越堆越长，所以每轮最多投递一个，
                # 等它真的搬完再放下一个。
                #
                # ⚠️ 不用「有种子在 moving 就整体跳过」做闸门：qB 出现过 state 永久
                # 停在 moving 的僵死种子，那样会把后面所有种子永久堵死（实测卡住
                # 28 个 / 139 GB）。改成每轮发一笔，进度只取决于单颗种子。
                issued_this_round = False

                for torrent in torrents:
                    if self.stop_event.is_set():
                        break
                    if not isinstance(torrent, dict):
                        continue
                    tags = self.normalize_qb_tags(torrent.get("tags"))

                    if "jav" not in tags:
                        continue

                    torrent_hash = torrent.get("hash")
                    if not torrent_hash:
                        continue
                    seeding_limit = self.parse_int_value(
                        self.db.get_config("seeding_time_limit_min"),
                        self.config.SEEDING_TIME_LIMIT,
                        minimum=0,
                    )
                    # qB 在种子被暂停后不再累计 seeding_time（实测：6 个已完成的 pausedUP 种子
                    # 该值全是 0），会把「已完成、做种也已停」的种子永远卡在等待时限这一步。
                    # 已完成 + 已暂停 = 做种已经结束，直接视作时限达成。
                    state_now = str(torrent.get("state") or "").lower()
                    current_seeding_s = self.safe_number(torrent.get("seeding_time"), 0)
                    seeding_ended_by_pause = (
                        "paused" in state_now
                        and self.safe_number(torrent.get("progress"), 0) >= 1
                    )
                    seeding_limit_reached = (
                        seeding_limit <= 0
                        or seeding_ended_by_pause
                        or current_seeding_s / 60 >= seeding_limit
                    )

                    if SEEDING_LIMIT_TAG not in tags:
                        self.logger.info(
                            "Setting QB seeding time limit (%s minutes): %s",
                            seeding_limit,
                            self.display_value_for_log(torrent.get("name")),
                        )
                        self.qb_api.set_share_limits(
                            session,
                            base_url,
                            [torrent_hash],
                            ratio_limit=-2,
                            seeding_time_limit=seeding_limit,
                        )
                        self.qb_api.add_tags(session, base_url, [torrent_hash], SEEDING_LIMIT_TAG)

                    if JAV_FILE_FILTER_TAG not in tags and self.is_groomable_torrent(torrent):
                        self.logger.info(
                            "Applying JAV %sMB file filter: %s (hash=%s)",
                            MIN_JAV_VIDEO_FILE_SIZE_BYTES // (1024 * 1024),
                            self.display_value_for_log(torrent.get("name")),
                            self.display_value_for_log(torrent.get("hash")),
                        )
                        filter_result = filter_torrent_files(session, base_url, torrent)
                        if filter_result.get("filtered"):
                            self.logger.info(
                                "JAV 400MB filter completed: %s, keep=%s, disabled=%s",
                                self.display_value_for_log(torrent.get("name")),
                                self.display_value_for_log(filter_result.get("kept_file")),
                                filter_result.get("disabled_files"),
                            )
                            filtered_count += 1
                            tags.append(JAV_FILE_FILTER_TAG)
                        elif filter_result.get("reason") == "no_video_over_400mb":
                            tags.append(JAV_FILE_FILTER_TAG)
                        # 其它失败（file_priority_failed / tag_update_failed）不打卡，
                        # 下一轮重试；打了卡就永远不会再补，垃圾会一直躺在盘上

                    if self.safe_number(torrent.get("progress"), 0) < 1:
                        continue

                    # pausedup 必须在内：qB 达到做种限制后会把种子转成 pausedUP，
                    # 生产上已完成的 6 个 jav 种子全是这个状态，漏掉就一个都投递不出去。
                    # pauseddl 不能放进来 —— 那是「没下完且暂停下载」。
                    #
                    # ⚠️ 这一步必须排在 Delivered 判定之前：moving / checking 的种子
                    # 正处在 relocate 中途（save_path 已指向目标、文件还在旧位置），
                    # 若先跑 Delivered 判定就会把标签摘掉、又被这里拦下不投递，
                    # 标签白白丢失。
                    state = state_now
                    if state not in [
                        "completed", "uploading", "stalledup", "forcedup", "seeding", "pausedup",
                    ]:
                        continue

                    # Delivered 是「已投递」的墓碑标签：groom 见到就永久跳过，不再重试。
                    # 但它只在 setLocation 返回 200 时打上，而 qB 的 relocate 是异步的 ——
                    # 生产上 40 个种子拿到 200、文件仍留在 /downloads/88888、标签却已打上，
                    # 231 GB 卡了两天没人再碰。所以不能无条件跳过：先确认文件真的在投递
                    # 目录下，否则摘掉标签让这一轮重新走投递流程。
                    if "delivered" in tags:
                        if self.delivered_really_landed(torrent, delivery_folder):
                            continue
                        if issued_this_round:
                            # 本轮已经发过一笔 relocate，且这颗仍未落位。摘标签也
                            # 投不出去，只会把标签状态搅乱、把日志刷满，下轮再说。
                            continue
                        self.logger.warning(
                            "Delivered 标签但文件未落位，摘标签重投: %s (save_path=%s)",
                            self.display_value_for_log(torrent.get("name")),
                            self.display_path_for_log(torrent.get("save_path")),
                        )
                        if not self.qb_api.remove_tags(
                            session, base_url, [torrent_hash], "Delivered"
                        ):
                            # 摘不掉就别往下走：继续投递会导致同一颗种子被反复搬
                            self.logger.warning(
                                "摘除 Delivered 标签失败，跳过本轮: %s",
                                self.display_value_for_log(torrent.get("name")),
                            )
                            continue
                        tags = [tag for tag in tags if tag.lower() != "delivered"]

                    movie_id = self.extract_movie_id_from_text(torrent.get("name", ""))
                    if movie_id and DOWNLOAD_RECORDED_TAG not in tags:
                        if not self.db.is_movie_downloaded(movie_id):
                            self.db.record_download(movie_id)
                            self.db.record_lifecycle_event(
                                movie_id,
                                "qb_download_completed",
                                title=torrent.get("name"),
                                download_status="downloaded",
                                source="qbittorrent",
                                execution_result="success",
                                payload={
                                    "torrent_hash": torrent.get("hash"),
                                    "torrent_name": torrent.get("name"),
                                    "state": torrent.get("state"),
                                    "progress": torrent.get("progress"),
                                },
                            )
                            downloaded_count += 1
                        # 注意：这里不通知入库。amane 的库根不含下载目录，
                        # 文件必须先投递到库内子目录才扫得到，通知放在投递成功之后。
                        self.qb_api.add_tags(session, base_url, [torrent_hash], DOWNLOAD_RECORDED_TAG)
                        tags.append(DOWNLOAD_RECORDED_TAG)
                    elif not movie_id:
                        self.logger.warning(
                            "Could not extract movie id from completed qB task name; lifecycle record skipped: %s",
                            self.display_value_for_log(torrent.get("name")),
                        )

                    if not seeding_limit_reached:
                        self.logger.info(
                            "Torrent completed; waiting for seeding time limit before delivery (%s < %s minutes): %s",
                            current_seeding_s // 60,
                            seeding_limit,
                            self.display_value_for_log(torrent.get("name")),
                        )
                        seeding_wait_count += 1
                        continue

                    self.logger.info(
                        "Torrent reached seeding time limit (%s >= %s minutes); starting delivery flow: %s",
                        current_seeding_s // 60,
                        seeding_limit,
                        self.display_value_for_log(torrent.get("name")),
                    )
                    if not movie_id:
                        # 解析不出番号的一律不投递（刷流 / 电影 / 剧集等共用同一个 qB）。
                        # 投递目录以前在下载区内部，搬错无害；现在落在 amane 库内，
                        # 搬进去会被登记、刮削失败，堆成待人工垃圾。
                        continue
                    if "paused" not in state:
                        self.qb_api.pause_torrents(session, base_url, [torrent_hash])

                    target_delivery = delivery_folder or self.get_qb_delivery_folder()
                    if not target_delivery:
                        self.logger.warning("Invalid qB delivery folder configured; torrent delivery skipped")
                        # 没有投递目录就当文件已在库内，照旧入库，否则片子永远进不了 amane。
                        self._notify_download_completed(movie_id, torrent)
                        continue
                    # deliver_torrent 会回查 qB，确认文件真的搬到目标目录才返回 True。
                    # 不能像以前那样拿 setLocation 的 200 当成功：qB 的 relocate 是异步的，
                    # 200 只代表请求被接受，假成功会把种子打上 Delivered 永久挂起。
                    if issued_this_round:
                        # 本轮已经发过一笔 relocate，别把 qB 的串行队列堆起来
                        self.logger.info(
                            "本轮已投递过一颗种子，其余等下轮: %s",
                            self.display_value_for_log(torrent.get("name")),
                        )
                        continue
                    if self.delivered_really_landed(torrent, target_delivery):
                        # 上一轮的 relocate 已经搬完，只是当时没等到确认 —— 不必再搬一次
                        delivered_ok = True
                    else:
                        issued_this_round = True
                        delivered_ok = self.deliver_torrent(
                            session, base_url, torrent, target_delivery
                        )
                    if delivered_ok:
                        self.logger.info("Moved torrent to delivery folder: %s", self.display_path_for_log(target_delivery))
                        if movie_id:
                            self.db.record_lifecycle_event(
                                movie_id,
                                "qb_delivered",
                                title=torrent.get("name"),
                                download_status="delivered",
                                source="qbittorrent",
                                execution_result="success",
                                payload={
                                    "torrent_hash": torrent.get("hash"),
                                    "torrent_name": torrent.get("name"),
                                    "delivery_folder": target_delivery,
                                },
                            )

                        # 文件已落位到 amane 库内，交给 amane 扫/刮/整理。
                        # Delivered 标签会让 groom 永久跳过这个种子，所以只有入库闭环真的
                        # 被触发才打标签：amane 没配置/钩子没跑起来时留着不打，下个周期重试，
                        # 避免「已完成但从未成功整理」的种子被静默永久挂起。
                        if self._notify_download_completed(movie_id, torrent):
                            self.qb_api.add_tags(session, base_url, [torrent_hash], "Delivered")
                            delivered_count += 1
                    else:
                        # 投递没确认成功：不打标签，下个周期重试。这里必须显式记一笔，
                        # 否则又回到「日志里什么都看不到、片子默默卡住」的老路。
                        self.logger.warning(
                            "投递未确认落位，下轮重试: %s (target=%s)",
                            self.display_value_for_log(torrent.get("name")),
                            self.display_path_for_log(target_delivery),
                        )

                self.task_registry.mark_success(
                    "groom_torrents_task",
                    summary={
                        "torrent_count": torrent_count,
                        "filtered_count": filtered_count,
                        "downloaded_count": downloaded_count,
                        "delivered_count": delivered_count,
                        "seeding_wait_count": seeding_wait_count,
                    },
                )

            except Exception as e:
                self.logger.error("Groom torrents task failed: %s", self.safe_exception_label(e))
                self.task_registry.mark_error("groom_torrents_task", e)

            self.stop_event.wait(self.config.GROOM_TASK_INTERVAL)

    def download_dispatcher_task(self) -> None:
        while not self.stop_event.is_set():
            # 静默期不再往 qB 加种（add_magnet 后 apply_post_add_file_policy 会
            # 读文件+恢复下载，与暂停执行器来回打架）。pending 原样留着，
            # 出窗口后第一轮自动继续，不需要人工补单。
            if self._quiet_active():
                self.task_registry.mark_success(
                    "download_dispatcher_task",
                    summary={"quiet_hours": True, "pending_count": 0, "qb_connected": False},
                )
                if self.stop_event.wait(self.config.DOWNLOAD_DISPATCHER_INTERVAL):
                    break
                continue
            pending_count = 0
            submitted_count = 0
            failed_count = 0
            skipped_count = 0
            deferred_count = 0
            qb_connected = False
            try:
                pending = self.db.get_pending_downloads(limit=5)
                pending_count = len(pending or [])

                session, base_url = self.get_qb_session()
                if not session:
                    for task in pending:
                        self.db.defer_download_for_external_service(task["id"], "qb_unavailable")
                        deferred_count += 1
                    if pending and time.time() - self._dispatcher_last_warn > 60:
                        self.logger.warning(
                            "Dispatcher: deferred %s tasks because qBittorrent is not connected",
                            len(pending),
                        )
                        self._dispatcher_last_warn = time.time()
                else:
                    qb_connected = True
                    if pending:
                        self.logger.info("Dispatcher: found %s pending tasks", len(pending))
                        movie_ids = [task.get("movie_id") for task in pending if task.get("movie_id")]
                        downloaded_ids = self.db.get_downloaded_movie_ids(movie_ids) if movie_ids else set()
                        local_file_ids = self.db.get_local_file_movie_ids(movie_ids) if movie_ids else set()

                        for task in pending:
                            if self.stop_event.is_set():
                                break

                            try:
                                movie_id = task.get("movie_id")
                                if movie_id and (movie_id in downloaded_ids or movie_id in local_file_ids):
                                    self.db.update_download_status(
                                        task["id"],
                                        "skipped",
                                        "Already downloaded or exists in local library",
                                    )
                                    self.logger.info(
                                        "Dispatcher: skipped already downloaded task %s",
                                        self.display_value_for_log(task.get("title")),
                                    )
                                    skipped_count += 1
                                    continue

                                self.logger.info("Dispatcher: submitting task %s", self.display_value_for_log(task.get("title")))

                                # 下载目录 = 投递目录：qB 的 relocate 在 /downloads ↔ /hp
                                # 之间是跨挂载点，rename 直接 EXDEV、只能整份拷贝，qB
                                # 自己搬不动（实测 26 次回滚）。所以别再「先下到 /downloads
                                # 再搬进 /hp」，直接下到投递目录，落位就是天然成立的。
                                target_save = task.get("save_path") or self.get_qb_delivery_folder()

                                success = self.qb_api.add_magnet(
                                    session,
                                    base_url,
                                    magnet=task["magnet"],
                                    save_path=target_save,
                                    category=task.get("category"),
                                    tags=["jav", "BusTerminal"],
                                    paused=True,
                                )

                                if success:
                                    # 暂停加种 → 只留最大的那个文件 → 恢复。
                                    # 放在 submitted 之前：选片没做完就不该算提交完。
                                    policy = self.apply_post_add_file_policy(
                                        session,
                                        base_url,
                                        self.magnet_infohash(task.get("magnet")),
                                    )
                                    self.db.update_download_status(task["id"], "submitted")
                                    self.logger.info(
                                        "Dispatcher: submitted task %s (选片=%s)",
                                        self.display_value_for_log(task.get("title")),
                                        "kept" if policy.get("filtered") else policy.get("reason"),
                                    )
                                    submitted_count += 1
                                else:
                                    self.db.defer_download_for_external_service(
                                        task["id"],
                                        "qb_refused_or_timeout",
                                    )
                                    self.logger.warning(
                                        "Dispatcher: deferred task %s (QB refused)",
                                        self.display_value_for_log(task.get("title")),
                                    )
                                    deferred_count += 1

                            except Exception as e:
                                error_label = self.safe_exception_label(e)
                                self.db.defer_download_for_external_service(task["id"], error_label)
                                self.logger.error(
                                    "Dispatcher: deferred task %s after external error: %s",
                                    self.display_value_for_log(task.get("title")),
                                    error_label,
                                )
                                deferred_count += 1

                self.task_registry.mark_success(
                    "download_dispatcher_task",
                    summary={
                        "pending_count": pending_count,
                        "submitted_count": submitted_count,
                        "failed_count": failed_count,
                        "skipped_count": skipped_count,
                        "deferred_count": deferred_count,
                        "qb_connected": qb_connected,
                    },
                )

            except Exception as e:
                self.logger.error("Dispatcher task error: %s", self.safe_exception_label(e))
                self.task_registry.mark_error("download_dispatcher_task", e)

            self.stop_event.wait(self.config.DOWNLOAD_DISPATCHER_INTERVAL)

    # ------------------- Download queue management (per-entry) -------------------
    ALLOWED_QUEUE_STATUS_FILTERS = {
        "pending",
        "waiting_external_service",
        "submitted",
        "failed",
        "duplicate_skipped",
        "cancelled",
    }

    def _safe_display_path(self, save_path: Optional[str]) -> Optional[str]:
        """Return only the last one or two path segments so the UI can show a
        logical location (e.g. codex-validation/tears-of-steel) without leaking
        the absolute delivery root."""
        if not save_path:
            return None
        text = str(save_path).replace("\\", "/").rstrip("/")
        if not text:
            return None
        segments = [seg for seg in text.split("/") if seg]
        if not segments:
            return None
        return "/".join(segments[-2:])

    def _serialize_queue_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Project a download_queue row into a safe payload for the API.

        Drops the raw magnet URI and absolute save_path; exposes only fields
        needed for observability and manual recovery.
        """
        return {
            "id": item.get("id"),
            "movie_id": item.get("movie_id"),
            "title": item.get("title"),
            "status": item.get("status"),
            "retry_count": int(item.get("retry_count") or 0),
            "next_retry_at": item.get("next_retry_at"),
            "error_message": item.get("error_message"),
            "category": item.get("category"),
            "save_path_display": self._safe_display_path(item.get("save_path")),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        }

    def list_queue(self, status: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
        if status and status not in self.ALLOWED_QUEUE_STATUS_FILTERS:
            return {"success": False, "message": "invalid_status_filter"}
        result = self.db.list_download_queue(status=status, limit=limit, offset=offset)
        return {
            "success": True,
            "data": {
                "items": [self._serialize_queue_item(row) for row in result["items"]],
                "total": result["total"],
                "limit": result["limit"],
                "offset": result["offset"],
                "by_status": result["by_status"],
            },
        }

    def retry_task(self, task_id: str) -> Dict[str, Any]:
        outcome = self.db.retry_download_now(task_id)
        if not outcome.get("ok"):
            return {"success": False, "message": outcome.get("reason"), "status": outcome.get("status")}
        return {"success": True, "data": {"task_id": task_id, "status": outcome.get("status")}}

    def recover_task(self, task_id: str) -> Dict[str, Any]:
        outcome = self.db.recover_download(task_id)
        if not outcome.get("ok"):
            return {"success": False, "message": outcome.get("reason"), "status": outcome.get("status")}
        return {"success": True, "data": {"task_id": task_id, "status": outcome.get("status")}}

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        outcome = self.db.cancel_download(task_id)
        if not outcome.get("ok"):
            return {"success": False, "message": outcome.get("reason"), "status": outcome.get("status")}
        return {"success": True, "data": {"task_id": task_id, "status": outcome.get("status")}}

    def reconcile_download_queue_once(self, dry_run: bool = False) -> Dict[str, Any]:
        """对账 download_queue 与 qB 里的真实种子（幂等，可反复跑）。

        背景（2026-11-11 业务体检）：499 条 ``submitted`` 里只有 181 个 btih
        还能在 qB 找到，**318 个早已不在**（最老 303.9 天），另有 6 个
        ``metaDL`` 死种还挂在队列里。原因是队列状态只在「提交」时写一次，
        之后无论下载完成、被删种还是根本没开始，都永远停在 ``submitted`` ——
        既让运维看不出真实结果，又占着 active 唯一索引阻止同片重新下载。

        只改队列状态，**不动 qB 里的任何种子**（删种属于独立的运维动作）。
        ``pending`` 不参与对账：它还没提交到 qB，hash 查不到是正常的。
        """
        session, base_url = self.get_qb_session()
        if not session:
            return {"success": False, "message": "QB is not configured or unavailable"}
        try:
            torrents = self.qb_api.get_torrents(session, base_url) or []
        except Exception as exc:
            return {
                "success": False,
                "message": "qb_torrents_unavailable",
                "error": self.safe_exception_label(exc),
            }

        by_hash: Dict[str, Dict[str, Any]] = {}
        for torrent in torrents:
            if not isinstance(torrent, dict):
                continue
            info_hash = magnet_availability.info_hash_to_hex(torrent.get("hash"))
            if info_hash:
                by_hash[info_hash] = torrent

        rows = self.db.get_active_queue_entries_for_reconcile()
        now = time.time()
        updates: List[Dict[str, Any]] = []
        stats = {
            "success": True,
            "dry_run": bool(dry_run),
            "scanned": len(rows),
            "qb_torrents": len(by_hash),
            "cancelled_absent": 0,
            "cancelled_dead": 0,
            "unparseable": 0,
            "unchanged": 0,
        }

        for row in rows:
            info_hash = magnet_availability.info_hash_to_hex(row.get("magnet"))
            if not info_hash:
                stats["unparseable"] += 1
                continue
            created = float(row.get("created_at") or 0)
            torrent = by_hash.get(info_hash)
            if torrent is None:
                if created and now - created >= QUEUE_RECONCILE_GRACE_SECONDS:
                    updates.append({
                        "id": row["id"],
                        "status": "cancelled",
                        "error_message": "reconciled: torrent_absent_from_qb",
                    })
                    stats["cancelled_absent"] += 1
                    continue
                stats["unchanged"] += 1
                continue

            state = str(torrent.get("state") or "")
            added_on = float(torrent.get("added_on") or 0)
            aged = (
                now - added_on >= QUEUE_RECONCILE_GRACE_SECONDS
                if added_on
                else (created and now - created >= QUEUE_RECONCILE_GRACE_SECONDS)
            )
            if state in DEAD_TORRENT_STATES and aged:
                updates.append({
                    "id": row["id"],
                    "status": "cancelled",
                    "error_message": f"reconciled: dead_torrent_{state}",
                })
                stats["cancelled_dead"] += 1
                continue
            stats["unchanged"] += 1

        stats["applied"] = 0 if dry_run else self.db.apply_queue_reconciliation(updates)
        stats["pending_updates"] = len(updates)
        return stats
