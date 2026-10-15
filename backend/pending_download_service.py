import time
from typing import Any, Dict, Iterable, List, Optional


PENDING_DOWNLOAD_RETRY_DELAYS = (
    30 * 60,
    2 * 60 * 60,
    6 * 60 * 60,
    24 * 60 * 60,
)
PENDING_DOWNLOAD_SCAN_INTERVAL_SECONDS = 10 * 60


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_pending_retry_delay_seconds(retry_count: Any) -> int:
    count = max(0, _safe_int(retry_count, 0))
    index = min(count, len(PENDING_DOWNLOAD_RETRY_DELAYS) - 1)
    return PENDING_DOWNLOAD_RETRY_DELAYS[index]


def get_pending_next_retry_at(item: Dict[str, Any]) -> float:
    last_checked_at = _safe_float(item.get("last_checked_at"), 0)
    added_at = _safe_float(item.get("added_at"), 0)
    baseline = last_checked_at or added_at or time.time()
    return baseline + get_pending_retry_delay_seconds(item.get("retry_count"))


def is_pending_download_due(item: Dict[str, Any], now: Optional[float] = None) -> bool:
    if str(item.get("status") or "pending").lower() != "pending":
        return False
    return get_pending_next_retry_at(item) <= (now if now is not None else time.time())


def select_due_pending_downloads(
    items: Iterable[Dict[str, Any]],
    *,
    limit: int = 50,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    current = now if now is not None else time.time()
    due = [item for item in items if is_pending_download_due(item, current)]
    due.sort(key=lambda item: (get_pending_next_retry_at(item), _safe_float(item.get("added_at"), 0)))
    return due[:limit]


def summarize_pending_downloads(items: Iterable[Dict[str, Any]], now: Optional[float] = None) -> Dict[str, Any]:
    current = now if now is not None else time.time()
    rows = list(items)
    due_count = 0
    next_retry_at = None
    for item in rows:
        next_at = get_pending_next_retry_at(item)
        if next_at <= current:
            due_count += 1
        if next_retry_at is None or next_at < next_retry_at:
            next_retry_at = next_at
    return {
        "pending_count": len(rows),
        "due_count": due_count,
        "next_retry_at": next_retry_at,
        "retry_delays_seconds": list(PENDING_DOWNLOAD_RETRY_DELAYS),
        "scan_interval_seconds": PENDING_DOWNLOAD_SCAN_INTERVAL_SECONDS,
    }
