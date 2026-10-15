from datetime import datetime
import re
import threading
from typing import Any, Callable, Dict, Optional, Tuple


class TaskRegistry:
    """In-memory registry for long-running background tasks."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def register(
        self,
        name: str,
        *,
        description: str,
        category: str,
        interval: Optional[str] = None,
    ) -> None:
        with self._lock:
            existing = self._tasks.get(name, {})
            self._tasks[name] = {
                **existing,
                "name": name,
                "description": description,
                "category": category,
                "interval": interval,
                "status": existing.get("status", "registered"),
                "registered_at": existing.get("registered_at") or self._now(),
                "started_at": existing.get("started_at"),
                "stopped_at": existing.get("stopped_at"),
                "last_heartbeat_at": existing.get("last_heartbeat_at"),
                "last_success_at": existing.get("last_success_at"),
                "last_error_at": existing.get("last_error_at"),
                "last_error": existing.get("last_error"),
                "first_error_at": existing.get("first_error_at"),
                "consecutive_error_count": existing.get("consecutive_error_count", 0),
                "total_error_count": existing.get("total_error_count", 0),
                "last_recovery": existing.get("last_recovery"),
                "thread_name": existing.get("thread_name"),
                "thread_id": existing.get("thread_id"),
                "restart_count": existing.get("restart_count", 0),
            }

    def run_task(self, name: str, target: Callable[..., Any], args: Tuple[Any, ...] = ()) -> None:
        self.mark_started(name)
        try:
            target(*args)
            self.mark_success(name, stopped=True)
        except Exception as exc:
            self.mark_failure(name, exc)
            raise

    def mark_started(self, name: str) -> None:
        current = threading.current_thread()
        with self._lock:
            task = self._tasks.setdefault(name, {"name": name})
            task["status"] = "running"
            task["started_at"] = self._now()
            task["stopped_at"] = None
            task["last_heartbeat_at"] = self._now()
            task["thread_name"] = current.name
            task["thread_id"] = current.ident
            task["restart_count"] = int(task.get("restart_count") or 0) + 1

    def mark_success(self, name: str, *, stopped: bool = False, summary: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            task = self._tasks.setdefault(name, {"name": name})
            now = self._now()
            consecutive_errors = int(task.get("consecutive_error_count") or 0)
            if consecutive_errors > 0:
                task["last_recovery"] = {
                    "recovered_at": now,
                    "retry_count": consecutive_errors,
                    "first_error_at": task.get("first_error_at"),
                    "last_error_at": task.get("last_error_at"),
                    "last_error": task.get("last_error"),
                }
            task["last_success_at"] = now
            task["last_heartbeat_at"] = now
            task["last_error"] = None
            task["last_error_at"] = None
            task["first_error_at"] = None
            task["consecutive_error_count"] = 0
            if summary is not None:
                task["last_summary"] = summary
            if stopped:
                task["status"] = "stopped"
                task["stopped_at"] = self._now()

    def mark_error(
        self,
        name: str,
        exc: Exception,
        *,
        fatal: bool = False,
        summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            task = self._tasks.setdefault(name, {"name": name})
            now = self._now()
            if fatal:
                task["status"] = "failed"
            if not task.get("first_error_at"):
                task["first_error_at"] = now
            task["last_error_at"] = now
            task["last_error"] = {
                "type": exc.__class__.__name__,
                "message": self._safe_error_message(exc),
            }
            task["consecutive_error_count"] = int(task.get("consecutive_error_count") or 0) + 1
            task["total_error_count"] = int(task.get("total_error_count") or 0) + 1
            task["last_heartbeat_at"] = now
            if summary is not None:
                task["last_summary"] = summary
            if fatal:
                task["stopped_at"] = now

    def mark_failure(self, name: str, exc: Exception) -> None:
        self.mark_error(name, exc, fatal=True)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            tasks = [dict(task) for task in self._tasks.values()]
        tasks.sort(key=lambda item: (item.get("category") or "", item.get("name") or ""))
        return {
            "count": len(tasks),
            "running_count": sum(1 for task in tasks if task.get("status") == "running"),
            "failed_count": sum(1 for task in tasks if task.get("status") == "failed"),
            "tasks": tasks,
        }

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _safe_error_message(exc: Exception) -> str:
        message = str(exc or "").strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,119}", message):
            return message
        return exc.__class__.__name__


task_registry = TaskRegistry()
