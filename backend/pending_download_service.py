import time
from typing import Any, Dict, Iterable, List, Optional


# 普通重试退避：30min → 2h → 6h → 24h（封顶）
PENDING_DOWNLOAD_RETRY_DELAYS = (
    30 * 60,
    2 * 60 * 60,
    6 * 60 * 60,
    24 * 60 * 60,
)
# 「源站确实没有磁力」的退避：24h → 3d → 7d → 14d（封顶）
#
# 为什么单独一套：源站上没有磁力是**事实**，不是"暂时抓不到"。原逻辑每天
# 去问站点一次（1437 条 × 每天 = 每天 1437 次无效请求），纯属给源站添堵。
# 拉长到 3 天起步、14 天封顶后，对源站的请求降到原来的 1/14。
PENDING_NO_MAGNET_RETRY_DELAYS = (
    24 * 60 * 60,
    3 * 24 * 60 * 60,
    7 * 24 * 60 * 60,
    14 * 24 * 60 * 60,
)
# 「源站没磁力」最多重试几次后彻底不再自动打源站。
# 14 天一次 × 6 次 ≈ 两个半月还找不到，就认为这片子源站上真没有磁力。
PENDING_NO_MAGNET_MAX_ATTEMPTS = 6
PENDING_DOWNLOAD_SCAN_INTERVAL_SECONDS = 10 * 60

# note 里出现这些片段就归类为「源站没有磁力」（事实性缺失）
_NO_MAGNET_MARKERS = (
    "no magnet on source",
    "no suitable magnet",
    "movie_not_found",
)


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


def is_no_magnet_item(item: Dict[str, Any]) -> bool:
    """这条是不是「源站上确实没有磁力」（而不是网络/解析之类的临时故障）。"""
    note = str((item or {}).get("note") or "").lower()
    return any(marker in note for marker in _NO_MAGNET_MARKERS)


def get_pending_retry_delay_seconds(retry_count: Any, *, no_magnet: bool = False) -> int:
    count = max(0, _safe_int(retry_count, 0))
    delays = PENDING_NO_MAGNET_RETRY_DELAYS if no_magnet else PENDING_DOWNLOAD_RETRY_DELAYS
    index = min(count, len(delays) - 1)
    return delays[index]


def is_pending_exhausted(item: Dict[str, Any]) -> bool:
    """「源站没磁力」重试够多次 → 不再自动打源站，等源站新增磁力或人工介入。"""
    if not is_no_magnet_item(item):
        return False
    return _safe_int((item or {}).get("retry_count"), 0) >= PENDING_NO_MAGNET_MAX_ATTEMPTS


def get_pending_next_retry_at(item: Dict[str, Any]) -> float:
    last_checked_at = _safe_float(item.get("last_checked_at"), 0)
    added_at = _safe_float(item.get("added_at"), 0)
    baseline = last_checked_at or added_at or time.time()
    delay = get_pending_retry_delay_seconds(
        item.get("retry_count"), no_magnet=is_no_magnet_item(item)
    )
    return baseline + delay


def is_pending_download_due(item: Dict[str, Any], now: Optional[float] = None) -> bool:
    if str(item.get("status") or "pending").lower() != "pending":
        return False
    if is_pending_exhausted(item):
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
    exhausted_count = 0
    for item in rows:
        if is_pending_exhausted(item):
            exhausted_count += 1
            continue
        next_at = get_pending_next_retry_at(item)
        if next_at <= current:
            due_count += 1
        if next_retry_at is None or next_at < next_retry_at:
            next_retry_at = next_at
    return {
        "pending_count": len(rows),
        "due_count": due_count,
        "exhausted_count": exhausted_count,
        "next_retry_at": next_retry_at,
        "retry_delays_seconds": list(PENDING_DOWNLOAD_RETRY_DELAYS),
        "no_magnet_retry_delays_seconds": list(PENDING_NO_MAGNET_RETRY_DELAYS),
        "no_magnet_max_attempts": PENDING_NO_MAGNET_MAX_ATTEMPTS,
        "scan_interval_seconds": PENDING_DOWNLOAD_SCAN_INTERVAL_SECONDS,
    }
