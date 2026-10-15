"""BI 看板的测试。

核心保护一条原则：**看板上的每个数字都必须有真实来源，拿不到就显示"暂不可用"，
绝不能拿别的数凑**。下面多数用例都是在锁这条。
"""
import pytest

from bi_dashboard_service import BIDashboardService
from bi_snapshot_service import BISnapshotService, should_refresh


class FakeCursor:
    """最小 cursor 替身：按 SQL 交给 handler，返回它给的行。"""

    def __init__(self, handler):
        self._handler = handler
        self.rows = []

    def execute(self, sql, params=None):
        self.rows = self._handler(sql, params)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, handler):
        self._handler = handler

    def cursor(self):
        return FakeCursor(self._handler)

    def commit(self):
        pass


class FakeDB:
    def __init__(self, handler=None, is_postgres=False):
        self._handler = handler or (lambda sql, p: [])
        self.is_postgres = is_postgres
        self.store = {}

    def get_connection(self):
        return FakeConn(self._handler)

    def _release_connection(self, conn):
        pass

    def get_config(self, key):
        return self.store.get(key)

    def set_config(self, key, value):
        self.store[key] = value


class FakeJF:
    def __init__(self, played=None, all_items=None, total=0, fail=False):
        self._played = played if played is not None else [
            {"title": "A", "play_count": 2, "last_played": "2026-10-10T04:00:00Z",
             "runtime_minutes": 120.0, "file_size": 2_000_000_000, "genres": ["剧情"]},
        ]
        self._all = all_items if all_items is not None else []
        self._total = total
        self._fail = fail

    def get_library_items(self, is_played=None, limit=500, offset=0, fields=None):
        if self._fail:
            raise RuntimeError("jellyfin down")
        if is_played is True:
            return {"items": self._played, "total": len(self._played)}
        if is_played is False:
            return {"items": [], "total": 0}
        return {"items": self._all, "total": self._total}


LOG = type("L", (), {"warning": lambda *a, **k: None, "error": lambda *a, **k: None, "info": lambda *a, **k: None})()
CFG = type("C", (), {"X": 1})()


def _rows(sql, params=None):
    """按 SQL 里出现的表名返回对应假数据。匹配顺序要紧：聚合查询要先判。"""
    s = sql.lower()
    if "count(*)" in s and "from movies" in s:
        return [{"c": 1446508}]
    if "count(*)" in s and "from actresses" in s and "avatar" not in s:
        return [{"c": 102286}]
    if "count(*)" in s and "avatar" in s:
        return [{"c": 40139}]
    if "from subscriptions" in s:
        return [{"c": 180}]
    if "movie_actresses" in s:
        return [{"name": "波野結衣", "movie_count": 28440},
                {"name": "大槻ひびき", "movie_count": 19143}]
    if "from local_files" in s:
        return [{"c": 3, "s": 30_000_000}]
    if "count(*)" in s and "from downloads" in s:
        return [{"c": 248}]
    if "from download_queue" in s and "group by" in s:
        return [{"status": "submitted", "c": 248}, {"status": "cancelled", "c": 2}]
    if "from download_queue" in s and "created_at" in s:
        return [{"created_at": 1791800000.0}]
    if "from movie_lifecycle" in s:
        return [{"download_status": "completed", "c": 30}]
    if "detail_json" in s:
        return [{"detail_json": '{"genres": [{"name": "高画质"}, {"name": "中出"}]}'},
                {"detail_json": '{"genres": [{"name": "高画质"}]}'}]
    if "from movies" in s:
        return [{"title": "某片"}]
    return []


def _svc(db=None, jf=None):
    return BIDashboardService(
        db=db or FakeDB(_rows), config=CFG, logger=LOG, jellyfin_client=jf or FakeJF()
    )


# ------------------------------------------------------------------ 总览


def test_build_returns_all_five_sections():
    data = _svc().build()
    for key in ("library", "cast", "playback", "downloads", "storage", "runtime"):
        assert key in data, key


def test_movie_total_is_read_from_db():
    assert _svc().build()["library"]["movie_total"] == 1446508


def test_genre_breakdown_present():
    genres = _svc().build()["library"]["top_genres"]
    assert genres
    assert genres[0]["name"] == "高画质"
    assert genres[0]["sampled"] is True  # 必须标明是抽样


# ------------------------------------------------------------------ 播放


def test_play_totals_are_computed():
    data = _svc().build()
    play = data["playback"]
    assert play["played_movies"] == 1
    assert play["total_plays"] == 2
    # 时长 = 单片 120 分钟 × 播 2 次 = 240 分钟 = 4 小时
    assert play["total_watch_minutes"] == 240
    assert play["total_watch_hours"] == 4.0


def test_daily_trend_uses_only_played_days():
    """Jellyfin 没有逐次流水，趋势只列有记录的日期，不补零值伪造连续。"""
    trend = _svc().build()["playback"]["daily_trend"]
    assert len(trend) == 1
    assert trend[0]["plays"] == 2


def test_playback_degrades_when_jellyfin_down():
    data = _svc(jf=FakeJF(fail=True)).build()
    assert data["playback"]["played_movies"] == 0
    assert any("播放统计" in w for w in data["warnings"])


def test_watch_time_zero_does_not_divide_by_zero():
    """一条都没播过时，均次时长回退到经验值而不是崩掉或给 0，
    且「看完还需多久」必须仍有值（前端不能渲染出空白格）。"""
    play = _svc(jf=FakeJF(played=[])).build()["playback"]
    assert play["total_plays"] == 0
    assert play["avg_minutes_per_play"] == 5      # 回退经验值
    assert "eta_human" in play                    # 字段必须存在
    assert "eta_minutes" in play


# ------------------------------------------------------------------ 下载口径


def test_download_counts_use_correct_sources():
    """回归：download_queue 只有 submitted/cancelled/duplicate_skipped，
    **没有** completed —— 早期版本读它永远得到 0，页面显示「已完成 0」这种假数字。
    已提交数来自 queue 的 submitted，完成记录来自 downloads 表。"""
    down = _svc().build()["downloads"]
    assert down["submitted"] == 248     # 来自 download_queue.status = submitted
    assert down["completed"] == 248     # 来自 downloads 表
    assert down["queue_total"] == 250   # submitted 248 + cancelled 2


# ------------------------------------------------------------------ 存储口径


def test_storage_uses_total_not_first_page():
    """回归：曾把「第一页 500 条」当媒体库总数，2991 部算成 500 部。"""
    items = [{"file_size": 2_000_000_000, "title": f"m{i}"} for i in range(500)]
    data = _svc(jf=FakeJF(all_items=items, total=2991)).build()
    assert data["storage"]["library_items"] == 2991
    # 2GB × 2991 ≈ 5.8TB
    assert data["storage"]["library_bytes"] == 2_000_000_000 * 2991


def test_storage_does_not_mix_candidate_library():
    """回归：曾把「抽样均体积 × 本地 145 万候选片」当成媒体库体积，算出 7.6PB。
    候选库与已入库媒体库不是一个集合，必须分开展示。"""
    data = _svc(jf=FakeJF(all_items=[{"file_size": 2_000_000_000}], total=1)).build()
    store = data["storage"]
    assert store["library_items"] == 1
    assert store["candidate_movies"] == 1446508
    # 体积必须按媒体库条目数算，不能乘候选库
    assert store["library_bytes"] == 2_000_000_000


# ------------------------------------------------------------------ 快照调度


def test_should_refresh_false_right_after_refresh():
    from insight_stats_service import DISPLAY_TZ
    from datetime import datetime
    now = datetime.now(DISPLAY_TZ)
    meta = {"refreshed_at": now.strftime("%Y-%m-%d %H:%M:%S")}
    assert should_refresh(meta, now) is False


def test_should_refresh_true_after_slot_passed():
    from insight_stats_service import DISPLAY_TZ
    from datetime import datetime, timedelta
    now = datetime.now(DISPLAY_TZ)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d 08:00:00")
    assert should_refresh({"refreshed_at": yesterday}, now.replace(hour=12, minute=0)) is True


def test_should_refresh_false_before_first_slot():
    from insight_stats_service import DISPLAY_TZ
    from datetime import datetime, timedelta
    now = datetime.now(DISPLAY_TZ)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d 08:00:00")
    assert should_refresh({"refreshed_at": yesterday}, now.replace(hour=9, minute=0)) is False


def test_snapshot_roundtrip():
    db = FakeDB(_rows)
    svc = BISnapshotService(db=db, builder=lambda **k: _svc().build(), logger=LOG)
    payload = svc.refresh(reason="test")
    assert payload["library"]["movie_total"] == 1446508
    assert payload["stale"] is False
    # 再读应命中快照，不重算
    again = svc.get_payload()
    assert again["library"]["movie_total"] == 1446508


def test_snapshot_keeps_old_data_on_build_failure():
    db = FakeDB(_rows)
    svc = BISnapshotService(db=db, builder=lambda **k: _svc().build(), logger=LOG)
    svc.refresh(reason="ok")

    def boom(**kwargs):
        raise RuntimeError("jellyfin down")

    svc.builder = boom
    result = svc.refresh(reason="fail")
    # 算失败时保留旧快照并标 stale，不能把看板清空
    assert result["library"]["movie_total"] == 1446508
    assert result["stale"] is True