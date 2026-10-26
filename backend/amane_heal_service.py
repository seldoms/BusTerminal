"""amane 入库自愈：绕开 refresh 失败，把滞留影片直接补号 + 刮削 + 整理。

生产实测下 amane 的 refresh 会报 `UNIQUE constraint failed: media_files.path`，但
文件其实已经登记进 media_files（一条 status=pending、number 为空的行），只是从没
被刮削，于是永远停在投递目录。既然 `GET /api/media?search=<番号>` 能按路径里的番号
命中该行，就直接走"搜 → 补号 → 刮削 → 整理 → 回写状态"，完全不用重跑 refresh。
"""
import logging
import time
from typing import Any, Dict, List, Optional

from amane_client import AmaneError

# 每条自愈之间的间隔：串行 + 小停顿，避免打爆 amane 与其背后的刮削源。
BATCH_INTERVAL_SECONDS = 1.0

# 认为"需要自愈"的 media_file 状态：已登记但没刮完。
HEALABLE_MEDIA_STATUSES = ("pending", "failed")

# organize_status 里代表"卡住"的取值，检测时逐个查询再合并去重。
STUCK_ORGANIZE_STATUSES = ("pending", "failed", "not_found")

# 只有"下载确实完成过"的影片才谈得上入库滞留；否则它只是还没下（生产里这类占了绝大多数）。
REQUIRED_DOWNLOAD_STATUSES = ("delivered", "downloaded")

CONFIG_KEY_ORGANIZE = "amane_organize_enabled"
CONFIG_KEY_LIBRARY_ID = "amane_library_id"


class AmaneHealService:
    def __init__(
        self,
        *,
        db: Any,
        amane_service: Any,
        amane_client: Any = None,
        jellyfin_client: Any = None,
        logger: Any,
        safe_exception_label: Any,
        display_value_for_log: Any,
        config: Any = None,
    ) -> None:
        self.db = db
        self.amane_service = amane_service
        self._amane_client = amane_client
        self._jellyfin_client = jellyfin_client
        self.logger = logger or logging.getLogger(__name__)
        self.safe_exception_label = safe_exception_label or (lambda exc: type(exc).__name__)
        self.display_value_for_log = display_value_for_log or (lambda value: str(value))
        self.config = config

    # ---------- 检测 ----------

    def detect_stuck(self, *, limit: int = 20, older_than_seconds: int = 7200) -> List[Dict[str, Any]]:
        """找出滞留影片：**已下载完成**但迟迟没进 organize 终态。

        必须同时约束 download_status：生产里 organize_status=pending 的绝大多数
        片子其实还没下载（只登记了发现），把它们算进来会让检测结果完全失真。
        数据源是 movie_lifecycle，按 (下载态, 整理态) 组合查询后合并去重，
        再按 updated_at 过滤出超过阈值的老行。
        """
        now = time.time()
        merged: Dict[str, Dict[str, Any]] = {}
        for download_status in REQUIRED_DOWNLOAD_STATUSES:
            for status in STUCK_ORGANIZE_STATUSES:
                try:
                    rows = self.db.get_movie_lifecycle(
                        download_status=download_status, organize_status=status, limit=limit
                    ) or []
                except Exception as exc:
                    self.logger.warning(
                        "amane heal detect query failed for %s/%s: %s",
                        download_status, status, self.safe_exception_label(exc),
                    )
                    continue
                for row in rows:
                    movie_id = row.get("movie_id")
                    if movie_id:
                        merged[movie_id] = row

        stuck: List[Dict[str, Any]] = []
        for row in merged.values():
            updated_at = row.get("updated_at") or 0
            try:
                stuck_seconds = now - float(updated_at)
            except (TypeError, ValueError):
                continue
            if stuck_seconds < older_than_seconds:
                continue
            stuck.append({
                "movie_id": row.get("movie_id"),
                "title": row.get("title"),
                "organize_status": row.get("organize_status"),
                "jellyfin_status": row.get("jellyfin_status"),
                "updated_at": updated_at,
                "stuck_seconds": stuck_seconds,
            })
        stuck.sort(key=lambda item: item["stuck_seconds"], reverse=True)
        return stuck[:limit]

    # ---------- 单条自愈 ----------

    def heal_one(
        self,
        movie_id: str,
        *,
        number: Optional[str] = None,
        title: Optional[str] = None,
        library_id: Optional[int] = None,
        refresh_jellyfin: bool = True,
    ) -> Dict[str, Any]:
        """按番号定位滞留的 media_file，补号后推完刮削与整理。不抛业务异常。"""
        number = str(number or movie_id or "").strip().upper()
        stages: List[Dict[str, Any]] = []
        result: Dict[str, Any] = {
            "movie_id": movie_id,
            "healed": False,
            "stage": None,
            "reason": None,
            "media_file_id": None,
            "stages": stages,
        }
        client = self._client()
        if client is None:
            result["stage"] = "client"
            result["reason"] = "amane_not_configured"
            return result

        # ① 按番号搜 media_file：取 pending/failed 且路径含番号的第一条。
        try:
            listing = client.list_media(search=number, limit=20) or {}
        except AmaneError as exc:
            return self._fail(result, title, "find_media", exc)
        media = self._pick_media(listing.get("items") or [], number)
        if media is None or media.get("id") is None:
            result["stage"] = "find_media"
            result["reason"] = "media_not_found"
            self.logger.info(
                "amane heal: no media_file for %s", self.display_value_for_log(movie_id)
            )
            # 也记一笔：事件会刷新 movie_lifecycle.updated_at，使这条在滞留阈值窗口内
            # 不再被巡检反复检出（否则每轮都白查一次 amane）。整理态保持 pending 不变。
            self._record(movie_id, title, "pending", "media_not_found")
            return result

        media_id = media.get("id")
        result["media_file_id"] = media_id

        # ② 番号为空或错了一律改写；补号失败不致命，继续往下推。
        #    amane 扫描 _incoming 时按原始文件名猜号，站点水印会被当成番号前缀
        #    （`4k2.mestart-646.mp4` → MESTART-646），所以"非空"不等于"正确"。
        if str(media.get("number") or "").strip().upper() != number.upper():
            try:
                client.patch_media(media_id, number=number)
                stages.append({"stage": "patch_media", "status": "done"})
            except AmaneError as exc:
                stages.append({
                    "stage": "patch_media",
                    "status": "failed",
                    "error": getattr(exc, "code", None),
                })
                self.logger.warning(
                    "amane heal patch number failed for %s: %s",
                    self.display_value_for_log(movie_id), self.safe_exception_label(exc),
                )

        # ③ 刮削
        try:
            scrape_task = client.submit_scrape(number=number, media_id=media_id)
            scrape_task = client.wait_for_task(scrape_task.get("id"))
        except AmaneError as exc:
            return self._fail(result, title, "scrape", exc)
        stages.append(self._stage("scrape", scrape_task))
        if self._task_failed(scrape_task):
            return self._fail(
                result, title, "scrape",
                AmaneError(str(scrape_task.get("error") or "scrape_failed")),
            )

        # ④ 整理（amane 未开放自动整理，需显式触发）
        organized = False
        if self._organize_enabled():
            library_id = library_id or self._resolve_library_id()
            if library_id is None:
                result["stage"] = "organize"
                result["reason"] = "no_library"
                self._record(movie_id, title, "failed", "no_library")
                return result
            try:
                organize_task = client.submit_organize(library_id, media_file_ids=[media_id])
                organize_task = client.wait_for_task(organize_task.get("id"))
            except AmaneError as exc:
                return self._fail(result, title, "organize", exc)
            stages.append(self._stage("organize", organize_task))
            if self._task_failed(organize_task):
                return self._fail(
                    result, title, "organize",
                    AmaneError(str(organize_task.get("error") or "organize_failed")),
                )
            organized = True

        # ⑤ 让 Jellyfin 重新扫描：amane 只是把文件挪进正式目录并写好 NFO/封面，
        #    Jellyfin 不刷新就仍指着 _incoming 旧路径、没封面没标题（2026-10-17 生产实测）。
        if organized and refresh_jellyfin:
            self._refresh_jellyfin()

        # ⑥ 回写终态
        result["healed"] = True
        result["stage"] = "completed"
        self._record(movie_id, title, "completed", "healed")
        self.logger.info(
            "amane heal done for %s (media_file_id=%s)",
            self.display_value_for_log(movie_id), media_id,
        )
        return result

    # ---------- 批量自愈 ----------

    def heal_batch(
        self,
        *,
        limit: int = 10,
        older_than_seconds: int = 7200,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """批量自愈。dry_run 默认 True：只检测不写 amane，安全优先。"""
        stuck = self.detect_stuck(limit=limit, older_than_seconds=older_than_seconds)
        report: Dict[str, Any] = {
            "scanned": len(stuck),
            "attempted": 0,
            "healed": 0,
            "failed": 0,
            "skipped": 0,
            "dry_run": bool(dry_run),
            "results": [],
        }
        if dry_run:
            report["would_heal"] = [
                {
                    "movie_id": row.get("movie_id"),
                    "title": row.get("title"),
                    "organize_status": row.get("organize_status"),
                    "stuck_seconds": row.get("stuck_seconds"),
                }
                for row in stuck
            ]
            return report

        for index, row in enumerate(stuck):
            if index:
                time.sleep(BATCH_INTERVAL_SECONDS)
            report["attempted"] += 1
            try:
                # 整批只刷新一次 Jellyfin（每条都刷会把慢盘上的库扫描打爆）。
                outcome = self.heal_one(
                    row.get("movie_id"), title=row.get("title"), refresh_jellyfin=False
                )
            except Exception as exc:  # 兜底：单条意外不能拖垮整批
                outcome = {
                    "movie_id": row.get("movie_id"),
                    "healed": False,
                    "stage": "unexpected",
                    "reason": self.safe_exception_label(exc),
                    "media_file_id": None,
                    "stages": [],
                }
            report["results"].append(outcome)
            if outcome.get("healed"):
                report["healed"] += 1
            elif outcome.get("stage") == "find_media" and outcome.get("reason") == "media_not_found":
                report["skipped"] += 1
            else:
                report["failed"] += 1
        if report["healed"]:
            report["jellyfin_refresh"] = self._refresh_jellyfin()
        return report

    # ---------- 内部 ----------

    def _client(self) -> Any:
        """拿 amane HTTP 客户端：优先用注入的，否则向 amane_service 要。"""
        if self._amane_client is not None:
            return self._amane_client
        if self.amane_service is None:
            return None
        return self.amane_service.get_client()

    def _refresh_jellyfin(self) -> bool:
        """通知 Jellyfin 重扫库；失败只记日志，不影响自愈结果。"""
        client = self._jellyfin_client
        if client is None:
            return False
        try:
            reload_config = getattr(client, "reload_config", None)
            if callable(reload_config):
                reload_config()
            client.refresh_library()
            return True
        except Exception as exc:
            self.logger.warning(
                "amane heal jellyfin refresh failed: %s", self.safe_exception_label(exc)
            )
            return False

    @staticmethod
    def _pick_media(items: List[Dict[str, Any]], number: str) -> Optional[Dict[str, Any]]:
        wanted = str(number or "").upper()
        for item in items or []:
            if str(item.get("status") or "").lower() not in HEALABLE_MEDIA_STATUSES:
                continue
            if wanted and wanted in str(item.get("path") or "").upper():
                return item
        return None

    def _organize_enabled(self) -> bool:
        try:
            raw = self.db.get_config(CONFIG_KEY_ORGANIZE)
        except Exception:
            raw = None
        if raw is None:
            return True
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def _resolve_library_id(self) -> Optional[int]:
        try:
            configured = self.db.get_config(CONFIG_KEY_LIBRARY_ID)
        except Exception:
            configured = None
        if str(configured or "").isdigit():
            return int(configured)
        try:
            libraries = self.amane_service.list_libraries()
        except AmaneError as exc:
            self.logger.warning("amane heal list_libraries failed: %s", self.safe_exception_label(exc))
            return None
        if not libraries:
            return None
        for library in libraries:
            if library.get("automation") == "watch":
                return library.get("id")
        return libraries[0].get("id")

    @staticmethod
    def _stage(name: str, task: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "stage": name,
            "task_id": task.get("id"),
            "status": task.get("status"),
            "error": task.get("error"),
            "wait_timeout": bool(task.get("wait_timeout")),
        }

    @staticmethod
    def _task_failed(task: Dict[str, Any]) -> bool:
        """failed 或轮询超时都算"没成功"。

        超时时快照的 status 往往还停在 running/queued，只看 status 会把它当成
        成功继续往下推（与 amane_ingest_service 同一类坑），这里一并判掉。
        """
        if str(task.get("status") or "").lower() == "failed":
            return True
        return bool(task.get("wait_timeout"))

    def _fail(
        self,
        result: Dict[str, Any],
        title: Optional[str],
        stage: str,
        exc: Exception,
    ) -> Dict[str, Any]:
        label = self.safe_exception_label(exc)
        code = getattr(exc, "code", None) or label
        result["stage"] = stage
        result["reason"] = code
        self.logger.warning(
            "amane heal failed at %s for %s: %s",
            stage, self.display_value_for_log(result.get("movie_id")), label,
        )
        self._record(
            result.get("movie_id"), title, "failed", f"{stage}: {code}",
            error_summary=str(exc)[:240],
        )
        return result

    def _record(
        self,
        movie_id: str,
        title: Optional[str],
        organize_status: str,
        reason: str,
        *,
        error_summary: Optional[str] = None,
    ) -> None:
        try:
            self.db.record_lifecycle_event(
                movie_id=movie_id,
                event_type="amane_heal",
                title=title,
                organize_status=organize_status,
                source="amane",
                reason=reason,
                execution_result="success" if organize_status == "completed" else "failed",
                error_summary=error_summary,
            )
        except Exception as exc:
            self.logger.warning(
                "amane heal lifecycle event failed: %s", self.safe_exception_label(exc)
            )
