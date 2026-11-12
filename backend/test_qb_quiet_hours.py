"""qb_quiet_hours 单元测试（纯本地，不连真实 qBittorrent）。

覆盖：时间窗口判定（跨零点 / 同日 / 星期过滤）、下次切换点、
以及执行器在「进入窗口→用户手动恢复→窗口内新种子→退出窗口→重启恢复」这条链路上的行为。
"""
from datetime import datetime

import qb_quiet_hours as qh

ALL_DAYS = [1, 2, 3, 4, 5, 6, 7]


class FakeDB:
    def __init__(self, value=None):
        self.value = value

    def get_config(self, key):
        assert key == qh.CONFIG_KEY
        return self.value

    def set_config(self, key, value):
        assert key == qh.CONFIG_KEY
        # 模拟真实落库：必须能经过 json 序列化
        import json
        self.value = json.loads(json.dumps(value, ensure_ascii=False))


class FakeQB:
    """记录 pause/resume 调用，并可模拟「用户手动恢复了某个种子」。"""

    def __init__(self, torrents=None):
        self.torrents = list(torrents or [])
        self.pause_calls = []
        self.resume_calls = []
        self.pause_ok = True
        self.resume_ok = True

    def list_torrents(self):
        return list(self.torrents)

    def pause(self, hashes):
        self.pause_calls.append(list(hashes))
        if self.pause_ok:
            for item in self.torrents:
                if item["hash"] in hashes:
                    item["state"] = "pausedDL"
        return self.pause_ok

    def resume(self, hashes):
        self.resume_calls.append(list(hashes))
        if self.resume_ok:
            for item in self.torrents:
                if item["hash"] in hashes:
                    item["state"] = "downloading"
        return self.resume_ok


def make_controller(db, qb, now):
    return qh.QuietHoursController(
        db=db,
        list_torrents=qb.list_torrents,
        pause=qb.pause,
        resume=qb.resume,
        clock=lambda: now["at"],
    )


def test_parse_and_normalize():
    assert qh.parse_hhmm("23:30") == 23 * 60 + 30
    assert qh.parse_hhmm(" 7:05 ") == 7 * 60 + 5
    assert qh.parse_hhmm("24:00") is None
    assert qh.parse_hhmm("7点") is None
    assert qh.format_hhmm(1410) == "23:30"

    # end == start 是无效窗口；坏条目丢弃但不影响其它条目
    windows = qh.normalize_windows([
        {"start": "23:30", "end": "07:00", "days": [1, 2]},
        {"start": "10:00", "end": "10:00"},
        {"start": "bad", "end": "11:00"},
        "junk",
    ])
    assert windows == [{"start": "23:30", "end": "07:00", "days": [1, 2]}]

    # days 缺失 = 每天；最多 8 个窗口
    assert qh.normalize_windows([{"start": "01:00", "end": "02:00"}])[0]["days"] == ALL_DAYS
    many = [{"start": "%02d:00" % i, "end": "%02d:30" % i} for i in range(20)]
    assert len(qh.normalize_windows(many)) == qh.MAX_WINDOWS


def test_is_active_cross_midnight():
    window = {"start": "23:30", "end": "07:00", "days": ALL_DAYS}
    # 周一 23:45 与周二 03:00 都在窗口里
    assert qh.is_active(datetime(2026, 10, 26, 23, 45), [window]) is True
    assert qh.is_active(datetime(2026, 10, 27, 3, 0), [window]) is True
    # 左闭右开：23:30 算进入，07:00 已经退出
    assert qh.is_active(datetime(2026, 10, 26, 23, 30), [window]) is True
    assert qh.is_active(datetime(2026, 10, 27, 7, 0), [window]) is False
    assert qh.is_active(datetime(2026, 10, 27, 23, 0), [window]) is False


def test_is_active_same_day_and_weekday_filter():
    window = {"start": "13:00", "end": "14:00", "days": [1]}
    assert qh.is_active(datetime(2026, 10, 26, 13, 30), [window]) is True   # 周一
    assert qh.is_active(datetime(2026, 10, 27, 13, 30), [window]) is False  # 周二

    # 跨零点窗口的 days 指的是「开始日」：周日 23:30 起的那段算周日
    sunday = {"start": "23:30", "end": "02:00", "days": [7]}
    assert qh.is_active(datetime(2026, 11, 1, 23, 40), [sunday]) is True    # 周日
    assert qh.is_active(datetime(2026, 11, 2, 1, 0), [sunday]) is True      # 周一凌晨
    assert qh.is_active(datetime(2026, 11, 2, 23, 40), [sunday]) is False   # 周一晚


def test_next_change():
    windows = [{"start": "23:30", "end": "07:00", "days": ALL_DAYS}]
    # 未进入：下一次是今晚 23:30
    assert qh.next_change(datetime(2026, 10, 27, 12, 0), windows) == datetime(2026, 10, 27, 23, 30)
    # 静默中：下一次是今天早上 07:00
    assert qh.next_change(datetime(2026, 10, 27, 3, 0), windows) == datetime(2026, 10, 27, 7, 0)
    assert qh.next_change(datetime(2026, 10, 27, 3, 0), []) is None


def test_controller_pauses_then_resumes():
    now = {"at": datetime(2026, 10, 26, 23, 45)}
    db = FakeDB({"enabled": True, "windows": [{"start": "23:30", "end": "07:00", "days": ALL_DAYS}]})
    qb = FakeQB([
        {"hash": "aaa", "state": "downloading"},
        {"hash": "bbb", "state": "uploading"},
        {"hash": "ccc", "state": "pausedUP"},   # 本来就暂停，不该被算进暂停集
    ])
    ctl = make_controller(db, qb, now)

    report = ctl.tick()
    assert report["active"] is True
    assert sorted(qb.pause_calls[0]) == ["aaa", "bbb"]
    assert sorted(db.value["paused"]) == ["aaa", "bbb"]

    # 同一轮里不重复调用
    ctl.tick()
    assert len(qb.pause_calls) == 1

    # 出窗口 → 只恢复自己暂停的那批
    now["at"] = datetime(2026, 10, 27, 7, 0)
    report = ctl.tick()
    assert report["active"] is False
    assert qb.resume_calls == [["aaa", "bbb"]]
    assert db.value["paused"] == []


def test_controller_respects_manual_resume_and_new_arrivals():
    now = {"at": datetime(2026, 10, 26, 23, 45)}
    db = FakeDB({"enabled": True, "windows": [{"start": "23:30", "end": "07:00", "days": ALL_DAYS}]})
    qb = FakeQB([{"hash": "aaa", "state": "downloading"}])
    ctl = make_controller(db, qb, now)
    ctl.tick()
    assert sorted(db.value["paused"]) == ["aaa"]

    # 用户手动恢复了 aaa
    qb.torrents[0]["state"] = "downloading"
    # 同时队列里新进了一个种子
    qb.torrents.append({"hash": "ddd", "state": "downloading"})
    ctl.tick()

    assert "aaa" in db.value["released"]
    assert "aaa" not in db.value["paused"]
    assert "ddd" in db.value["paused"]
    # 之后不会再被按回去
    calls_before = len(qb.pause_calls)
    ctl.tick()
    assert len(qb.pause_calls) == calls_before
    assert qb.pause_calls and "aaa" not in qb.pause_calls[-1]

    # 出窗口时也不恢复 aaa（本来就是用户在跑）
    now["at"] = datetime(2026, 10, 27, 7, 0)
    ctl.tick()
    assert qb.resume_calls == [["ddd"]]


def test_controller_survives_restart_and_deleted_torrents():
    """重启后靠 config 里的暂停集继续恢复；已删除的种子不必再 resume。"""
    now = {"at": datetime(2026, 10, 27, 7, 0)}
    db = FakeDB({"enabled": True,
                 "windows": [{"start": "23:30", "end": "07:00", "days": ALL_DAYS}],
                 "paused": ["gone", "stay"]})
    qb = FakeQB([{"hash": "stay", "state": "pausedDL"}])   # gone 已被删除
    ctl = make_controller(db, qb, now)

    ctl.tick()
    assert qb.resume_calls == [["stay"]]
    assert db.value["paused"] == []


def test_controller_disabled_restores():
    now = {"at": datetime(2026, 10, 27, 3, 0)}
    db = FakeDB({"enabled": False,
                 "windows": [{"start": "23:30", "end": "07:00", "days": ALL_DAYS}],
                 "paused": ["aaa"]})
    qb = FakeQB([{"hash": "aaa", "state": "pausedDL"}])
    ctl = make_controller(db, qb, now)

    report = ctl.tick()
    assert report["active"] is False
    assert qb.resume_calls == [["aaa"]]
    assert db.value["paused"] == []


def test_controller_keeps_set_when_qb_unavailable():
    """qB 不可达时不丢暂停集：否则种子会永远停在暂停状态没人恢复。"""
    now = {"at": datetime(2026, 10, 26, 23, 45)}
    db = FakeDB({"enabled": True,
                 "windows": [{"start": "23:30", "end": "07:00", "days": ALL_DAYS}]})

    def boom():
        raise RuntimeError("qb_unavailable")

    ctl = qh.QuietHoursController(db=db, list_torrents=boom,
                                  pause=lambda h: True, resume=lambda h: True,
                                  clock=lambda: now["at"])
    report = ctl.tick()
    assert report["ok"] is False
    assert report["error"] == "list_failed"

    # 恢复阶段列表拉不到、但 resume 还能用：宁可全部恢复，也不能让种子一直停着
    now["at"] = datetime(2026, 10, 27, 7, 0)
    db.value["paused"] = ["aaa"]
    called = []
    ctl2 = qh.QuietHoursController(db=db, list_torrents=boom,
                                   pause=lambda h: True,
                                   resume=lambda h: called.append(list(h)) or True,
                                   clock=lambda: now["at"])
    report = ctl2.tick()
    assert called == [["aaa"]]
    assert report["ok"] is True
    assert db.value["paused"] == []

    # qB 彻底不可用（resume 也抛异常）：暂停集必须保留，下个 tick 再试
    db.value["paused"] = ["aaa"]

    def resume_boom(hashes):
        raise RuntimeError("qb_unavailable")

    ctl3 = qh.QuietHoursController(db=db, list_torrents=boom,
                                   pause=lambda h: True, resume=resume_boom,
                                   clock=lambda: now["at"])
    report = ctl3.tick()
    assert report["ok"] is False
    assert db.value["paused"] == ["aaa"]


def test_public_view_hides_hashes():
    now = datetime(2026, 10, 27, 3, 0)
    data = {
        "enabled": True,
        "windows": [{"start": "23:30", "end": "07:00", "days": ALL_DAYS}],
        "paused": ["aaa", "bbb"],
        "released": [],
        "last_change": None,
    }
    view = qh.public_view(data, now=now)
    assert view["state"]["active"] is True
    assert view["state"]["paused_count"] == 2
    assert view["state"]["next_change_action"] == "resume"
    assert "paused" not in view          # 不把 hash 列表暴露给前端
    assert view["max_windows"] == qh.MAX_WINDOWS
