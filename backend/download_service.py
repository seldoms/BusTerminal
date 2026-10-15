import os
import time
from typing import Any, Callable, Dict, List, Optional


MIN_JAV_VIDEO_FILE_SIZE_BYTES = 400 * 1024 * 1024
JAV_FILE_FILTER_TAG = "smart_filter_400mb"
SEEDING_LIMIT_TAG = "seeding_limit_set"
DOWNLOAD_RECORDED_TAG = "download_recorded"
VIDEO_FILE_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".rmvb", ".ts", ".m2ts"}


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
        self._dispatcher_last_warn = 0.0

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
        if movie_id and not allowed:
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
        filter_torrent_files = filter_torrent_files or self.filter_jav_torrent_files

        for torrent in torrents or []:
            if not isinstance(torrent, dict):
                skipped += 1
                continue
            tags = set(self.normalize_qb_tags(torrent.get("tags")))
            if JAV_FILE_FILTER_TAG in tags or not self.is_groomable_torrent(torrent):
                skipped += 1
                continue

            scanned += 1
            result = filter_torrent_files(session, base_url, torrent)
            if result.get("filtered"):
                filtered += 1
            else:
                small_files_disabled += int(result.get("small_files_disabled") or 0)

        return {
            "success": True,
            "scanned": scanned,
            "filtered": filtered,
            "skipped": skipped,
            "small_files_disabled": small_files_disabled,
            "min_file_size_mb": 400,
        }

    def groom_torrents_task(
        self,
        filter_torrent_files: Optional[Callable[[Any, str, Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> None:
        filter_torrent_files = filter_torrent_files or self.filter_jav_torrent_files
        while not self.stop_event.is_set():
            torrent_count = 0
            delivered_count = 0
            downloaded_count = 0
            filtered_count = 0
            seeding_wait_count = 0
            try:
                session, base_url = self.get_qb_session()
                if not session:
                    if self.stop_event.wait(self.config.GROOM_TASK_INTERVAL):
                        break
                    continue

                # 只处理 BusTerminal 自己发起的种子（add_magnet 打的 "jav" tag）。
                # 这个 qB 与 MoviePilot 共用（辅种 / 刷流-自动 等共 600+ 个种子），
                # 投递目录现在落在 amane 库内，误搬别人的种子会堆成待人工垃圾。
                torrents = self.qb_api.get_torrents(session, base_url, tag="jav")
                torrent_count = len(torrents or [])

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
                            "Applying JAV 400MB file filter: %s (hash=%s)",
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

                    if self.safe_number(torrent.get("progress"), 0) < 1 or "delivered" in tags:
                        continue

                    state = state_now
                    # pausedup 必须在内：qB 达到做种限制后会把种子转成 pausedUP，
                    # 生产上已完成的 6 个 jav 种子全是这个状态，漏掉就一个都投递不出去。
                    # pauseddl 不能放进来 —— 那是「没下完且暂停下载」。
                    if state not in [
                        "completed", "uploading", "stalledup", "forcedup", "seeding", "pausedup",
                    ]:
                        continue

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

                    target_delivery = self.get_qb_delivery_folder()
                    if not target_delivery:
                        self.logger.warning("Invalid qB delivery folder configured; torrent delivery skipped")
                        # 没有投递目录就当文件已在库内，照旧入库，否则片子永远进不了 amane。
                        self._notify_download_completed(movie_id, torrent)
                        continue
                    if self.qb_api.set_torrent_location(session, base_url, [torrent_hash], target_delivery):
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

                                success = self.qb_api.add_magnet(
                                    session,
                                    base_url,
                                    magnet=task["magnet"],
                                    save_path=task.get("save_path"),
                                    category=task.get("category"),
                                    tags=["jav", "BusTerminal"],
                                )

                                if success:
                                    self.db.update_download_status(task["id"], "submitted")
                                    self.logger.info("Dispatcher: submitted task %s", self.display_value_for_log(task.get("title")))
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
