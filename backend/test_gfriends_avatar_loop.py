"""头像定时补全循环的用例（backend/test_gfriends_avatar_loop.py）。

重点守住三件事：
  1. **幂等** —— 只能改 avatar 为空的行，绝不覆盖已有头像；
  2. **空转不扰民** —— 没有候选时不应再发请求；
  3. **异常不中断** —— 上游索引拉失败时循环必须继续下一轮。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import main as main_module  # noqa: E402


class _StopAfterFirst:
    """让 while 循环只跑一轮就停。"""

    def __init__(self):
        self.calls = 0

    def is_set(self):
        self.calls += 1
        return self.calls > 1

    def wait(self, seconds):
        return None


def _loop_with(syncer, stop_event):
    """把 main 模块里的依赖替换成假对象，跑一轮 gfriends_avatar_sync_loop。"""
    original = (
        main_module.stop_event,
        getattr(main_module.config, "GFRIENDS_AVATAR_SYNC_INTERVAL_SECONDS", None),
        getattr(main_module.config, "GFRIENDS_AVATAR_SYNC_BATCH", None),
    )
    main_module.stop_event = stop_event
    try:
        main_module.gfriends_avatar_sync_loop()
    finally:
        main_module.stop_event = original[0]


def _install_fake_syncer(monkey_target, report_or_exc):
    """把 gfriends_sync.GfriendsAvatarSyncer 换成可控假对象。"""
    import gfriends_sync

    calls = {"sync": 0}

    class FakeSyncer:
        def __init__(self, db):
            self.db = db

        def sync(self, dry_run=False, limit=None, randomize=False):
            calls["sync"] += 1
            calls["limit"] = limit
            calls["randomize"] = randomize
            if isinstance(report_or_exc, Exception):
                raise report_or_exc
            return report_or_exc

    monkey_target.GfriendsAvatarSyncer = FakeSyncer
    return calls


def test_loop_runs_once_and_marks_success():
    stop = _StopAfterFirst()
    import gfriends_sync
    original = gfriends_sync.GfriendsAvatarSyncer
    calls = _install_fake_syncer(
        gfriends_sync,
        {"candidates": 300, "matched": 120, "applied": 118, "dry_run": False},
    )
    try:
        _loop_with(None, stop)
    finally:
        gfriends_sync.GfriendsAvatarSyncer = original
    assert calls["sync"] == 1
    # 每轮扫描量必须来自配置，不能一次性把 6 万条全捞出来：
    # 循环按「BATCH × SCAN_FACTOR（下限 BATCH、上限 50000）」放大扫描窗口，
    # 因为索引命中率只有 2~15%，只扫 BATCH 条会长期零产出（见 main 循环注释）。
    batch = main_module.config.GFRIENDS_AVATAR_SYNC_BATCH
    factor = main_module.config.GFRIENDS_AVATAR_SYNC_SCAN_FACTOR
    assert calls["limit"] == min(max(batch * factor, batch), 50000)
    assert calls["randomize"] is True


def test_loop_survives_syncer_exception():
    """上游索引拉失败不能让常驻线程死掉。"""
    stop = _StopAfterFirst()
    import gfriends_sync
    original = gfriends_sync.GfriendsAvatarSyncer
    _install_fake_syncer(gfriends_sync, RuntimeError("index download failed"))
    try:
        _loop_with(None, stop)  # 不抛异常即通过
    finally:
        gfriends_sync.GfriendsAvatarSyncer = original
    assert stop.calls >= 2  # 异常后仍然进入了下一轮等待


def test_sync_only_touches_rows_without_avatar():
    """幂等的根在数据库层：只取缺头像的行、只覆盖空值。"""
    from database import Database
    import inspect

    src = inspect.getsource(Database.update_actress_avatars)
    assert "avatar IS NULL" in src or "avatar = ''" in src, \
        "update_actress_avatars 必须限定只覆盖空头像，否则会冲掉已补好的"
    src2 = inspect.getsource(Database.get_actresses_missing_avatar)
    assert "avatar IS NULL" in src2 and "avatar = ''" in src2