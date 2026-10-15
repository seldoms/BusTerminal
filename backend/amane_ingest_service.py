"""入库闭环：下载完成 → amane 扫描/刮削/整理 → 回读 → 写生命周期与身份映射。

BusTerminal 不碰文件，这一层是它在方案 C 下唯一的"入库执行器"：
把决策（某片已下载完成）翻译成 amane 的任务序列，再把结果回写成六维状态。
"""
import logging
import time
from typing import Any, Dict, List, Optional

from amane_client import AmaneError

CONFIG_KEY_ENABLED = "amane_ingest_enabled"
CONFIG_KEY_LIBRARY_ID = "amane_library_id"
CONFIG_KEY_SCAN_PATH = "amane_scan_path"
CONFIG_KEY_ORGANIZE = "amane_organize_enabled"
CONFIG_KEY_VERIFY_VISIBLE = "amane_ingest_verify_visible"
CONFIG_KEY_DEDUPE = "amane_dedupe_enabled"

VISIBILITY_TIMEOUT = 40.0
VISIBILITY_INTERVAL = 5.0


class AmaneTaskTimeout(AmaneError):
    """amane 任务轮询超时。

    超时说明 amane 可能还在跑，跟"任务真失败"是两种处置：这里单独给一个
    `code=timeout`，让 `_fail()` 的 reason 能写出 `stage: timeout` 而不是笼统的失败。
    """
    code = "timeout"


class AmaneIngestService:
    def __init__(
        self,
        *,
        db: Any,
        amane_service: Any,
        logger: Any,
        safe_exception_label: Any = None,
        record_lifecycle_event: Any = None,
        jellyfin_client: Any = None,
    ) -> None:
        self.db = db
        self.amane_service = amane_service
        self.logger = logger or logging.getLogger(__name__)
        self.safe_exception_label = safe_exception_label or (lambda exc: type(exc).__name__)
        self._record_event = record_lifecycle_event or self._default_record_event
        self.jellyfin_client = jellyfin_client

    def _default_record_event(self, **kwargs: Any) -> None:
        try:
            self.db.record_lifecycle_event(**kwargs)
        except Exception as exc:
            self.logger.warning("lifecycle event failed: %s", self.safe_exception_label(exc))

    @staticmethod
    def _bool_config(db: Any, key: str, default: bool) -> bool:
        try:
            raw = db.get_config(key)
        except Exception:
            raw = None
        if raw is None:
            return default
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def get_config(self) -> Dict[str, Any]:
        try:
            library_id = self.db.get_config(CONFIG_KEY_LIBRARY_ID)
            scan_path = self.db.get_config(CONFIG_KEY_SCAN_PATH) or ""
        except Exception:
            library_id, scan_path = None, ""
        return {
            "enabled": self._bool_config(self.db, CONFIG_KEY_ENABLED, True),
            "library_id": int(library_id) if str(library_id or "").isdigit() else None,
            "scan_path": str(scan_path),
            "organize_enabled": self._bool_config(self.db, CONFIG_KEY_ORGANIZE, True),
            "verify_visible": self._bool_config(self.db, CONFIG_KEY_VERIFY_VISIBLE, True),
            "dedupe_enabled": self._bool_config(self.db, CONFIG_KEY_DEDUPE, True),
        }

    def save_config(self, **updates: Any) -> Dict[str, Any]:
        mapping = {
            "enabled": CONFIG_KEY_ENABLED,
            "library_id": CONFIG_KEY_LIBRARY_ID,
            "scan_path": CONFIG_KEY_SCAN_PATH,
            "organize_enabled": CONFIG_KEY_ORGANIZE,
            "verify_visible": CONFIG_KEY_VERIFY_VISIBLE,
            "dedupe_enabled": CONFIG_KEY_DEDUPE,
        }
        for key, value in updates.items():
            if key not in mapping or value is None:
                continue
            self.db.set_config(mapping[key], "true" if value is True else ("false" if value is False else value))
        return self.get_config()

    def resolve_library_id(self) -> Optional[int]:
        """优先用配置指定的库，否则挑第一个 automation=watch 的库，再不行取第一个。"""
        configured = self.get_config().get("library_id")
        if configured:
            return int(configured)
        libraries = self.amane_service.list_libraries()
        if not libraries:
            return None
        for library in libraries:
            if library.get("automation") == "watch":
                return library.get("id")
        return libraries[0].get("id")

    def resolve_scan_path(self, library_id: Optional[int]) -> str:
        """把扫描子目录补成 amane 要求的绝对路径。

        amane 会校验 refresh 的 path 必须是库路径的后代，传 `_incoming` 直接
        422（"Path _incoming is not a descendant of library path /hp"）。配置里
        写相对子目录时，这里用库根拼成绝对路径，免得换个写法就扫不到。
        """
        scan_path = str(self.get_config().get("scan_path") or "").strip()
        if not scan_path or scan_path.startswith("/"):
            return scan_path.rstrip("/")
        try:
            libraries = self.amane_service.list_libraries()
        except Exception:
            libraries = []
        root = ""
        for library in libraries or []:
            if str(library.get("id")) == str(library_id):
                root = str(library.get("path") or "")
                break
        if not root:
            return scan_path
        return f"{root.rstrip('/')}/{scan_path.strip('/')}"

    def _linked_media_id(self, movie_id: str) -> Optional[int]:
        try:
            row = self.db.get_local_file(movie_id)
        except Exception:
            return None
        if not row:
            return None
        value = row.get("amane_media_file_id")
        return int(value) if str(value or "").isdigit() else None

    def check_library_presence(
        self,
        movie_id: str,
        *,
        number: Optional[str] = None,
    ) -> Dict[str, Any]:
        """提交下载前的三方查重（P2-3）：库里已经有就别再下一遍。

        查两处——amane 已索引的媒体文件、Jellyfin 已可见的条目。命中会把身份回填到
        `local_files`，下次 `evaluate_download_permission` 直接走库内判断，不必再打接口。

        查不动（未配置/不可达）一律当作"没查过"返回 `checked=false`：查不到≠没有，
        按这个去拦下载会误杀。
        """
        number = str(number or movie_id or "").strip().upper()
        result: Dict[str, Any] = {
            "movie_id": movie_id,
            "number": number,
            "checked": False,
            "present": False,
            "sources": [],
            "amane": None,
            "jellyfin": None,
            "error": None,
        }
        if not movie_id or not number:
            result["error"] = "missing_number"
            return result
        if not self._bool_config(self.db, CONFIG_KEY_DEDUPE, True):
            result["error"] = "dedupe_disabled"
            return result

        try:
            media = self._find_media(number)
        except AmaneError as exc:
            result["error"] = exc.code
            self.logger.info("library presence check skipped for %s: %s", number, exc.code)
            return result
        result["checked"] = True
        if media:
            result["present"] = True
            result["sources"].append("amane")
            result["amane"] = {
                "media_file_id": media.get("id"),
                "path": media.get("path"),
                "status": media.get("status"),
            }
            self._save_amane_identity(movie_id, media)

        item = self._lookup_jellyfin_item(number)
        if item:
            result["present"] = True
            result["sources"].append("jellyfin")
            result["jellyfin"] = {"item_id": item.get("Id"), "name": item.get("Name")}
            self._save_jellyfin_identity(movie_id, item.get("Id"))
        return result

    def ingest(
        self,
        movie_id: str,
        *,
        number: Optional[str] = None,
        title: Optional[str] = None,
        wait: bool = True,
        force: bool = False,
        source: str = "manual",
    ) -> Dict[str, Any]:
        """执行一次入库。返回可审计的 trace，不抛业务异常。"""
        started = time.time()
        number = str(number or movie_id or "").strip().upper()
        trace: Dict[str, Any] = {
            "movie_id": movie_id,
            "number": number,
            "library_id": None,
            "stages": [],
            "media": None,
            "visibility": None,
            "organize_status": "skipped",
            "reason": None,
            "error": None,
            "elapsed": None,
        }

        config = self.get_config()
        if not config["enabled"]:
            trace["reason"] = "ingest_disabled"
            return self._finish(trace, started)

        if not force and self._linked_media_id(movie_id):
            trace["reason"] = "already_linked"
            return self._finish(trace, started)

        try:
            library_id = self.resolve_library_id()
        except AmaneError as exc:
            return self._fail(trace, started, movie_id, title, exc, stage="resolve_library")
        if not library_id:
            trace["organize_status"] = "failed"
            trace["reason"] = "no_library"
            self._record_event(
                movie_id=movie_id,
                event_type="amane_ingest",
                title=title,
                organize_status="failed",
                source="amane",
                reason="no_library",
                execution_result="failed",
                error_summary="amane 里没有可用媒体库",
            )
            return self._finish(trace, started)
        trace["library_id"] = library_id

        self._record_event(
            movie_id=movie_id,
            event_type="amane_ingest_start",
            title=title,
            organize_status="delivered",
            source="amane",
            reason=source,
            execution_result="recorded",
            payload={"library_id": library_id, "number": number},
        )

        # 1) 扫描：让 amane 登记新文件
        scan_path = self.resolve_scan_path(library_id)
        try:
            scan_task = self.amane_service.trigger_refresh(
                library_id, path=scan_path, wait=wait
            )
        except AmaneError as exc:
            return self._fail(trace, started, movie_id, title, exc, stage="refresh")
        trace["stages"].append(self._stage("refresh", scan_task))
        outcome = self._task_outcome(scan_task)
        if outcome in ("failed", "timeout"):
            return self._fail(
                trace, started, movie_id, title,
                self._task_error(scan_task, outcome, "refresh_failed"),
                stage="refresh",
            )

        # 2) 按番号回读：判断是否真的被登记
        try:
            media = self._find_media(number)
        except AmaneError as exc:
            return self._fail(trace, started, movie_id, title, exc, stage="find_media")
        if not media:
            trace["organize_status"] = "not_found"
            trace["reason"] = "media_not_indexed"
            self._record_event(
                movie_id=movie_id,
                event_type="amane_ingest",
                title=title,
                organize_status="pending",
                source="amane",
                reason="media_not_indexed",
                execution_result="recorded",
                payload={"library_id": library_id, "number": number,
                         "scan_task_id": scan_task.get("id"), "scan_path": scan_path},
            )
            return self._finish(trace, started)

        # 2.5) 校正番号：amane 扫描时按原始文件名猜号，站点水印会被当成番号前缀
        media = self._ensure_media_number(media, number)

        # 3) 刮削：未刮削的补刮
        if str(media.get("status") or "") != "scraped":
            try:
                scrape_task = self.amane_service.trigger_scrape(
                    number=number, media_id=media.get("id"), wait=wait
                )
            except AmaneError as exc:
                return self._fail(trace, started, movie_id, title, exc, stage="scrape")
            trace["stages"].append(self._stage("scrape", scrape_task))
            outcome = self._task_outcome(scrape_task)
            if outcome in ("failed", "timeout"):
                return self._fail(
                    trace, started, movie_id, title,
                    self._task_error(scrape_task, outcome, "scrape_failed"),
                    stage="scrape",
                )
            try:
                media = self._reread_media(media.get("id"), number) or media
            except AmaneError:
                pass
            media = self._ensure_media_number(media, number)

        # 4) 整理归档（amane 未开放自动整理，需显式触发）
        if config["organize_enabled"]:
            try:
                organize_task = self.amane_service.trigger_organize(
                    library_id, media_file_ids=[media.get("id")], wait=wait
                )
            except AmaneError as exc:
                return self._fail(trace, started, movie_id, title, exc, stage="organize")
            trace["stages"].append(self._stage("organize", organize_task))
            # 整理会改路径（改名/归目录），回读一次再落库，别把旧路径写进 local_files
            try:
                media = self._reread_media(media.get("id"), number) or media
            except AmaneError:
                pass
            media = self._ensure_media_number(media, number)
            outcome = self._task_outcome(organize_task)
            if outcome in ("failed", "timeout"):
                return self._fail(
                    trace, started, movie_id, title,
                    self._task_error(organize_task, outcome, "organize_failed"),
                    stage="organize",
                )

        trace["media"] = {
            "id": media.get("id"),
            "path": media.get("path"),
            "status": media.get("status"),
            "size": media.get("size"),
            "number": media.get("number"),
        }
        trace["organize_status"] = "completed"

        self.db.save_local_file_amane(
            movie_id,
            media_file_id=media.get("id"),
            task_id=trace["stages"][-1]["task_id"] if trace["stages"] else None,
            file_path=media.get("path"),
            file_size=media.get("size"),
        )
        self._record_event(
            movie_id=movie_id,
            event_type="amane_ingest_done",
            title=title,
            organize_status="completed",
            jellyfin_status="pending",
            source="amane",
            reason=source,
            execution_result="success",
            payload={"library_id": library_id, "media": trace["media"],
                     "stages": trace["stages"]},
        )

        # 5) 可见性：触发 Jellyfin 扫描并确认该片真的出现了（闭环收尾）
        if config["verify_visible"] and wait:
            trace["visibility"] = self.confirm_visibility(
                movie_id, number=number, title=title, source=source
            )
        return self._finish(trace, started)

    def confirm_visibility(
        self,
        movie_id: str,
        *,
        number: Optional[str] = None,
        title: Optional[str] = None,
        timeout: float = VISIBILITY_TIMEOUT,
        interval: float = VISIBILITY_INTERVAL,
        refresh: bool = True,
        source: str = "manual",
    ) -> Dict[str, Any]:
        """闭环收尾：触发 Jellyfin 扫描，轮询直到该片可见或超时。不抛业务异常。"""
        started = time.time()
        number = str(number or movie_id or "").strip().upper()
        result: Dict[str, Any] = {
            "movie_id": movie_id,
            "number": number,
            "status": "skipped",
            "item_id": None,
            "name": None,
            "attempts": 0,
            "elapsed": None,
            "reason": None,
        }
        client = self.jellyfin_client
        if client is None:
            result["reason"] = "jellyfin_not_wired"
            return self._finish(result, started)
        try:
            client.reload_config()
        except Exception:
            pass
        if not getattr(client, "host", None) or not getattr(client, "api_key", None):
            result["reason"] = "jellyfin_not_configured"
            return self._finish(result, started)

        if refresh:
            try:
                client.refresh_library()
            except Exception as exc:
                self.logger.warning("jellyfin refresh failed: %s", self.safe_exception_label(exc))

        deadline = time.time() + max(float(timeout or 0), 0.0)
        item = None
        while True:
            result["attempts"] += 1
            try:
                items = client.search_items(number, limit=5)
            except Exception as exc:
                self.logger.warning("jellyfin search failed for %s: %s", number, self.safe_exception_label(exc))
                items = []
            item = self._match_jellyfin_item(items, number)
            if item or time.time() >= deadline:
                break
            time.sleep(max(float(interval or 1.0), 0.5))

        if item:
            result["status"] = "visible"
            result["item_id"] = item.get("Id") or item.get("id")
            result["name"] = item.get("Name") or item.get("name")
            self._save_jellyfin_identity(movie_id, result["item_id"])
            self._record_event(
                movie_id=movie_id,
                event_type="jellyfin_visible",
                title=title,
                organize_status="completed",
                jellyfin_status="visible",
                source="jellyfin",
                reason=source,
                execution_result="success",
                payload={"number": number, "item_id": result["item_id"], "attempts": result["attempts"]},
            )
        else:
            result["status"] = "pending"
            result["reason"] = "jellyfin_not_visible"
            self._record_event(
                movie_id=movie_id,
                event_type="jellyfin_visible",
                title=title,
                organize_status="completed",
                jellyfin_status="pending",
                source="jellyfin",
                reason="jellyfin_not_visible",
                execution_result="recorded",
                payload={"number": number, "attempts": result["attempts"], "timeout": timeout},
            )
        return self._finish(result, started)

    @staticmethod
    def _match_jellyfin_item(items: List[Dict[str, Any]], number: str) -> Optional[Dict[str, Any]]:
        wanted = str(number or "").upper()
        if not wanted:
            return None
        for item in items or []:
            name = str(item.get("Name") or item.get("name") or "").upper()
            if wanted in name:
                return item
        return None

    def _lookup_jellyfin_item(self, number: str) -> Optional[Dict[str, Any]]:
        if not self.jellyfin_client or not number:
            return None
        try:
            items = self.jellyfin_client.search_items(number) or []
        except Exception as exc:
            self.logger.info(
                "jellyfin search failed for %s: %s", number, self.safe_exception_label(exc)
            )
            return None
        return self._match_jellyfin_item(items, number)

    def _save_amane_identity(self, movie_id: str, media: Dict[str, Any]) -> None:
        """回填 amane 身份，让重复的库藏品不用每次都打接口查。"""
        try:
            self.db.save_local_file_amane(
                movie_id,
                media_file_id=media.get("id"),
                file_path=media.get("path"),
                file_size=media.get("size") or media.get("file_size"),
            )
        except Exception as exc:
            self.logger.warning(
                "save amane identity failed for %s: %s", movie_id, self.safe_exception_label(exc)
            )

    def _save_jellyfin_identity(self, movie_id: str, item_id: Optional[str]) -> None:
        try:
            self.db.save_local_file_jellyfin(movie_id, item_id=item_id)
        except Exception as exc:
            self.logger.warning(
                "save jellyfin identity failed for %s: %s", movie_id, self.safe_exception_label(exc)
            )

    # ---------- 内部 ----------

    def _find_media(self, number: str) -> Optional[Dict[str, Any]]:
        result = self.amane_service.find_media(number, limit=5)
        items = result.get("items") or []
        if not items:
            return None
        wanted = str(number or "").upper()
        for item in items:
            if str(item.get("number") or "").upper() == wanted:
                return item
        return items[0]

    def _reread_media(self, media_id: Optional[int], number: str) -> Optional[Dict[str, Any]]:
        """按 id 回读最新状态（整理会改名改目录），拿不到再退回按番号搜。

        刻意用 id 而不是番号：amane 扫描 `_incoming` 时会把站点水印当成番号前缀
        （`4k2.mestart-646.mp4` → `MESTART-646`），番号一旦被改坏，按番号就再也搜不到，
        只能拿到旧快照，路径/状态全是整理前的。
        """
        if media_id is None:
            return self._find_media(number)
        try:
            latest = self.amane_service.get_client().get_media(int(media_id))
        except (AmaneError, ValueError, TypeError):
            latest = {}
        if latest.get("id") is not None:
            return latest
        return self._find_media(number)

    def _ensure_media_number(self, media: Dict[str, Any], number: str) -> Dict[str, Any]:
        """把 amane 猜错的番号改回来。

        amane 按原始文件名猜番号，下载站的水印会被当成番号的一部分：
        `4k2.mestart-646.mp4` → `MESTART-646`（真番号 START-646），
        另有两条不同片子被写成同一个 `COM-956998`。organize 出的路径是对的，
        错的只有 number 列——而 BusTerminal 全靠它对齐片库，错了整条链就断了。
        改号失败不致命，不打断入库。
        """
        stored = str(media.get("number") or "").strip()
        if not number or stored.upper() == str(number).upper():
            return media
        media_id = media.get("id")
        if media_id is None:
            return media
        try:
            self.amane_service.get_client().patch_media(int(media_id), number=str(number).upper())
        except (AmaneError, ValueError, TypeError) as exc:
            self.logger.warning(
                "amane patch number failed for media %s: %s",
                media_id, self.safe_exception_label(exc),
            )
            return media
        self.logger.info(
            "amane number corrected for media %s: %s -> %s", media_id, stored, number,
        )
        corrected = dict(media)
        corrected["number"] = str(number).upper()
        return corrected

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
    def _task_outcome(task: Dict[str, Any]) -> str:
        """把任务快照归成 done / failed / timeout / running 四类。

        关键：轮询超时时快照 status 往往还停在 running/queued，只看 status 会把
        一次超时当成"没失败"继续往下走。`wait_timeout` 为真即判为 timeout，交给
        调用方按失败处置（超时说明 amane 还在跑，重试比当成功更安全）。
        """
        status = str(task.get("status") or "").lower()
        if status == "failed":
            return "failed"
        if status == "done":
            return "done"
        if task.get("wait_timeout"):
            return "timeout"
        return "running"

    @staticmethod
    def _task_error(task: Dict[str, Any], outcome: str, default: str) -> AmaneError:
        """按类别构造可读报错：超时用 timeout，真失败用任务 error 或阶段默认名。"""
        if outcome == "timeout":
            return AmaneTaskTimeout()
        return AmaneError(str(task.get("error") or default))

    def _fail(
        self,
        trace: Dict[str, Any],
        started: float,
        movie_id: str,
        title: Optional[str],
        exc: Exception,
        *,
        stage: str,
    ) -> Dict[str, Any]:
        label = self.safe_exception_label(exc)
        message = getattr(exc, "code", None) or label
        trace["organize_status"] = "failed"
        trace["error"] = {"stage": stage, "code": message, "message": str(exc)[:200]}
        self.logger.warning("amane ingest failed at %s for %s: %s", stage, movie_id, label)
        self._record_event(
            movie_id=movie_id,
            event_type="amane_ingest",
            title=title,
            organize_status="failed",
            source="amane",
            # last_reason 是列表页唯一能看到的原因字段：带上具体报错，
            # 只写阶段名的话运维只看得到 "failed + refresh"，不知道为什么
            reason=f"{stage}: {message}",
            execution_result="failed",
            error_summary=str(exc)[:240],
            payload={"stage": stage, "stages": trace["stages"]},
        )
        return self._finish(trace, started)

    @staticmethod
    def _finish(trace: Dict[str, Any], started: float) -> Dict[str, Any]:
        trace["elapsed"] = round(time.time() - started, 2)
        return trace

    def ingest_many(
        self,
        items: List[Dict[str, Any]],
        *,
        wait: bool = True,
        source: str = "batch",
    ) -> Dict[str, Any]:
        """批量入库（调试/补跑用）。串行执行，amane worker 队列本来就单线程。"""
        results = []
        for item in items:
            results.append(
                self.ingest(
                    item.get("movie_id"),
                    number=item.get("number"),
                    title=item.get("title"),
                    wait=wait,
                    force=bool(item.get("force")),
                    source=source,
                )
            )
        return {
            "total": len(results),
            "completed": len([r for r in results if r.get("organize_status") == "completed"]),
            "failed": len([r for r in results if r.get("organize_status") == "failed"]),
            "skipped": len([r for r in results if r.get("organize_status") == "skipped"]),
            "not_found": len([r for r in results if r.get("organize_status") == "not_found"]),
            "results": results,
        }
