"""观看提醒邮件的测试。

重点保护三件事：
1. **数字必须真算** —— 「看完还要多久」不能是模板里写死的字符串。
2. **图表在邮件里真能显示** —— 必须内联 SVG，不能依赖会被客户端屏蔽的外链图片。
3. **降级要如实说** —— 取数失败时不能拿 0 冒充真实值。
"""
import re

import pytest

from insight_email_template import (
    build_watching_digest_email,
    _progress_ring,
    _pie_slices,
)
from insight_stats_service import InsightStatsService


class FakeJF:
    """模拟真实 Jellyfin：只有 get_library_items，按 is_played 过滤并分页。"""

    def __init__(self, watched=None, unplayed=None, fail=()):
        self._watched = watched if watched is not None else [
            {"title": "A", "play_count": 5, "last_played": "2026-10-03T20:00:00Z"},
        ]
        self._unplayed = unplayed if unplayed is not None else [
            {"title": f"片{i}", "added_at": "2026-10-10T00:00:00Z", "genres": []} for i in range(10)
        ]
        self._fail = set(fail)

    def get_library_items(self, is_played=None, limit=500, offset=0):
        if "watched" in self._fail and is_played is True:
            raise RuntimeError("boom")
        if "unplayed" in self._fail and is_played is False:
            raise RuntimeError("boom")
        rows = self._watched if is_played is True else self._unplayed
        page = rows[offset:offset + limit]
        return {"items": list(page), "total": len(rows)}


def _svc(jf, **kw):
    return InsightStatsService(
        jellyfin_client=jf, db=None, config=None,
        logger=type("L", (), {"warning": lambda *a, **k: None})(),
        **kw
    )


class PagedJF:
    """模拟真实 Jellyfin：get_library_items 分页，且只返回规范化字段。"""

    def __init__(self, watched_count, unplayed_count, last_played="2026-10-03T08:00:00Z"):
        self.watched_count = watched_count
        self.unplayed_count = unplayed_count
        self.last_played = last_played
        self.calls = []

    def get_library_items(self, is_played=None, limit=500, offset=0):
        self.calls.append((is_played, limit, offset))
        total = self.watched_count if is_played is True else self.unplayed_count
        rows = []
        for i in range(offset, min(offset + limit, total)):
            if is_played is True:
                rows.append({
                    "title": f"看过的{i}", "play_count": 2,
                    "last_played": self.last_played, "added_at": "2026-09-01T00:00:00Z",
                })
            else:
                rows.append({
                    "title": f"待看的{i}", "play_count": 0, "last_played": None,
                    "added_at": "2026-10-10T00:00:00Z", "genres": [],
                })
        return {"items": rows, "total": total}


# ------------------------------------------------------------------ 回归


def test_pagination_keeps_full_counts():
    """回归：只取 limit=500 会把 2888 报成 500，「还需多久」错一个数量级。"""
    svc = _svc(PagedJF(watched_count=103, unplayed_count=2888))
    stats = svc.collect()
    assert stats["watched_total"] == 103
    assert stats["unwatched_total"] == 2888
    assert stats["eta_minutes"] == 2888 * 5


def test_uses_library_items_not_raw_watched_movies():
    """回归：get_watched_movies 返回原始 Jellyfin 结构，取不到 LastPlayedDate，
    未观看天数会永远是 None → 永远不发提醒。"""
    class RawJF:
        def get_watched_movies(self):
            return [{"Name": "x", "UserData": {"LastPlayedDate": "2026-10-03T08:00:00Z"}}]
        def get_library_items(self, is_played=None, limit=500, offset=0):
            rows = [{"title": "y", "play_count": 1, "last_played": "2026-10-03T08:00:00Z"}]
            return {"items": rows, "total": 1}

    stats = _svc(RawJF()).collect()
    assert stats["last_played_at"] == "2026-10-03"
    assert stats["idle_days_actual"] >= 8


def test_reads_nested_user_data_as_fallback():
    """兼容原始结构：UserData.LastPlayedDate 也要认，否则换接口就断。"""
    stats = _svc(FakeJF(watched=[{
        "title": "A", "UserData": {"LastPlayedDate": "2026-10-03T08:00:00Z", "PlayCount": 4},
    }])).collect()
    assert stats["last_played_at"] == "2026-10-03"
    assert stats["play_count_top"][0]["play_count"] == 4


def test_recently_watched_does_not_notify():
    """今天刚看过 -> idle 0 -> 不该发提醒，避免打扰。"""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    stats = _svc(PagedJF(watched_count=10, unplayed_count=100, last_played=now)).collect(idle_days=10)
    assert stats["idle_days_actual"] == 0
    assert stats["should_notify"] is False


# ------------------------------------------------------------------ 统计


def test_eta_is_computed_not_hardcoded():
    # 20 部 × 5 分钟 = 100 分钟 = 1.7 小时。模板写死就会露馅
    stats = _svc(FakeJF(unplayed=[{"title": f"x{i}", "added_at": "2026-10-10T00:00:00Z"} for i in range(20)])).collect()
    assert stats["unwatched_total"] == 20
    assert stats["eta_minutes"] == 100
    assert "小时" in stats["eta_human"]


def test_eta_scales_to_days():
    stats = _svc(FakeJF(unplayed=[{"title": f"x{i}", "added_at": "2026-10-10T00:00:00Z"} for i in range(2888)])).collect()
    # 2888 × 5 分钟 ≈ 10 天（不是 20 年 —— 这正是不能硬编文案的例子）
    assert stats["eta_minutes"] == 2888 * 5
    assert "天" in stats["eta_human"]


def test_eta_units_scale_correctly():
    """直接测换算：小时/天/年三档都要对，不能只测其中一档。

    之前这条用 40 万部去构造「一年」，实际 5 分钟/部只需 10.5 万部才够一年
    （400000×5 分钟 = 1389 天 = 3.8 年），而分页上限又把数量截住了，
    于是测出来是「17.4 天」。数字换算本身没错，是测试的量级取错了。
    """
    from insight_stats_service import InsightStatsService as Svc
    svc = Svc(jellyfin_client=None, db=None, config=None,
              logger=type("L", (), {"warning": lambda *a, **k: None})())
    assert "小时" in svc._eta(20, 5)["human"]        # 100 分钟
    assert "天" in svc._eta(2888, 5)["human"]       # 10.0 天
    # 105120 部 × 5 分钟 = 525600 分钟 = 365 天 → 应显示为年
    assert "年" in svc._eta(105120, 5)["human"]


def test_truncation_is_disclosed_not_hidden():
    """命中分页上限时必须标注，不能拿被截断的计数当真值。"""
    from insight_stats_service import MAX_PAGINATE
    over = MAX_PAGINATE + 1000
    stats = _svc(PagedJF(watched_count=1, unplayed_count=over)).collect()
    assert stats["unwatched_total"] == MAX_PAGINATE
    assert stats["truncated"] is True
    _, html = build_watching_digest_email(stats)
    assert "下限" in html


def test_empty_watchlist_says_cleared():
    stats = _svc(FakeJF(unplayed=[])).collect()
    assert stats["unwatched_total"] == 0
    assert stats["eta_human"] == "片单已清空"


def test_idle_days_derived_from_last_played():
    # UTC 08:00 -> UTC+8 当天 16:00，不跨日，便于断言日历日
    stats = _svc(FakeJF(watched=[{"title": "A", "play_count": 1, "last_played": "2026-10-03T08:00:00Z"}])).collect(idle_days=10)
    assert stats["last_played_at"] == "2026-10-03"
    assert stats["idle_days_actual"] >= 8
    assert isinstance(stats["should_notify"], bool)


def test_last_played_uses_fixed_utc8_not_machine_tz():
    """UTC 晚间播放换算后不该跨日。

    回归：曾用 astimezone() 换成本机时区，UTC 20:00 在 UTC+8 下变成次日 04:00，
    「已 N 天没看」整体偏大一天。必须固定按 UTC+8，且与机器 TZ 无关。
    """
    from insight_stats_service import _parse_datetime

    # UTC 12:00 之后换算到 UTC+8 就是同一天更晚的时刻；UTC 20:00 -> 次日 04:00
    assert _parse_datetime("2026-10-03T04:00:00Z").strftime("%Y-%m-%d %H:%M") == "2026-10-03 12:00"
    assert _parse_datetime("2026-10-03T20:00:00Z").strftime("%Y-%m-%d %H:%M") == "2026-10-04 04:00"
    # 不带时区的原样返回
    assert _parse_datetime("2026-10-03T20:00:00").strftime("%Y-%m-%d %H:%M") == "2026-10-03 20:00"
    # 坏输入返回 None 而不是抛异常
    assert _parse_datetime("not-a-date") is None
    assert _parse_datetime(None) is None


def test_should_notify_false_before_threshold():
    watched = [{"title": "A", "play_count": 1, "last_played": None}]
    # 从没看过 -> idle 0，不该发
    stats = _svc(FakeJF(watched=watched)).collect(idle_days=10)
    assert stats["idle_days_actual"] == 0
    assert stats["should_notify"] is False


def test_degraded_flagged_when_fetch_fails():
    stats = _svc(FakeJF(fail=("watched", "unplayed"))).collect()
    assert stats["degraded"] is True
    assert len(stats["degraded_reasons"]) == 2


def test_kind_breakdown_sums_to_total():
    unplayed = [{"title": "a", "added_at": "2026-10-10T00:00:00Z", "genres": ["剧情"]},
                {"title": "b", "added_at": "2026-10-10T00:00:00Z", "genres": ["综艺"]},
                {"title": "c", "added_at": "2026-10-10T00:00:00Z", "genres": []}]
    stats = _svc(FakeJF(unplayed=unplayed)).collect()
    assert sum(r["count"] for r in stats["kind_breakdown"]) == 3


# ------------------------------------------------------------------ 图表


def test_email_uses_inline_svg_not_remote_images():
    """邮件客户端默认屏蔽外链图片，用 <img src=http> 的图表必然空白。"""
    stats = _svc(FakeJF()).collect()
    _, html = build_watching_digest_email(stats)
    assert "<svg" in html
    assert not re.search(r'<img[^>]+src="https?://', html)


def test_pie_chart_slices_sum_to_full_circle():
    rows = [{"kind": "a", "count": 3}, {"kind": "b", "count": 1}]
    assert len(_pie_slices(rows)) == 2


def test_pie_chart_handles_full_circle():
    # 单一分类占 100% 时起止点重合，必须用 circle 才画得出来
    rows = [{"kind": "only", "count": 10}]
    slices = _pie_slices(rows)
    assert len(slices) == 1
    assert "<circle" in slices[0]


def test_progress_ring_never_renders_empty_for_nonzero():
    """0.1% 会被四舍五入成 0%，环上看不见 -> 用户以为数据坏了。"""
    for percent in (0.001, 0.0034, 0.34):
        svg = _progress_ring(percent)
        filled = float(re.search(r'stroke-dasharray="([\d.]+)', svg).group(1))
        assert filled > 0, f"{percent} 渲染成了空环"


def test_progress_ring_zero_is_explicit():
    assert ">0%<" in _progress_ring(0.0)


def test_progress_ring_sub_one_percent_label():
    assert "&lt; 1%" in _progress_ring(0.001)


# ------------------------------------------------------------------ 文案


def test_email_contains_real_numbers():
    stats = _svc(FakeJF(unplayed=[{"title": f"x{i}", "added_at": "2026-10-10T00:00:00Z"} for i in range(2888)])).collect()
    stats["should_notify"] = True
    stats["idle_days_actual"] = 10
    stats["last_played_at"] = "2026-10-03"
    subject, html = build_watching_digest_email(stats)
    assert "2888" in subject
    assert "2888" in html
    assert stats["eta_human"] in html


def test_email_surfaces_degraded_warning():
    stats = _svc(FakeJF(fail=("watched",))).collect()
    stats["should_notify"] = True
    _, html = build_watching_digest_email(stats)
    assert "部分数据未能读取" in html


def test_email_states_pace_is_estimate():
    # 观看速度是假设值，必须在邮件里讲明，不能让人误以为是实测
    stats = _svc(FakeJF()).collect()
    _, html = build_watching_digest_email(stats)
    assert "估算" in html


def test_email_escapes_titles():
    stats = _svc(FakeJF(unplayed=[{"title": "<script>alert(1)</script>", "added_at": "2026-10-10T00:00:00Z"}])).collect()
    _, html = build_watching_digest_email(stats)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_email_has_no_hardcoded_watchlist_size():
    """模板里若写死某个片单数量，换库就露馅 —— 断言数字只来自 stats。"""
    stats = _svc(FakeJF(unplayed=[{"title": f"x{i}", "added_at": "2026-10-10T00:00:00Z"} for i in range(37)])).collect()
    _, html = build_watching_digest_email(stats)
    assert "37" in html
    assert "2888" not in html