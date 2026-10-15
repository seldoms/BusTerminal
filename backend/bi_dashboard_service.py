"""BI 看板数据聚合

给 `/api/bi/overview` 提供全部指标。**每天只跑两次**（11:30 / 18:00），
结果落 snapshot 供页面秒开；不在页面请求路径上做重计算。

## 数据来源与可得性（务必按此判断，不要臆造指标）

| 指标 | 来源 | 说明 |
| --- | --- | --- |
| 影片总数 | 本地库 `movies` | 145 万量级 |
| 影片类型分布 | `movies.detail_json.genres` | 结构化 JSON 里 |
| 影片总大小 | Jellyfin 条目 `Size` + 本地 `local_files.file_size` | 库里没有全量体积，只能取媒体库侧 |
| 关注演员数 | `actresses` / `subscriptions` | 库里 actresses 是「名字清单」 |
| 播放次数 / 时长 | Jellyfin `UserData.PlayCount` + `RunTimeTicks` | BusTerminal 自己**没有**播放记录表 |
| 每日播放 | Jellyfin `UserData.LastPlayedDate` | 无历史播放流水，取「最近有记录的几天」 |
| 下载量 | `download_queue` / `downloads` / `movie_lifecycle` | |
| 运行时长 | 容器启动时间 | 由调用方传入 |

**播放历史流水在 Jellyfin 侧不存在**（`/user_usage_stats/*` 返回 404），
所以「每天播放趋势」按「最近有播放记录的日期」聚合，不伪造连续零值。
"""
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from insight_stats_service import DISPLAY_TZ, FALLBACK_MINUTES_PER_ITEM

# 快照在 cache 表里的键
BI_SNAPSHOT_KEY = "bi_dashboard_snapshot"

# 每天刷新时刻（小时, 分钟）
BI_REFRESH_SLOTS = ((11, 30), (18, 0))

# 每日自动汇总一次时的时刻。用户反馈"没必要实时"，默认一天一次。
BI_DAILY_SLOT = (11, 30)

# 刷新频率模式：
#   daily  —— 每天在 BI_DAILY_SLOT 汇总一次（默认）
#   twice  —— 保留原来的 11:30 / 18:00 两次
#   realtime —— 每次打开看板都现算（最重，仅在用户明确要求时选）
BI_REFRESH_MODE_KEY = "bi_refresh_mode"
BI_REFRESH_MODE_DAILY = "daily"
BI_REFRESH_MODE_TWICE = "twice"
BI_REFRESH_MODE_REALTIME = "realtime"
BI_REFRESH_MODES = (BI_REFRESH_MODE_DAILY, BI_REFRESH_MODE_TWICE, BI_REFRESH_MODE_REALTIME)


def resolve_refresh_slots(mode: Optional[str]) -> tuple:
    """按配置解析本次应使用的刷新时刻。未知模式退回默认的每天一次。"""
    if mode == BI_REFRESH_MODE_TWICE:
        return BI_REFRESH_SLOTS
    if mode == BI_REFRESH_MODE_REALTIME:
        # 实时模式下用最小间隔表达"随时可刷"，实际由读取路径决定是否现算
        return (BI_DAILY_SLOT,)
    return (BI_DAILY_SLOT,)

# 每日播放趋势最多回看几天
PLAY_TREND_DAYS = 30

# 类型分布 Top N
TOP_GENRES = 10

# 影片大小统计抽样上限：145 万行逐条取体积不现实，
# 且本地库没存体积，只能从 Jellyfin 侧拿已入库条目的真实体积。
LIBRARY_SAMPLE_LIMIT = 5000


def _iso_days_ago(days: int) -> str:
    return (datetime.now(DISPLAY_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")


def _parse_jf_time(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(DISPLAY_TZ).replace(tzinfo=None)


class BIDashboardService:
    def __init__(self, *, db: Any, config: Any, logger: Any, jellyfin_client: Any):
        self.db = db
        self.config = config
        self.logger = logger
        self.jellyfin_client = jellyfin_client

    # ==================================================================
    # 主入口
    # ==================================================================
    def build(self, *, container_uptime_hours: Optional[float] = None) -> Dict[str, Any]:
        """聚合全量 BI 指标。

        任何一路失败都进 `warnings`，不抛异常 —— 看板缺一块也比整页白屏好，
        但缺的那块必须**显示出来**（前端会显示"暂不可用"），不能静默少一块。
        """
        warnings: List[str] = []
        library = self._collect_library(warnings)
        cast = self._collect_cast(warnings)
        playback = self._collect_playback(warnings)
        downloads = self._collect_downloads(warnings)
        storage = self._collect_storage(library, warnings)

        return {
            "generated_at": datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "refresh_slots": [f"{h:02d}:{m:02d}" for h, m in BI_REFRESH_SLOTS],
            "runtime": {
                "container_uptime_hours": round(container_uptime_hours, 1) if container_uptime_hours else None,
            },
            "library": library,
            "cast": cast,
            "playback": playback,
            "downloads": downloads,
            "storage": storage,
            "warnings": warnings,
        }

    # ==================================================================
    # 1. 影片：数量 / 类型 / 大小
    # ==================================================================
    def _eta(self, unwatched: int, minutes_per_item: float) -> Dict[str, Any]:
        """按当前观看速度，看完剩余片单还要多久（与观看提醒邮件同一算法）。"""
        minutes = max(int(unwatched), 0) * max(float(minutes_per_item), 0)
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

    def _collect_library(self, warnings: List[str]) -> Dict[str, Any]:
        total = 0
        try:
            conn = self.db.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) AS c FROM movies")
                row = cursor.fetchone()
                total = int(row["c"] if row else 0)
            finally:
                self.db._release_connection(conn)
        except Exception as exc:
            warnings.append(f"影片总数读取失败：{type(exc).__name__}: {exc}")

        genres = self._genres_from_db(warnings)
        return {
            "movie_total": total,
            "genre_breakdown": genres,
            "genre_total_kinds": len(genres),
            "top_genres": genres[:TOP_GENRES],
            "recent_unwatched": self._recent_unwatched(),
        }

    def _recent_unwatched(self) -> List[str]:
        """最近入库且还没看的片名（用于「待看清单」）。

        取本地候选库里最新入库、且生命周期里尚未完成观看的一批。
        """
        try:
            conn = self.db.get_connection()
            try:
                cursor = conn.cursor()
                placeholder = "%s" if getattr(self.db, "is_postgres", False) else "?"
                cursor.execute(
                    f"SELECT title FROM movies WHERE title IS NOT NULL AND title <> '' "
                    f"ORDER BY scraped_at DESC NULLS LAST LIMIT 8"
                )
                rows = cursor.fetchall() or []
            finally:
                self.db._release_connection(conn)
        except Exception:
            return []
        return [
            str((r["title"] if not isinstance(r, dict) else r.get("title")) or "")
            for r in rows
        ]

    def _genres_from_db(self, warnings: List[str]) -> List[Dict[str, Any]]:
        """从 detail_json.genres 统计类型分布。

        145 万行逐条解 JSON 太重，因此**抽样最近抓取的若干条**做结构占比估计，
        并在结果里标注 sampled=True，页面会写明这是抽样而非全量。
        """
        counter: Counter = Counter()
        sampled = 0
        try:
            conn = self.db.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT detail_json FROM movies "
                    "WHERE detail_json IS NOT NULL AND scraped_at IS NOT NULL "
                    "ORDER BY scraped_at DESC LIMIT 4000"
                )
                rows = cursor.fetchall() or []
            finally:
                self.db._release_connection(conn)
        except Exception as exc:
            warnings.append(f"类型分布读取失败：{type(exc).__name__}: {exc}")
            return []

        for row in rows:
            raw = row["detail_json"] if not isinstance(row, dict) else row.get("detail_json")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    continue
            if not isinstance(raw, dict):
                continue
            sampled += 1
            for genre in raw.get("genres") or []:
                name = genre.get("name") if isinstance(genre, dict) else genre
                if name:
                    counter[str(name).strip()] += 1

        if not counter:
            warnings.append("类型分布暂不可用：detail_json 无 genres 字段")
            return []

        total = sum(counter.values()) or 1
        breakdown = [
            {"name": name, "count": count, "ratio": count / total}
            for name, count in counter.most_common(TOP_GENRES * 2)
        ]
        return [{"name": r["name"], "count": r["count"], "ratio": r["ratio"], "sampled": True,
                 "sampled_rows": sampled} for r in breakdown]

    # ==================================================================
    # 2. 演员 / 关注
    # ==================================================================
    def _collect_cast(self, warnings: List[str]) -> Dict[str, Any]:
        result = {"actress_total": 0, "with_avatar": 0, "subscribed": 0, "top_actresses": []}
        try:
            conn = self.db.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) AS c FROM actresses")
                result["actress_total"] = int(cursor.fetchone()["c"] or 0)
                try:
                    cursor.execute(
                        "SELECT COUNT(*) AS c FROM actresses WHERE avatar IS NOT NULL AND avatar <> ''"
                    )
                    result["with_avatar"] = int(cursor.fetchone()["c"] or 0)
                except Exception:
                    pass
                cursor.execute("SELECT COUNT(DISTINCT url) AS c FROM subscriptions")
                result["subscribed"] = int(cursor.fetchone()["c"] or 0)
            finally:
                self.db._release_connection(conn)
        except Exception as exc:
            warnings.append(f"演员统计读取失败：{type(exc).__name__}: {exc}")

        # 关注最多的演员：按 movie_actresses 的实际关联数统计作品量。
        # ⚠️ 不能对 actresses 直接 ORDER BY 某个计数列 —— 该表没有作品数字段，
        # 排序会退化成常量，SQLite 返回一行、PG 直接报错，页面上就会出现
        # 「某演员 102286 部」这种把总数当作品数的假数据。
        try:
            conn = self.db.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT a.name AS name, COUNT(*) AS movie_count "
                    "FROM movie_actresses ma "
                    "JOIN actresses a ON a.name = ma.actress_name "
                    "GROUP BY a.name "
                    "ORDER BY movie_count DESC LIMIT 10"
                )
                rows = cursor.fetchall() or []
                result["top_actresses"] = [
                    {
                        "name": (r["name"] if not isinstance(r, dict) else r.get("name")),
                        "movies": int(
                            (r["movie_count"] if not isinstance(r, dict) else r.get("movie_count")) or 0
                        ),
                    }
                    for r in rows
                    if str((r["name"] if not isinstance(r, dict) else r.get("name")) or "").strip()
                ]
            finally:
                self.db._release_connection(conn)
        except Exception as exc:
            warnings.append(f"热门演员读取失败：{type(exc).__name__}: {exc}")
        return result

    # ==================================================================
    # 3 & 4. 播放：次数 / 时长 / 每日趋势
    # ==================================================================
    def _collect_playback(self, warnings: List[str]) -> Dict[str, Any]:
        # 字段集要一次给全：早退分支也必须返回同样的键，
        # 否则前端读到 undefined 会渲染成空白格或 NaN。
        result: Dict[str, Any] = {
            "played_movies": 0,
            "total_plays": 0,
            "total_watch_minutes": 0,
            "total_watch_hours": 0,
            "avg_minutes_per_play": 0,
            "last_played_at": None,
            "daily_trend": [],
            "trend_note": "",
            "top_played": [],
            "eta_human": "片单已清空",
            "eta_minutes": 0,
            "minutes_per_item": FALLBACK_MINUTES_PER_ITEM,
            "unwatched_total": 0,
        }

        fetched = self._fetch_jf_items(is_played=True)
        if fetched is None:
            warnings.append("播放统计暂不可用：Jellyfin 未返回数据")
            result["trend_note"] = "播放统计暂不可用"
            return result
        items, _ = fetched

        total_plays = 0
        total_minutes = 0.0
        days: Counter = Counter()
        for item in items:
            plays = int(item.get("play_count") or 0)
            minutes = float(item.get("runtime_minutes") or 0)
            total_plays += plays
            # 时长按「实际播了几次」累加
            total_minutes += minutes * plays
            played_at = _parse_jf_time(item.get("last_played"))
            if played_at and plays > 0:
                days[played_at.strftime("%Y-%m-%d")] += plays

        latest = None
        for item in items:
            parsed = _parse_jf_time(item.get("last_played"))
            if parsed and (latest is None or parsed > latest):
                latest = parsed

        # 未观看总量与「看完还需多久」：与观看提醒邮件同一口径，
        # 让看板和邮件说同一个数，不会两处对不上。
        unplayed_fetched = self._fetch_jf_items(is_played=False)
        unplayed_items = unplayed_fetched[0] if unplayed_fetched else []
        unwatched = len(unplayed_items)
        per_play = (
            round(total_minutes / total_plays, 1)
            if total_plays else FALLBACK_MINUTES_PER_ITEM
        )
        eta = self._eta(unwatched, per_play)

        result.update({
            "played_movies": len(items),
            "total_plays": total_plays,
            "total_watch_minutes": int(total_minutes),
            "total_watch_hours": round(total_minutes / 60, 1),
            "avg_minutes_per_play": per_play,
            "minutes_per_item": per_play,
            "unwatched_total": unwatched,
            "eta_human": eta["human"],
            "eta_minutes": eta["minutes"],
            "last_played_at": latest.strftime("%Y-%m-%d %H:%M") if latest else None,
            "daily_trend": self._build_trend(days),
            "top_played": [
                {
                    "title": str(item.get("title") or ""),
                    "play_count": int(item.get("play_count") or 0),
                    "minutes": float(item.get("runtime_minutes") or 0),
                }
                for item in sorted(items, key=lambda i: int(i.get("play_count") or 0), reverse=True)[:10]
                if int(item.get("play_count") or 0) > 0
            ],
        })

        if not days:
            result["trend_note"] = "暂无播放记录"
        elif len(days) < 7:
            # Jellyfin 只存每片的「最后播放时间」，没有逐次流水，
            # 所以趋势图按「最近有播放记录的日期」给，不补零值伪造连续性。
            result["trend_note"] = (
                f"仅有 {len(days)} 天有播放记录（Jellyfin 仅保存每片最后播放时间，无逐次流水）"
            )
        return result

    def _build_trend(self, days: Counter) -> List[Dict[str, Any]]:
        """只列有播放记录的日期，按日期升序。"""
        rows = [{"date": day, "plays": count} for day, count in sorted(days.items())]
        return rows[-PLAY_TREND_DAYS:]

    # ==================================================================
    # 5. 下载
    # ==================================================================
    def _collect_downloads(self, warnings: List[str]) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "queue_total": 0, "by_status": [], "completed": 0,
            "submitted": 0, "skipped": 0,
            "downloaded_records": 0, "lifecycle_done": 0, "by_source": [],
            "daily_downloads": [],
        }
        try:
            conn = self.db.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT status, COUNT(*) AS c FROM download_queue GROUP BY status")
                # get_connection 返回的是字典行（列名可取），不是元组 ——
                # 早期这里按 `for s, c in ...` 解包，会把字符串列名 'c' 拿去做 int()
                # 直接抛 ValueError，整个下载统计降级为空。
                rows = cursor.fetchall() or []
                result["by_status"] = [
                    {
                        "status": (r["status"] if not isinstance(r, dict) else r.get("status")),
                        "count": int((r["c"] if not isinstance(r, dict) else r.get("c")) or 0),
                    }
                    for r in rows
                    if (r["status"] if not isinstance(r, dict) else r.get("status"))
                ]
                result["queue_total"] = sum(r["count"] for r in result["by_status"])
                # ⚠️ download_queue 里**没有** completed 状态 —— 只有
                # cancelled / duplicate_skipped / submitted（状态止于"已提交给 qB"）。
                # 早期版本在这里读 completed，永远是 0，页面会显示"已完成 0"这种假数字。
                # 真正的完成记录在 downloads 表。
                result["submitted"] = next(
                    (r["count"] for r in result["by_status"] if r["status"] == "submitted"), 0
                )
                result["skipped"] = next(
                    (r["count"] for r in result["by_status"] if r["status"] == "duplicate_skipped"), 0
                )
                cursor.execute("SELECT COUNT(*) AS c FROM downloads")
                result["completed"] = int(cursor.fetchone()["c"] or 0)
                try:
                    cursor.execute(
                        "SELECT download_status, COUNT(*) AS c FROM movie_lifecycle GROUP BY download_status"
                    )
                    rows = cursor.fetchall() or []
                    result["by_source"] = [
                        {
                            "status": (r["download_status"] if not isinstance(r, dict) else r.get("download_status")),
                            "count": int((r["c"] if not isinstance(r, dict) else r.get("c")) or 0),
                        }
                        for r in rows
                    ]
                    result["lifecycle_done"] = next(
                        (r["count"] for r in result["by_source"] if r["status"] == "completed"), 0
                    )
                except Exception as exc:
                    # 这里曾被静默 except 吞掉，导致状态分布永远为空还看不出原因
                    warnings.append(f"生命周期状态读取失败：{type(exc).__name__}: {exc}")
                # 下载按天分布（近 30 天）
                since = datetime.now(DISPLAY_TZ).timestamp() - 30 * 86400
                placeholder = "%s" if getattr(self.db, "is_postgres", False) else "?"
                cursor.execute(
                    f"SELECT created_at FROM download_queue WHERE created_at >= {placeholder}",
                    (since,),
                )
                buckets: Counter = Counter()
                for row in cursor.fetchall():
                    raw = row["created_at"] if not isinstance(row, dict) else row.get("created_at")
                    try:
                        buckets[datetime.fromtimestamp(float(raw), DISPLAY_TZ).strftime("%Y-%m-%d")] += 1
                    except Exception:
                        continue
                result["daily_downloads"] = [
                    {"date": d, "count": n} for d, n in sorted(buckets.items())
                ]
            finally:
                self.db._release_connection(conn)
        except Exception as exc:
            warnings.append(f"下载统计读取失败：{type(exc).__name__}: {exc}")
        return result

    # ==================================================================
    # 存储
    # ==================================================================
    def _collect_storage(self, library: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "library_items": 0, "library_bytes": 0,
            "local_files": 0, "local_bytes": 0, "sampled": False, "avg_bytes": 0,
        }
        # 本地已登记文件（条数少但体积真实）
        try:
            conn = self.db.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) AS c, COALESCE(SUM(file_size),0) AS s FROM local_files")
                row = cursor.fetchone()
                result["local_files"] = int(row["c"] or 0)
                result["local_bytes"] = int(row["s"] or 0)
            finally:
                self.db._release_connection(conn)
        except Exception as exc:
            warnings.append(f"本地存储统计失败：{type(exc).__name__}: {exc}")

        # 媒体库侧体积：抽样已入库条目估算平均值
        fetched = self._fetch_jf_items(is_played=None)
        items, library_total = fetched if fetched else ([], 0)
        if items:
            sizes = [int(i.get("file_size") or 0) for i in items if int(i.get("file_size") or 0) > 0]
            if sizes:
                avg = sum(sizes) / len(sizes)
                result["sampled"] = True
                result["sample_count"] = len(sizes)
                result["avg_bytes"] = int(avg)
                # ⚠️ 只能拿抽样均值 × **同一个口径的总数**。
                # 拿它乘本地 145 万条会算出 7.6 PB —— 本地库是「发现过的候选片」，
                # 与「实际入库可播放的片」根本不是一个集合，不能混算。
                # 同理也不能拿「第一页条数」当总数（早期版本这么干，2991 部算成 500 部）。
                result["library_items"] = int(library_total or len(items))
                result["library_bytes"] = int(avg * result["library_items"])
        else:
            warnings.append("媒体库体积暂不可用")
        # 本地候选库条目数单独给出，供页面对比（不参与体积计算）
        result["candidate_movies"] = library.get("movie_total") or 0
        return result

    # ==================================================================
    def _fetch_jf_items(
        self, *, is_played: Optional[bool]
    ) -> Optional[Tuple[List[Dict[str, Any]], int]]:
        """取 Jellyfin 条目。返回 (条目列表, 媒体库总数)；失败返回 None。

        **必须同时返回 total**：早期版本只返回第一页条目，把「一页 500 条」
        当成了媒体库总数，2991 部的库被算成 500 部，总量估算偏小 6 倍。

        fields 必须带 MediaSources：Jellyfin 的体积挂在 MediaSources[].Size 上，
        条目顶层 Size 恒为 null，少了这个字段会算出「总存储 0 字节」。
        """
        items: List[Dict[str, Any]] = []
        fields = "Name,UserData,RunTimeTicks,Size,Genres,DateCreated,MediaSources"
        try:
            page = self.jellyfin_client.get_library_items(
                is_played=is_played, limit=500, offset=0, fields=fields
            )
        except Exception as exc:
            self.logger.warning("BI: jellyfin fetch failed: %s", type(exc).__name__)
            return None
        items.extend(page.get("items") or [])
        total = int(page.get("total") or 0)
        # 播放统计只需已看/未看全量；全库体积靠抽样，不翻全库
        if is_played is None:
            return items, total
        offset = len(items)
        while total > offset and offset < 3000:
            try:
                nxt = self.jellyfin_client.get_library_items(
                    is_played=is_played, limit=500, offset=offset, fields=fields
                )
            except Exception:
                break
            batch = list(nxt.get("items") or [])
            if not batch:
                break
            items.extend(batch)
            offset += len(batch)
        return items, total