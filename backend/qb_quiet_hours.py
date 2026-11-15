"""半夜静默模式：在用户指定的多个时间段内暂停 qBittorrent 的上传/下载。

为什么不用 qB 自带的「备用速度限制调度」：它只能配**一个**时间段（scheduler
一样只有 from/to 一组小时），而这个需求是「多个时间段」，且要的是暂停而不是降速。
所以窗口判定放在后端，到点调 /api/v2/torrents/pause|resume。

几条硬约束（都是踩出来的）：

  · **只恢复自己暂停的种子**。暂停前先快照 hash 集合存进 config，退出窗口时只
    resume 这批；否则会把用户自己手动暂停的种子一起放出来。
  · **用户手动恢复的不再强按回去**。窗口内用户 resume 了某个种子（`released`），
    后续 tick 不会再暂停它，也不再算作待恢复 —— 半夜被系统反复按回去是最烦的。
  · **新进队列的种子也要暂停**。窗口内手动/自动下发的种子会立刻开始拉数据，
    不处理的话静默模式等于没用；它们会被加入暂停集，并在退出时一起恢复。
  · **状态落库而不是只放内存**。后端重启（NAS 上发布/重建容器很常见）后如果
    丢了暂停集，这些种子会一直停在暂停状态，直到有人手动恢复。

窗口语义：`days` 指的是**开始日**的 ISO 星期（周一=1…周日=7）。`end` 小于
`start` 表示跨零点（23:30 → 07:00）；`end == start` 视为无效窗口直接丢弃。
时间区间是左闭右开 [start, end)。

`all_day=True` 的窗口是「整天」：强制 00:00 → 次日 00:00，忽略 start/end。
这是「周末全天不开 qB」这类策略用的，不能用 00:00→23:59 近似 —— 中间那一
分钟会让执行器先 resume 全部种子再 pause 一次，比不静默还吵。

时区：容器里已经注入了 TZ=Asia/Shanghai，但判定不能依赖部署环境。这里统一
用 Asia/Shanghai 的墙钟做窗口运算；单测注入的 naive datetime 也按北京墙钟解释。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

CONFIG_KEY = "qb_quiet_hours"
MAX_WINDOWS = 8
MIN_WINDOW_MINUTES = 10
ALL_DAY_MINUTES = 24 * 60

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 精简镜像可能没有 tzdata；中国无夏令时，固定 +8 等价
    LOCAL_TZ = timezone(timedelta(hours=8))


def local_now() -> datetime:
    """北京时间（aware）。"""
    return datetime.now(LOCAL_TZ)


def _wall_clock(now: datetime) -> datetime:
    """把任意 datetime 归一成「北京墙钟的 naive datetime」，供窗口运算使用。

    aware → 换算到 +08:00 后去掉时区；naive → 原样（按北京墙钟解释）。
    """
    if not isinstance(now, datetime):
        return local_now().replace(tzinfo=None)
    if now.tzinfo is None:
        return now
    return now.astimezone(LOCAL_TZ).replace(tzinfo=None)

# qB 里这些状态本来就不在传输，暂停它们没有意义，也不该被算进「本次暂停集」
IDLE_STATES = {
    "pauseddl", "pausedup", "stoppeddl", "stoppedup",
    "checkingdl", "checkingup", "checkingresumedata",
    "queueddl", "queuedup",
    "error", "missingfiles", "unknown",
    "moving", "allocating",
}

DEFAULT_WINDOW = {"start": "23:30", "end": "07:00", "days": [1, 2, 3, 4, 5, 6, 7]}


# --------------------------------------------------------------------------- #
# 纯函数：时间窗口判定（不碰 qB，便于单测）
# --------------------------------------------------------------------------- #
def parse_hhmm(value: Any) -> Optional[int]:
    """'23:30' -> 1410（当日分钟数）；非法返回 None。"""
    text = str(value or "").strip()
    if not text or ":" not in text:
        return None
    head, _, tail = text.partition(":")
    try:
        hour = int(head.strip())
        minute = int(tail.strip())
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def format_hhmm(minutes: int) -> str:
    minutes = int(minutes) % (24 * 60)
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def normalize_days(raw: Any) -> List[int]:
    """ISO 星期 1..7，去重排序；空值视为「每天」。"""
    if raw is None:
        return [1, 2, 3, 4, 5, 6, 7]
    if isinstance(raw, (str, bytes)):
        items: Iterable[Any] = str(raw).replace("，", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        items = [raw]
    days = set()
    for item in items:
        try:
            day = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if 1 <= day <= 7:
            days.add(day)
    return sorted(days) or [1, 2, 3, 4, 5, 6, 7]


def normalize_windows(raw: Any) -> List[Dict[str, Any]]:
    """校验并归一化窗口列表。非法条目静默丢弃，不因一个坏窗口整体拒绝保存。

    `all_day` 窗口不带 all_day 键的旧数据一律按普通窗口处理，格式完全向后兼容：
    只有显式声明全天的条目才会多出 `all_day: True`，老断言/老前端都不受影响。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    windows: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("all_day"):
            windows.append({
                "start": "00:00",
                "end": "00:00",
                "days": normalize_days(item.get("days")),
                "all_day": True,
            })
            if len(windows) >= MAX_WINDOWS:
                break
            continue
        start = parse_hhmm(item.get("start"))
        end = parse_hhmm(item.get("end"))
        if start is None or end is None or start == end:
            continue
        windows.append({
            "start": format_hhmm(start),
            "end": format_hhmm(end),
            "days": normalize_days(item.get("days")),
        })
        if len(windows) >= MAX_WINDOWS:
            break
    return windows


def window_span_minutes(window: Dict[str, Any]) -> int:
    if window.get("all_day"):
        return ALL_DAY_MINUTES
    start = parse_hhmm(window.get("start"))
    end = parse_hhmm(window.get("end"))
    if start is None or end is None:
        return 0
    return (end - start) % ALL_DAY_MINUTES or ALL_DAY_MINUTES


def _occurrences(
    window: Dict[str, Any], now: datetime, max_offset: int = 2
) -> List[Tuple[datetime, datetime]]:
    """返回「可能覆盖 now」与「紧接着的下一次」两组区间（北京墙钟）。"""
    all_day = bool(window.get("all_day"))
    start = parse_hhmm(window.get("start"))
    end = parse_hhmm(window.get("end"))
    if not all_day and (start is None or end is None or start == end):
        return []
    days = set(window.get("days") or [1, 2, 3, 4, 5, 6, 7])
    spans: List[Tuple[datetime, datetime]] = []
    # 覆盖 now 的窗口只可能起于今天或昨天（跨零点最多跨一天）；
    # next_change 会把范围放大到 9 天，用来跨过连续多天的全天窗口。
    for offset in range(-1, max_offset + 1):
        day = (now + timedelta(days=offset)).date()
        if day.isoweekday() not in days:
            continue
        begin = datetime.combine(day, datetime.min.time()) + timedelta(
            minutes=0 if all_day else int(start or 0)
        )
        length = ALL_DAY_MINUTES if all_day else (int(end or 0) - int(start or 0)) % ALL_DAY_MINUTES
        spans.append((begin, begin + timedelta(minutes=length)))
    return spans


def is_active(now: datetime, windows: List[Dict[str, Any]]) -> bool:
    now = _wall_clock(now)
    for window in windows or []:
        for begin, finish in _occurrences(window, now):
            if begin <= now < finish:
                return True
    return False


def next_change(now: datetime, windows: List[Dict[str, Any]]) -> Optional[datetime]:
    """下一次状态翻转的时刻：active 时取最近一个 end，否则取最近一个 start。

    连续窗口（周六全天 + 周日全天）在周日 00:00 会有一个「窗口 end 但仍在静默里」
    的假边界，直接取它会在状态栏显示「00:00 自动恢复」而实际根本不会恢复。
    所以 end 候选必须再过一次 is_active，只保留真正退出静默的那个时刻。
    """
    now = _wall_clock(now)
    spans: List[Tuple[datetime, datetime]] = []
    for window in windows or []:
        spans.extend(_occurrences(window, now, max_offset=9))
    active = any(begin <= now < finish for begin, finish in spans)
    if active:
        ends = sorted({
            finish for _, finish in spans
            if finish > now and not is_active(finish, windows)
        })
        return ends[0] if ends else None
    starts = sorted({begin for begin, _ in spans if begin > now})
    return starts[0] if starts else None


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #
def _empty_config() -> Dict[str, Any]:
    return {"enabled": False, "windows": [], "paused": [], "released": [],
            "last_change": None}


def load_config(db: Any) -> Dict[str, Any]:
    raw = None
    try:
        raw = db.get_config(CONFIG_KEY)
    except Exception as exc:  # 读配置失败不该拖垮循环
        logger.warning("quiet hours config read failed: %s", type(exc).__name__)
    if isinstance(raw, str) and raw.strip():
        # 正常情况下 get_config 已经 json.loads 过了，这里只兜底历史遗留的字符串写法
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    data = dict(_empty_config())
    if isinstance(raw, dict):
        data["enabled"] = bool(raw.get("enabled"))
        data["windows"] = normalize_windows(raw.get("windows"))
        data["paused"] = [str(h) for h in (raw.get("paused") or []) if h]
        data["released"] = [str(h) for h in (raw.get("released") or []) if h]
        data["last_change"] = raw.get("last_change")
    return data


def save_config(db: Any, data: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "enabled": bool(data.get("enabled")),
        "windows": normalize_windows(data.get("windows")),
        "paused": [str(h) for h in (data.get("paused") or []) if h],
        "released": [str(h) for h in (data.get("released") or []) if h],
        "last_change": data.get("last_change"),
    }
    db.set_config(CONFIG_KEY, payload)
    return payload


def public_view(data: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """给前端的状态视图：不暴露 hash 列表本身，只给数量。"""
    now = _wall_clock(now or local_now())
    windows = data.get("windows") or []
    active = bool(data.get("enabled")) and is_active(now, windows)
    upcoming = next_change(now, windows) if data.get("enabled") else None
    return {
        "enabled": bool(data.get("enabled")),
        "windows": windows,
        "max_windows": MAX_WINDOWS,
        "min_window_minutes": MIN_WINDOW_MINUTES,
        "state": {
            "active": active,
            "paused_count": len(data.get("paused") or []),
            "released_count": len(data.get("released") or []),
            "now": now.isoformat(timespec="seconds"),
            "next_change_at": upcoming.isoformat(timespec="seconds") if upcoming else None,
            "next_change_action": (
                None if upcoming is None else ("resume" if active else "pause")
            ),
            "last_change": data.get("last_change"),
            "timezone": "Asia/Shanghai",
        },
    }


def is_active_now(db: Any, now: Optional[datetime] = None) -> bool:
    """给下载闸门用的轻量查询：此刻是否处于静默窗口内。

    一次 `get_config`（带缓存）即可，不碰 qB。读配置/判定出错一律返回 False
    —— 闸门坏了宁可少挡一次下载，也不能把整条下载链路卡死。
    """
    try:
        data = load_config(db)
        if not data.get("enabled"):
            return False
        return is_active(_wall_clock(now or local_now()), data.get("windows") or [])
    except Exception as exc:  # pragma: no cover - 防御性
        logger.warning("quiet hours gate read failed: %s", type(exc).__name__)
        return False


# --------------------------------------------------------------------------- #
# 执行器
# --------------------------------------------------------------------------- #
class QuietHoursController:
    """每个 tick 判一次「现在该不该静默」，只在状态翻转时动 qB。

    依赖全部注入，单测里可以塞假的 list/pause/resume，不碰真实 qB。
    """

    def __init__(
        self,
        *,
        db: Any,
        list_torrents: Callable[[], List[Dict[str, Any]]],
        pause: Callable[[List[str]], bool],
        resume: Callable[[List[str]], bool],
        clock: Callable[[], datetime] = local_now,
    ):
        self.db = db
        self.list_torrents = list_torrents
        self.pause = pause
        self.resume = resume
        self.clock = clock
        self._lock = threading.Lock()

    # -- 内部 -------------------------------------------------------------- #
    @staticmethod
    def _ongoing(torrents: List[Dict[str, Any]]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for item in torrents or []:
            if not isinstance(item, dict):
                continue
            digest = str(item.get("hash") or "").strip()
            if not digest:
                continue
            state = str(item.get("state") or "").strip().lower()
            if state in IDLE_STATES:
                continue
            out[digest] = state
        return out

    def tick(self) -> Dict[str, Any]:
        """跑一轮。返回本轮动作摘要（便于日志与单测断言）。"""
        with self._lock:
            data = load_config(self.db)
            now = _wall_clock(self.clock())
            windows = data.get("windows") or []
            enabled = bool(data.get("enabled"))
            should_pause = enabled and is_active(now, windows)

            paused: List[str] = list(data.get("paused") or [])
            released: List[str] = list(data.get("released") or [])
            actions: List[str] = []
            errors: List[str] = []

            if should_pause:
                try:
                    torrents = self.list_torrents() or []
                except Exception as exc:
                    return {"ok": False, "error": "list_failed", "detail": type(exc).__name__,
                            "active": True}

                ongoing = self._ongoing(torrents)
                pause_set = set(paused)
                released_set = set(released)

                # 用户在自己动：我们暂停过、现在又跑起来了 = 他手动恢复了。
                # 从暂停集里摘出去并记住，本窗口内不再按回去，也不再算作待恢复。
                for digest in list(pause_set):
                    if digest in ongoing:
                        pause_set.discard(digest)
                        released_set.add(digest)
                        actions.append("release:" + digest[:8])

                # 窗口内新出现的活跃种子（手动/自动下发）同样要静默
                newcomers = [
                    digest for digest in ongoing
                    if digest not in pause_set and digest not in released_set
                ]
                if newcomers:
                    try:
                        if self.pause(newcomers):
                            pause_set.update(newcomers)
                            actions.append("pause:%d" % len(newcomers))
                        else:
                            errors.append("pause_failed")
                    except Exception as exc:
                        errors.append("pause_exc:" + type(exc).__name__)

                data["paused"] = sorted(pause_set)
                data["released"] = sorted(released_set)
                if actions or errors:
                    data["last_change"] = now.isoformat(timespec="seconds")
                    save_config(self.db, data)
                elif pause_set != set(paused) or released_set != set(released):
                    data["last_change"] = now.isoformat(timespec="seconds")
                    save_config(self.db, data)
                return {"ok": not errors, "active": True, "actions": actions,
                        "errors": errors, "paused": len(pause_set)}

            # 不在窗口内（或功能被关掉）：把还在暂停的恢复回去
            if paused:
                try:
                    torrents = self.list_torrents() or []
                    alive = {
                        str(item.get("hash") or "").strip()
                        for item in torrents if isinstance(item, dict)
                    }
                    # 已删除的种子不必再 resume，否则 qB 会返回失败
                    targets = [digest for digest in paused if digest in alive]
                except Exception:
                    targets = list(paused)

                resumed_ok = False
                if targets:
                    try:
                        resumed_ok = bool(self.resume(targets))
                    except Exception as exc:
                        errors.append("resume_exc:" + type(exc).__name__)
                    if resumed_ok:
                        actions.append("resume:%d" % len(targets))
                    else:
                        errors.append("resume_failed")
                # 恢复失败时保留暂停集，下个 tick 再试；成功则清空
                data["paused"] = [] if (resumed_ok or not targets) else paused
            # released 只在「本窗口内」有效：paused 已被 emergency_resume 清空的
            # 情况下也必须落一次库把它抹掉，否则放行名单会残留到下周，那些种子
            # 下个周末再也不会被暂停。
            if paused or released:
                data["released"] = []
                data["last_change"] = now.isoformat(timespec="seconds")
                save_config(self.db, data)

            return {"ok": not errors, "active": False, "actions": actions,
                    "errors": errors, "paused": len(data.get("paused") or [])}

    def emergency_resume(self) -> Dict[str, Any]:
        """一键解除：把本功能暂停的种子全部恢复，并在**本窗口内**不再按回去。

        必须把恢复过的 hash 记进 released：否则下一轮 tick 仍落在静默窗口里，
        会把它当成「用户手动恢复的新活跃种子」再暂停一次 —— 周末想临时放行
        点了这个按钮，30 秒后又全停回去，等于按钮没用。released 在退出窗口
        时会清空，所以语义正好是「放行到本窗口结束」。
        """
        with self._lock:
            data = load_config(self.db)
            paused = list(data.get("paused") or [])
            ok = True
            if paused:
                try:
                    ok = bool(self.resume(paused))
                except Exception as exc:
                    return {"ok": False, "error": type(exc).__name__}
            data["paused"] = []
            data["released"] = sorted(set(data.get("released") or []) | set(paused))
            data["last_change"] = _wall_clock(self.clock()).isoformat(timespec="seconds")
            save_config(self.db, data)
            return {"ok": ok, "resumed": len(paused)}
