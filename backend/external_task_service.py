import re
import threading
from typing import Any, Callable, Dict, Optional


TASK_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ExternalTaskService:
    def __init__(
        self,
        *,
        db: Any,
        logger: Any,
        safe_exception_label: Callable[[Exception], str],
    ):
        self.db = db
        self.logger = logger
        self.safe_exception_label = safe_exception_label
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        self._handler_lock = threading.Lock()

    def register_handler(
        self,
        task_type: str,
        handler: Callable[[Dict[str, Any]], Any],
    ) -> None:
        if not TASK_TYPE_PATTERN.fullmatch(str(task_type or "")):
            raise ValueError("invalid_external_task_type")
        if not callable(handler):
            raise ValueError("external_task_handler_required")
        with self._handler_lock:
            self._handlers[task_type] = handler

    def enqueue(
        self,
        task_type: str,
        dedupe_key: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        source: Optional[str] = None,
        max_retries: int = 20,
    ) -> Dict[str, Any]:
        if not TASK_TYPE_PATTERN.fullmatch(str(task_type or "")):
            raise ValueError("invalid_external_task_type")
        safe_key = str(dedupe_key or "").strip()
        if not safe_key or len(safe_key) > 160:
            raise ValueError("invalid_external_task_dedupe_key")
        if payload is not None and not isinstance(payload, dict):
            raise ValueError("invalid_external_task_payload")
        return self.db.enqueue_external_task(
            task_type,
            safe_key,
            payload or {},
            source,
            max_retries,
        )

    def run_due(self, task_type: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
        tasks = self.db.claim_due_external_tasks(task_type=task_type, limit=limit)
        summary = {
            "claimed": len(tasks),
            "completed": 0,
            "deferred": 0,
            "needs_attention": 0,
            "recovered": 0,
            "retry_count": 0,
        }

        with self._handler_lock:
            handlers = dict(self._handlers)

        for task in tasks:
            handler = handlers.get(task.get("task_type"))
            try:
                if not handler:
                    raise RuntimeError("external_task_handler_not_registered")
                result = handler(task.get("payload") or {})
                completed = self.db.complete_external_task(
                    task["id"],
                    result if isinstance(result, dict) else {"result": result},
                )
                retries = int((completed or task).get("retry_count") or 0)
                summary["completed"] += 1
                summary["retry_count"] += retries
                if retries > 0:
                    summary["recovered"] += 1
                    self.logger.info(
                        "External task recovered: type=%s retries=%s",
                        task.get("task_type"),
                        retries,
                    )
            except Exception as exc:
                error_label = self.safe_exception_label(exc)
                deferred = self.db.defer_external_task(task["id"], error_label)
                retry_count = int((deferred or {}).get("retry_count") or 0)
                summary["retry_count"] += 1
                if (deferred or {}).get("status") == "needs_attention":
                    summary["needs_attention"] += 1
                    self.logger.error(
                        "External task needs attention: type=%s retries=%s error=%s",
                        task.get("task_type"),
                        retry_count,
                        error_label,
                    )
                else:
                    summary["deferred"] += 1
                    if retry_count == 1:
                        self.logger.warning(
                            "External task deferred: type=%s error=%s",
                            task.get("task_type"),
                            error_label,
                        )

        return summary
