"""待下载重试策略的锁定测试（2026-11-09 用户口径：对源站压力小）。

背景：1437 条 pending 的 note 是「No magnet on source」——源站上确实没有磁力。
原退避阶梯封顶 24 小时且**无次数上限** → 每天每条都去问站点一次，
等于每天给源站发 1437 次无效请求。

新口径：
- 「源站没磁力」单独一套慢退避（24h → 3d → 7d → 14d 封顶）
- 重试够 6 次就彻底不再自动打源站（源站加磁力或人工介入才恢复）
- 其它 pending（网络抖动等临时故障）保持原来的快节奏退避
"""

import time

from pending_download_service import (
    PENDING_NO_MAGNET_MAX_ATTEMPTS,
    get_pending_next_retry_at,
    get_pending_retry_delay_seconds,
    is_no_magnet_item,
    is_pending_download_due,
    is_pending_exhausted,
    select_due_pending_downloads,
    summarize_pending_downloads,
)


def _item(note, retry_count=0, last_checked_at=None, status="pending"):
    return {
        "movie_id": "ABC-001",
        "title": "某作品",
        "note": note,
        "status": status,
        "retry_count": retry_count,
        "added_at": 1_000_000.0,
        "last_checked_at": last_checked_at if last_checked_at is not None else 1_000_000.0,
    }


def test_no_magnet_item_is_recognized():
    assert is_no_magnet_item(_item("No magnet on source; retry later"))
    assert is_no_magnet_item(_item("No suitable magnet found"))
    assert is_no_magnet_item(_item("movie_not_found"))
    # 临时故障 / 本地拦截不算
    assert not is_no_magnet_item(_item("blocked: daily_limit_reached"))
    assert not is_no_magnet_item(_item(""))


def test_no_magnet_uses_slow_backoff():
    """源站没磁力：第一次 24h、之后 3d/7d/14d 封顶（不再是每天一次）。"""
    delays = [
        get_pending_retry_delay_seconds(i, no_magnet=True)
        for i in range(6)
    ]
    assert delays[0] == 24 * 3600
    assert delays[1] == 3 * 24 * 3600
    assert delays[2] == 7 * 24 * 3600
    assert delays[3] == 14 * 24 * 3600
    # 封顶：不再无限增长
    assert delays[4] == delays[5] == 14 * 24 * 3600


def test_regular_pending_keeps_fast_backoff():
    """临时故障仍走原来的快节奏（30min→2h→6h→24h）。"""
    delays = [
        get_pending_retry_delay_seconds(i, no_magnet=False)
        for i in range(5)
    ]
    assert delays == [30 * 60, 2 * 3600, 6 * 3600, 24 * 3600, 24 * 3600]


def test_no_magnet_item_next_retry_uses_slow_delay():
    now = 1_000_000.0
    item = _item("No magnet on source; retry later", retry_count=1, last_checked_at=now)
    # 第 2 次应是 3 天后，不是 1 天后
    assert get_pending_next_retry_at(item) == now + 3 * 24 * 3600


def test_exhausted_after_max_attempts():
    """重试够 6 次 → 不再自动打源站。"""
    item = _item("No magnet on source; retry later", retry_count=PENDING_NO_MAGNET_MAX_ATTEMPTS)
    assert is_pending_exhausted(item)
    # 差一次还没到上限
    assert not is_pending_exhausted(
        _item("No magnet on source; retry later", retry_count=PENDING_NO_MAGNET_MAX_ATTEMPTS - 1)
    )


def test_exhausted_item_is_never_due():
    """已达上限的条目，即使过了很久也不该被选中去打站。"""
    item = _item(
        "No magnet on source; retry later",
        retry_count=PENDING_NO_MAGNET_MAX_ATTEMPTS,
        last_checked_at=time.time() - 100 * 86400,
    )
    assert not is_pending_download_due(item, now=time.time())


def test_regular_item_never_exhausted():
    """临时故障不受重试次数上限约束（否则真故障会被永久放弃）。"""
    item = _item("blocked: daily_limit_reached", retry_count=99)
    assert not is_pending_exhausted(item)


def test_select_due_skips_exhausted():
    now = time.time()
    rows = [
        _item("No magnet on source; retry later", retry_count=PENDING_NO_MAGNET_MAX_ATTEMPTS,
              last_checked_at=now - 10 * 86400),
        _item("blocked: daily_limit_reached", retry_count=0, last_checked_at=now - 10 * 86400),
    ]
    due = select_due_pending_downloads(rows, limit=50, now=now)
    assert len(due) == 1
    assert due[0]["note"] == "blocked: daily_limit_reached"


def test_summary_reports_exhausted_count():
    now = time.time()
    rows = [
        _item("No magnet on source; retry later", retry_count=PENDING_NO_MAGNET_MAX_ATTEMPTS,
              last_checked_at=now - 10 * 86400),
        _item("blocked: daily_limit_reached", retry_count=0, last_checked_at=now - 10 * 86400),
    ]
    summary = summarize_pending_downloads(rows, now=now)
    assert summary["pending_count"] == 2
    assert summary["exhausted_count"] == 1
    assert summary["due_count"] == 1


def test_daily_source_pressure_drops():
    """核心目标：同样 100 条无磁力条目，一天的实际打站次数要显著下降。"""
    now = time.time()
    rows = [
        _item("No magnet on source; retry later", retry_count=3, last_checked_at=now - 2 * 86400)
        for _ in range(100)
    ]
    due = select_due_pending_downloads(rows, limit=1000, now=now)
    # 旧策略：24h 封顶 → 第 3 次是 24h 后，2 天前就该到期 → 100 条全部到期
    # 新策略：第 3 次是 7 天后 → 2 天前没到期 → 0 条
    assert len(due) == 0


def test_stats_use_full_pending_set_not_truncated():
    """统计口径：pending 有 1637 条时，只取 500 条会让 pending_count /
    exhausted_count 全部失真（页面看到的数字对不上实际）。"""
    now = time.time()
    rows = [
        _item("No magnet on source; retry later", retry_count=PENDING_NO_MAGNET_MAX_ATTEMPTS,
              last_checked_at=now - 10 * 86400)
        for _ in range(9)
    ] + [
        _item("blocked: daily_limit_reached", retry_count=0, last_checked_at=now - 10 * 86400)
        for _ in range(1628)
    ]
    summary = summarize_pending_downloads(rows, now=now)
    assert summary["pending_count"] == 1637
    assert summary["exhausted_count"] == 9
    # 耗尽的条目不占用「到期」名额
    assert summary["due_count"] == 1628
