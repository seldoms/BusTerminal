"""翻译链路调用明细：入队 / 发送 / 返回 / 写库，供设置页「调用明细」面板查看。

刻意不写主日志：容器日志是 50MB×5，一次 10 条标题的请求体加原始响应就有几 KB，
高频翻译会把历史日志轮转冲掉（2026-11-07 已有过 44 小时日志被冲掉的故障）。
这里用独立的内存环形缓冲，只留最近 200 条，重启即清空 —— 定位是实时排查，不是审计。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Dict, Iterable, List, Optional

MAX_ENTRIES = 200
MAX_TEXT_LENGTH = 4000

_lock = threading.Lock()
_entries: deque = deque(maxlen=MAX_ENTRIES)
_sequence = 0


def _clip(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) <= MAX_TEXT_LENGTH:
        return text
    return text[:MAX_TEXT_LENGTH] + f" …[已截断，共 {len(text)} 字]"


def record(
    stage: str,
    *,
    source: str = "",
    model: str = "",
    request: Any = None,
    response: Any = None,
    written: Optional[Iterable[Dict[str, str]]] = None,
    duration_ms: Optional[int] = None,
    note: str = "",
) -> Dict[str, Any]:
    """追加一条明细。字段按场景可选，面板按存在的字段渲染。"""
    global _sequence
    entry: Dict[str, Any] = {
        "ts": time.time(),
        "stage": stage,
        "source": source,
        "model": model,
        "request": _clip(request),
        "response": _clip(response),
        "written": [
            {k: _clip(v) for k, v in item.items()} for item in (written or [])
        ],
        "duration_ms": duration_ms,
        "note": _clip(note),
    }
    with _lock:
        _sequence += 1
        entry["id"] = _sequence
        _entries.append(entry)
    return entry


def recent(limit: int = 50) -> List[Dict[str, Any]]:
    """最新在前；limit 会被夹在 1..MAX_ENTRIES。"""
    try:
        count = max(1, min(int(limit), MAX_ENTRIES))
    except (TypeError, ValueError):
        count = 50
    with _lock:
        items = list(_entries)
    return items[-count:][::-1]


def clear() -> None:
    with _lock:
        _entries.clear()
