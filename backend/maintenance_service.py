import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional

import javbus_path

# 回填游标的日期格式：严格的 YYYY-MM-DD（时间戳等脏数据不算）
_BACKFILL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class MaintenanceService:
    DAILY_MAINTENANCE_RUN_HOUR = 2
    DAILY_MAINTENANCE_CHECKPOINT_KEY = "daily_maintenance_last_success_date"
    BACKFILL_CURSOR_KEY = "translation_backfill_cursor"
    # 回填游标起点：从最新发行日期往回扫
    BACKFILL_CURSOR_START = "9999-12-31"
    # 翻译队列上限：排到这个数还没翻完，就先停止喂料（模型跟不上或不可用）
    TRANSLATION_QUEUE_MAX = 2000

    def __init__(
        self,
        *,
        db: Any,
        qb_api: Any,
        config: Any,
        task_registry: Any,
        scraper: Any,
        logger: Any,
        stop_event: Any,
        cover_cache_dir: str,
        cache_cleanup_image_suffixes: set,
        pending_scan_interval_seconds: int,
        information_flow_preload_pages: int,
        get_qb_session: Callable[[], Any],
        get_disk_threshold_bytes: Callable[[], float],
        get_subscription_paused: Callable[[], bool],
        set_subscription_paused: Callable[[bool], None],
        safe_number: Callable[..., float],
        safe_exception_label: Callable[[Exception], str],
        display_path_for_log: Callable[[Any], str],
        normalize_magnet_url: Callable[[Any], Optional[str]],
        evaluate_download_permission: Callable[..., Dict[str, Any]],
        queue_download_if_absent: Callable[[str, str, str, Optional[str]], Dict[str, Any]],
        select_due_pending_downloads: Callable[..., list],
        summarize_pending_downloads: Callable[[Any], Dict[str, Any]],
        is_pending_exhausted: Callable[[Any], bool],
        no_magnet_max_attempts: int,
        preload_information_flow_cache: Callable[..., Any],
        is_safe_cache_file: Callable[..., bool],
    ):
        self.db = db
        self.qb_api = qb_api
        self.config = config
        self.task_registry = task_registry
        self.scraper = scraper
        self.logger = logger
        self.stop_event = stop_event
        self.cover_cache_dir = cover_cache_dir
        self.cache_cleanup_image_suffixes = cache_cleanup_image_suffixes
        self.pending_scan_interval_seconds = pending_scan_interval_seconds
        self.information_flow_preload_pages = information_flow_preload_pages
        self.get_qb_session = get_qb_session
        self.get_disk_threshold_bytes = get_disk_threshold_bytes
        self.get_subscription_paused = get_subscription_paused
        self.set_subscription_paused = set_subscription_paused
        self.safe_number = safe_number
        self.safe_exception_label = safe_exception_label
        self.display_path_for_log = display_path_for_log
        self.normalize_magnet_url = normalize_magnet_url
        self.evaluate_download_permission = evaluate_download_permission
        self.queue_download_if_absent = queue_download_if_absent
        self.select_due_pending_downloads = select_due_pending_downloads
        self.summarize_pending_downloads = summarize_pending_downloads
        self.is_pending_exhausted = is_pending_exhausted
        self.no_magnet_max_attempts = no_magnet_max_attempts
        self.preload_information_flow_cache = preload_information_flow_cache
        self.is_safe_cache_file = is_safe_cache_file
        self.cache_size_limit_provider: Optional[Callable[[], float]] = None
        self.cache_cleanup_lock = threading.Lock()
        self.last_cache_cleanup_summary: Optional[Dict[str, Any]] = None
        # 可选：空间告急时触发的已看片自动删除回调（由 main.py 注入，避免循环依赖）
        self.auto_delete_watched_provider: Optional[Callable[[str], Dict[str, Any]]] = None
        self._auto_delete_space_cooldown_until: float = 0.0

    def _space_emergency_auto_delete(self) -> None:
        """磁盘空间告急时，若开启自动删除且模式含 space，触发一次紧急清理。"""
        provider = self.auto_delete_watched_provider
        if not provider:
            return
        try:
            enabled = str(self.db.get_config("jellyfin_auto_delete_enabled") or "").lower() in ("1", "true", "yes", "on")
            if not enabled:
                return
            mode = str(self.db.get_config("auto_delete_mode") or "both").strip().lower()
            if mode not in ("space", "both"):
                return
            now = time.time()
            if now < self._auto_delete_space_cooldown_until:
                return
            self.logger.warning("Disk space low: triggering watched-movie auto-delete (space_emergency)")
            self._auto_delete_space_cooldown_until = now + 3600  # 1 小时冷却
            provider("space_emergency")
        except Exception as exc:
            self.logger.error("Space-emergency auto-delete failed: %s", self.safe_exception_label(exc))

    def check_disk_space_logic(self, trigger_source: str = "auto") -> None:
        try:
            session, base_url = self.get_qb_session()
            if session:
                state = self.qb_api.get_server_state(session, base_url)
                free_space = state.get("free_space_on_disk", 0)
                threshold_bytes = self.get_disk_threshold_bytes()

                self.logger.debug(
                    "[%s] Disk check: available %.2fGB / threshold %.2fGB",
                    trigger_source,
                    free_space / (1024**3),
                    threshold_bytes / (1024**3),
                )

                paused = self.get_subscription_paused()
                if free_space < threshold_bytes and free_space > 0:
                    # 优先尝试自动删除已看片腾出空间，再决定是否暂停下载
                    self._space_emergency_auto_delete()
                    # 重新读取空间（删除后可能已恢复）
                    try:
                        state = self.qb_api.get_server_state(session, base_url)
                        free_space = state.get("free_space_on_disk", 0)
                    except Exception:
                        pass
                    if free_space < threshold_bytes and free_space > 0:
                        if not paused:
                            self.logger.warning(
                                "Disk space is low (%.2f GB < %.0f GB); pausing download tasks.",
                                free_space / (1024**3),
                                threshold_bytes / (1024**3),
                            )
                            self.set_subscription_paused(True)
                            self.qb_api.pause_torrents(session, base_url, hashes="all")
                else:
                    if paused and free_space >= threshold_bytes:
                        self.logger.info(
                            "Disk space recovered (%.2f GB >= %.0f GB); resuming downloads.",
                            free_space / (1024**3),
                            threshold_bytes / (1024**3),
                        )
                        self.set_subscription_paused(False)
                        self.qb_api.resume_torrents(session, base_url, hashes="all")
                    elif trigger_source == "manual" and not paused:
                        self.qb_api.resume_torrents(session, base_url, hashes="all")

                if trigger_source == "auto":
                    self.task_registry.mark_success(
                        "disk_space_monitor_task",
                        summary={
                            "free_space_gb": round(free_space / (1024**3), 2),
                            "threshold_gb": round(threshold_bytes / (1024**3), 2),
                            "paused": self.get_subscription_paused(),
                        },
                    )

        except Exception as e:
            self.logger.error("Disk space check failed: %s", self.safe_exception_label(e))
            if trigger_source == "auto":
                self.task_registry.mark_error("disk_space_monitor_task", e)

    def disk_space_monitor_task(self) -> None:
        while not self.stop_event.is_set():
            self.check_disk_space_logic(trigger_source="auto")
            self.stop_event.wait(self.config.DISK_MONITOR_INTERVAL)

    def get_cache_size_limit_gb(self) -> float:
        if self.cache_size_limit_provider:
            return self.cache_size_limit_provider()
        fallback = self.safe_number(getattr(self.config, "CACHE_SIZE_LIMIT_GB", 10.0), 10.0)
        limit_gb = self.safe_number(self.db.get_config("cache_size_limit_gb"), fallback)
        return limit_gb if 0.1 <= limit_gb <= 1000 else fallback

    def run_cache_cleanup_once(self) -> Dict[str, Any]:
        if not self.cache_cleanup_lock.acquire(blocking=False):
            return {
                "status": "already_running",
                "limit_gb": self.get_cache_size_limit_gb(),
                "deleted_count": 0,
                "freed_mb": 0,
            }

        try:
            summary = self._run_cache_cleanup_locked()
            self.last_cache_cleanup_summary = summary
            return summary
        finally:
            self.cache_cleanup_lock.release()

    def _run_cache_cleanup_locked(self) -> Dict[str, Any]:
        deleted_count = 0
        freed_bytes = 0
        total_size = 0
        limit_gb = self.get_cache_size_limit_gb()
        limit_bytes = limit_gb * (1024 ** 3)
        trigger_bytes = limit_bytes * 0.9
        target_bytes = limit_bytes * 0.8
        files = []

        if os.path.exists(self.cover_cache_dir):
            for root, _dirs, filenames in os.walk(self.cover_cache_dir):
                for filename in filenames:
                    path = os.path.join(root, filename)
                    allowed_suffixes = self._cache_cleanup_allowed_suffixes(path)
                    if not self.is_safe_cache_file(path, self.cover_cache_dir, allowed_suffixes):
                        continue
                    try:
                        stat = os.stat(path)
                    except OSError:
                        continue
                    total_size += stat.st_size
                    files.append((path, stat.st_mtime, stat.st_size))

        size_before = total_size
        if total_size >= trigger_bytes:
            self.logger.info("Cover cache reached cleanup watermark %.2fGB / %.2fGB; pruning oldest files", total_size / (1024**3), limit_gb)
            files.sort(key=lambda item: item[1])

            for path, _mtime, size in files:
                if total_size <= target_bytes:
                    break
                try:
                    allowed_suffixes = self._cache_cleanup_allowed_suffixes(path)
                    if not self.is_safe_cache_file(path, self.cover_cache_dir, allowed_suffixes):
                        continue
                    os.remove(path)
                    total_size -= size
                    freed_bytes += size
                    deleted_count += 1
                except Exception as ignored:
                    self.logger.debug("Failed to delete cache file %s: %s", self.display_path_for_log(path), self.safe_exception_label(ignored))

            self.logger.info("Cover cache cleanup removed %s files and freed %.2fMB", deleted_count, freed_bytes / (1024**2))

        return {
            "status": "cleaned" if deleted_count else "within_limit",
            "cache_size_before_gb": round(size_before / (1024**3), 3),
            "cache_size_gb": round(total_size / (1024**3), 3),
            "limit_gb": limit_gb,
            "trigger_ratio": 0.9,
            "target_ratio": 0.8,
            "scanned_count": len(files),
            "deleted_count": deleted_count,
            "freed_mb": round(freed_bytes / (1024**2), 2),
            "completed_at": time.time(),
        }

    def _cache_cleanup_allowed_suffixes(self, path: str) -> set:
        if (
            os.path.basename(os.path.dirname(path)) == "proxy_failures"
            and os.path.splitext(path)[1].lower() == ".json"
        ):
            return {".json"}
        return self.cache_cleanup_image_suffixes

    def cache_cleanup_task(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.task_registry.mark_success(
                    "cache_cleanup_task",
                    summary=self.run_cache_cleanup_once(),
                )
            except Exception as e:
                self.logger.error("Cache cleanup task failed: %s", self.safe_exception_label(e))
                self.task_registry.mark_error("cache_cleanup_task", e)

            self.stop_event.wait(3600)

    def pending_download_refresh_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                # 取全量 pending，且「最早加入优先」：等得最久的条目该先重试，
                # 不能只盯着最新加入的那批（老条目会永远轮不到）。
                pending_rows = self.db.get_pending_downloads_list(
                    limit=5000, order_by_added=False
                )
                due_list = self.select_due_pending_downloads(pending_rows, limit=50)
                if not due_list:
                    self.task_registry.mark_success(
                        "pending_download_refresh_loop",
                        summary=self.summarize_pending_downloads(pending_rows),
                    )
                    self.stop_event.wait(self.pending_scan_interval_seconds)
                    continue

                self.logger.info("Refreshing %s due pending downloads", len(due_list))
                current_best = self.scraper.find_best_domain()
                self.scraper.base_url = current_best

                success_count = 0
                fail_count = 0
                exhausted_count = 0

                for item in due_list:
                    if self.stop_event.is_set():
                        break

                    m_id = item["movie_id"]
                    m_title = item.get("title", "Unknown")

                    # 「源站没磁力」重试够多次：不再自动打源站（省请求也省带宽）。
                    # 保留在 pending 里，等源站新增磁力或人工在页面上重试。
                    if self.is_pending_exhausted(item):
                        self.db.update_pending_download_status(
                            m_id,
                            "pending",
                            note="No magnet on source; gave up after %s attempts"
                            % self.no_magnet_max_attempts,
                        )
                        exhausted_count += 1
                        continue

                    if self.db.is_movie_downloaded(m_id):
                        self.db.update_pending_download_status(m_id, "downloaded")
                        success_count += 1
                        continue

                    if self.db.get_local_file(m_id):
                        self.db.update_pending_download_status(m_id, "downloaded")
                        self.db.record_download(m_id)
                        success_count += 1
                        continue

                    try:
                        self.logger.info("Searching magnet links for pending movie %s", m_id)
                        # 番号要换成 JavBus 的路径形态：库里 id 有 amane 的 0 填充写法
                        # （jur00696 / gma00090），直接拼进 URL 必然 404
                        # （2026-11-07 实测：gma00090 → 404，GMA-090 → 200 且有 gid）。
                        detail_url = javbus_path.build_detail_url(self.scraper.base_url, m_id)
                        detail = self.scraper.get_movie_detail(detail_url, perform_translate=False)

                        if detail and detail.get("magnets"):
                            best_magnet = self.scraper.select_alive_magnet(detail["magnets"], preference="1080P")
                            if best_magnet:
                                selected_magnet = self.normalize_magnet_url(best_magnet.get("magnet"))
                                if selected_magnet and self.db.get_active_queue_entry(movie_id=m_id, magnet=selected_magnet):
                                    self.db.update_pending_download_status(m_id, "submitted", magnet=selected_magnet, note="active queue already exists")
                                    success_count += 1
                                    continue
                                permission = self.evaluate_download_permission(
                                    m_id,
                                    selected_magnet,
                                    f"{m_title} ({best_magnet['resolution']})",
                                    "pending_download_refresh",
                                    automated=True,
                                )
                                if not permission["allowed"]:
                                    self.db.update_pending_download_status(m_id, "pending", note=f"blocked: {', '.join(permission['reasons'])}")
                                    fail_count += 1
                                    continue
                                self.queue_download_if_absent(
                                    selected_magnet,
                                    m_id,
                                    f"{m_title} ({best_magnet['resolution']})",
                                    "pending_download_refresh",
                                )
                                self.db.update_pending_download_status(m_id, "submitted", magnet=selected_magnet)
                                success_count += 1
                                self.logger.info("Pending movie %s submitted to download queue", m_id)
                            else:
                                self.db.update_pending_download_status(m_id, "pending", note="No suitable magnet found")
                                fail_count += 1
                        else:
                            self.db.update_pending_download_status(m_id, "pending", note="No magnet on source; retry later")
                            fail_count += 1
                    except Exception as e:
                        error_label = self.safe_exception_label(e)
                        self.logger.error("Failed to refresh pending movie %s magnets: %s", m_id, error_label)
                        self.db.update_pending_download_status(m_id, "error", note=error_label)
                        fail_count += 1

                summary = self.summarize_pending_downloads(
                    self.db.get_pending_downloads_list(limit=5000)
                )
                summary.update({
                    "success": success_count,
                    "failed": fail_count,
                    "exhausted": exhausted_count,
                })
                self.task_registry.mark_success("pending_download_refresh_loop", summary=summary)
                self.logger.info("Pending download refresh finished: success=%s, failed=%s", success_count, fail_count)

            except Exception as e:
                self.logger.error("Pending download refresh loop failed: %s", self.safe_exception_label(e))
                self.task_registry.mark_error("pending_download_refresh_loop", e)

            self.stop_event.wait(self.pending_scan_interval_seconds)

    def run_translation_backfill_once(self, batch_size: int) -> Dict[str, Any]:
        """从积压里按发行日期从新到旧取片子排进翻译队列（一次一批，由队列线程慢慢翻）。"""
        if not getattr(self.scraper, "ollama_translator", None):
            return {"skipped": "local_llm_not_configured"}

        # 队列积压太多说明翻不过来（模型慢/不可用），先不推进游标，避免把积压游标空推过去
        if self.scraper.pending_translation_queue_size() >= self.TRANSLATION_QUEUE_MAX:
            return {"skipped": "queue_full"}

        cursor = self.db.get_config(self.BACKFILL_CURSOR_KEY)
        cursor_date, cursor_title = self.parse_backfill_cursor(cursor)

        rows = self.db.get_translation_backfill_batch(cursor_date, batch_size, cursor_title)
        if not rows:
            # 从新到旧整轮扫完：回到最新发行日期重扫，捞漏翻/翻失败的
            self.db.set_config(self.BACKFILL_CURSOR_KEY, self.BACKFILL_CURSOR_START)
            return {"sweep": "restart", "queued": 0}

        titles = [row["original_title"] for row in rows]
        queued = self.scraper.queue_title_translations(titles, priority=False)
        # 游标推到本批**最后一条**（按 release_date DESC, original_title ASC 排序）。
        # 带上标题才能在同一天内继续往后翻 —— 只按日期推进会卡在这一天上，
        # 后面 122 万条永远翻不到（2026-11-09 实测：2026-01-22 有 213 条待翻）。
        oldest_date = min(str(row["release_date"]) for row in rows)
        oldest_title = ""
        for row in rows:
            if str(row["release_date"]) == oldest_date:
                oldest_title = str(row["original_title"])
        next_cursor = self.format_backfill_cursor(oldest_date, oldest_title)
        self.db.set_config(self.BACKFILL_CURSOR_KEY, next_cursor)
        return {"queued": queued, "cursor": next_cursor}

    @staticmethod
    def parse_backfill_cursor(raw: Any) -> tuple:
        """解析游标 → (日期, 标题)。兼容旧的纯日期游标（没有标题部分）。

        旧游标只有日期时，标题留空 → 取数会带上当天的全部待翻条目，
        第一轮就能把当天翻完并把游标推进到当天最后一条标题。

        ⚠️ 日期必须严格是 `YYYY-MM-DD`：历史遗留的**时间戳**游标
        （`2026-01-22T10:00:00`）不能当日期用 —— 它会被塞进 SQL 的
        `release_date <= cursor` 里比较，既匹配不到任何一天，游标也会卡死。
        """
        text = str(raw or "").strip()
        if not text:
            return MaintenanceService.BACKFILL_CURSOR_START, ""
        if "|" in text:
            date_part, _, title_part = text.partition("|")
            date_part = date_part.strip()
            if _BACKFILL_DATE_RE.match(date_part):
                return date_part, title_part.strip()
            return MaintenanceService.BACKFILL_CURSOR_START, ""
        if _BACKFILL_DATE_RE.match(text):
            return text, ""
        # 时间戳等历史脏数据一律从头重来
        return MaintenanceService.BACKFILL_CURSOR_START, ""

    @staticmethod
    def format_backfill_cursor(date_str: str, title: str) -> str:
        """日期|标题 的游标格式。"""
        title = str(title or "").strip()
        return f"{date_str}|{title}" if title else str(date_str)

    def translation_backfill_loop(self) -> None:
        """历史标题翻译回填喂料：连续把片子排进翻译队列，队列满了就等着，重启后按游标续跑。"""
        batch_size = max(int(getattr(self.config, "TRANSLATION_BACKFILL_BATCH", 8)), 1)
        interval = max(int(getattr(self.config, "TRANSLATION_BACKFILL_INTERVAL_SECONDS", 0)), 0)
        while not self.stop_event.is_set():
            summary = {}
            try:
                summary = self.run_translation_backfill_once(batch_size)
                self.task_registry.mark_success("translation_backfill_loop", summary=summary)
            except Exception as e:
                self.logger.error("Translation backfill failed: %s", self.safe_exception_label(e))
                self.task_registry.mark_error("translation_backfill_loop", e)

            # 队列满 / 本轮报错：退避几秒再看，别空转刷日志；正常则按配置间隔（0 = 连续喂）
            if summary.get("skipped") or not summary:
                self.stop_event.wait(max(interval, 5))
            else:
                self.stop_event.wait(interval)

    def daily_maintenance_task(self) -> None:
        last_run_date = str(self.db.get_config(self.DAILY_MAINTENANCE_CHECKPOINT_KEY) or "")
        while not self.stop_event.is_set():
            now = datetime.now()
            today = now.date()
            today_key = today.isoformat()
            should_run = self.should_run_daily_maintenance(now, last_run_date)

            if should_run:
                try:
                    self.logger.info("Starting daily maintenance task")
                    self.preload_information_flow_cache(
                        max_pages=self.information_flow_preload_pages,
                        force_refresh=True,
                    )
                    last_run_date = today_key
                    self.db.set_config(self.DAILY_MAINTENANCE_CHECKPOINT_KEY, today_key)
                    self.task_registry.mark_success(
                        "daily_maintenance_task",
                        summary={"completed_date": today.isoformat()},
                    )
                    self.logger.info("Daily maintenance task completed")
                except Exception as e:
                    self.logger.error("Daily maintenance task failed: %s", self.safe_exception_label(e))
                    self.task_registry.mark_error("daily_maintenance_task", e)

            self.stop_event.wait(60)

    @classmethod
    def should_run_daily_maintenance(cls, now: datetime, last_run_date: Any) -> bool:
        return now.hour >= cls.DAILY_MAINTENANCE_RUN_HOUR and str(last_run_date or "") != now.date().isoformat()
