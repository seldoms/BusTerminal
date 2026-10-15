"""观看行为统计聚合

给「观看提醒邮件」提供数据：多久没看片、这期间下载了什么、播放进度如何、
按当前观看习惯看完片单还要多久。

邮件文案里的每个数字都必须来自这里，**不允许在模板里硬编** —— 编出来的图表
比没有图表更糟。取不到数据就明说取不到（`degraded=True`），不拿 0 冒充真实值。
"""
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# Jellyfin 的 LastPlayedDate / DateCreated 恒为 UTC，而「几天没看」是要按**日历日**算的。
# 若换算成本机时区，运行机器的 TZ 一变结果就跟着变；生产容器 TZ 是 Asia/Shanghai，
# 晚间播放（UTC 12:00 之后）换算过去会跨到次日，使「已 N 天」整体偏大一天。
# 这里固定按 UTC+8 换算，与部署环境一致，不受执行机器时区影响。
DISPLAY_TZ = timezone(timedelta(hours=8))

# 取不到足够样本时的兜底假设。写进结果里会随邮件一起告知用户，不静默使用。
FALLBACK_MINUTES_PER_ITEM = 5

# 播放时长兜底区间（分钟）。Jellyfin 不给单片时长时，只能用历史平均或经验值。
MIN_MINUTES_PER_ITEM = 1
MAX_MINUTES_PER_ITEM = 600

# 分页上限。Jellyfin 每页 500 条，按需翻页；设上限是为了别把邮件任务变成压测
# （十万级片单会翻两百多次）。命中上限时**必须**在结果里标注 truncated，
# 不能让邮件拿一个被截断的计数当成真值说 —— 那正是本次修掉的那类错误。
MAX_PAGINATE = 5000


def _parse_datetime(value: Any) -> Optional[datetime]:
    """解析 Jellyfin 的 ISO 时间串（结尾常带 Z），统一换算到 DISPLAY_TZ 后去掉时区。

    解析不了就返回 None —— 不猜时间，宁可标记数据缺失也不给错日期。
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(DISPLAY_TZ).replace(tzinfo=None)


def _play_time_of(item: Dict[str, Any]) -> Any:
    """取播放时间：兼容规范化字段与 Jellyfin 原始结构（UserData.LastPlayedDate）。"""
    direct = item.get("last_played")
    if direct:
        return direct
    user_data = item.get("UserData")
    if isinstance(user_data, dict):
        return user_data.get("LastPlayedDate")
    return None


def _play_count_of(item: Dict[str, Any]) -> int:
    direct = item.get("play_count")
    if direct is not None:
        try:
            return int(direct)
        except (TypeError, ValueError):
            return 0
    user_data = item.get("UserData")
    if isinstance(user_data, dict):
        try:
            return int(user_data.get("PlayCount") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _classify_kind(item: Dict[str, Any]) -> str:
    """粗分类用于饼图。拿不到可靠类型就归到「其他」，不硬猜。"""
    genres = item.get("genres") or []
    if isinstance(genres, str):
        genres = [genres]
    for genre in genres:
        text = str(genre).lower()
        if any(k in text for k in ("剧情", "drama", "短片", "short")):
            return "剧情 / 短片"
        if any(k in text for k in ("综艺", "variety", "vlog")):
            return "综艺 / Vlog"
        if any(k in text for k in ("动画", "animation", "anime")):
            return "动画"
    # genres 没带时退回按码长粗分，仅作为图表兜底
    title = str(item.get("title") or "")
    if any(ch.isdigit() for ch in title):
        return "其他"
    return "其他"


def _percentile(values: List[float], ratio: float) -> float:
    """取分位数。样本为空返回 0，避免除零。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * ratio))
    index = max(0, min(index, len(ordered) - 1))
    return ordered[index]


class InsightStatsService:
    def __init__(self, *, jellyfin_client: Any, db: Any, config: Any, logger: Any):
        self.jellyfin_client = jellyfin_client
        self.db = db
        self.config = config
        self.logger = logger

    # ------------------------------------------------------------------
    def collect(self, idle_days: int = 10, scan_limit: int = 500) -> Dict[str, Any]:
        """汇总观看与片单状况。

        idle_days 超过阈值时会附 idle_days=10 的离线区间统计。
        任何一路取数失败都写进 degraded_reasons，不抛异常 —— 邮件是通知，
        不该因为 Jellyfin 抖一下就整个发不出去。
        """
        degraded: List[str] = []

        watched, watched_error = self._fetch_watched()
        if watched_error and watched_error != "truncated":
            degraded.append(f"watched:{watched_error}")

        unplayed, unplayed_error = self._fetch_unplayed()
        if unplayed_error and unplayed_error != "truncated":
            degraded.append(f"unplayed:{unplayed_error}")

        # truncated 是「拿到了但不完整」，与「取数失败」是两回事，
        # 分开表达才不会在邮件里混成一句"数据不可用"。
        truncated = (watched_error == "truncated") or (unplayed_error == "truncated")

        watched_last_at = self._latest_play_time(watched)
        idle_days_actual = self._idle_days(watched_last_at)

        minutes_per_item, pace_note = self._minutes_per_item(watched)
        unwatched_count = len(unplayed)
        eta = self._eta(unwatched_count, minutes_per_item)

        return {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "idle_days_actual": idle_days_actual,
            "idle_days_threshold": idle_days,
            "should_notify": idle_days_actual >= idle_days,
            "last_played_at": watched_last_at.strftime("%Y-%m-%d") if watched_last_at else None,
            "watched_total": len(watched),
            "unwatched_total": unwatched_count,
            "truncated": truncated,
            "minutes_per_item": round(minutes_per_item, 1),
            "pace_note": pace_note,
            "eta_human": eta["human"],
            "eta_minutes": eta["minutes"],
            "kind_breakdown": self._kind_breakdown(unplayed),
            "newest_unwatched": self._newest_titles(unplayed, 5),
            "play_count_top": self._top_played(watched, 5),
            "degraded": bool(degraded),
            "degraded_reasons": degraded,
        }

    # ------------------------------------------------------------------
    def _fetch_watched(self) -> tuple:
        """取已观看条目。

        必须走 get_library_items（会规范化出 last_played / play_count），
        **不能**用 get_watched_movies —— 后者返回的是 Jellyfin 原始结构，
        播放时间藏在 UserData.LastPlayedDate 里，字段名还对不上，拿不到就是 None。
        """
        try:
            result = self.jellyfin_client.get_library_items(is_played=True, limit=500)
            items = list(result.get("items") or [])
            total = int(result.get("total") or len(items))
            # 超过一页时继续翻，否则「已看 N 部」会被截断在 500
            offset = len(items)
            while total > offset and offset < MAX_PAGINATE:
                page = self.jellyfin_client.get_library_items(is_played=True, limit=500, offset=offset)
                batch = list(page.get("items") or [])
                if not batch:
                    break
                items.extend(batch)
                offset += len(batch)
            return items, ("truncated" if total > offset else None)
        except Exception as exc:
            self.logger.warning("Insight: watched fetch failed: %s", type(exc).__name__)
            return [], type(exc).__name__

    def _fetch_unplayed(self) -> tuple:
        """取未观看条目，同样翻页取全量。

        只取 500 会把 2888 报成 500，「按习惯还需多久」会算错一个数量级。
        """
        try:
            result = self.jellyfin_client.get_library_items(is_played=False, limit=500)
            items = list(result.get("items") or [])
            total = int(result.get("total") or len(items))
            offset = len(items)
            while total > offset and offset < MAX_PAGINATE:
                page = self.jellyfin_client.get_library_items(is_played=False, limit=500, offset=offset)
                batch = list(page.get("items") or [])
                if not batch:
                    break
                items.extend(batch)
                offset += len(batch)
            return items, ("truncated" if total > offset else None)
        except Exception as exc:
            self.logger.warning("Insight: unplayed fetch failed: %s", type(exc).__name__)
            return [], type(exc).__name__

    def _latest_play_time(self, watched: List[Dict[str, Any]]) -> Optional[datetime]:
        latest: Optional[datetime] = None
        for item in watched:
            parsed = _parse_datetime(_play_time_of(item))
            if parsed and (latest is None or parsed > latest):
                latest = parsed
        return latest

    def _idle_days(self, last_played: Optional[datetime]) -> int:
        if last_played is None:
            # 从没看过：不要谎报天数，交给调用方按「无记录」表达
            return 0
        return max((datetime.now() - last_played).days, 0)

    def _minutes_per_item(self, watched: List[Dict[str, Any]]) -> tuple:
        """估「看一部要多久」。

        没有单片时长数据时，退回「每部按 5 分钟」的明确假设，
        并把假设本身写进 pace_note 随邮件告知 —— 不让用户误以为是实测值。
        """
        return FALLBACK_MINUTES_PER_ITEM, (
            f"按每部 {FALLBACK_MINUTES_PER_ITEM} 分钟估算"
            f"（尚无单片时长统计，{FALLBACK_MINUTES_PER_ITEM} 分钟为经验假设）"
        )

    def _eta(self, unwatched: int, minutes_per_item: float) -> Dict[str, Any]:
        minutes = unwatched * minutes_per_item
        if minutes <= 0:
            return {"minutes": 0, "human": "片单已清空"}
        hours = minutes / 60
        days = hours / 24
        if days >= 365:
            human = f"{days / 365:.1f} 年"
        elif days >= 1:
            human = f"{days:.1f} 天"
        else:
            human = f"{hours:.1f} 小时"
        return {"minutes": int(minutes), "human": human}

    def _kind_breakdown(self, unplayed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        counts: Dict[str, int] = {}
        for item in unplayed:
            kind = _classify_kind(item)
            counts[kind] = counts.get(kind, 0) + 1
        total = sum(counts.values()) or 1
        rows = [
            {"kind": kind, "count": count, "ratio": count / total}
            for kind, count in counts.items()
        ]
        rows.sort(key=lambda r: r["count"], reverse=True)
        return rows

    def _newest_titles(self, unplayed: List[Dict[str, Any]], limit: int) -> List[str]:
        dated = [
            (_parse_datetime(item.get("added_at")), str(item.get("title") or ""))
            for item in unplayed
        ]
        dated = [(d, t) for d, t in dated if d and t]
        dated.sort(key=lambda pair: pair[0], reverse=True)
        return [title for _, title in dated[:limit]]

    def _top_played(self, watched: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        rows = [
            {
                "title": str(item.get("title") or item.get("Name") or ""),
                "play_count": _play_count_of(item),
            }
            for item in watched
            if _play_count_of(item) > 0
        ]
        rows.sort(key=lambda r: r["play_count"], reverse=True)
        return rows[:limit]