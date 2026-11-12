"""投递落位确认与会话自愈单测（全部离线）。

背景（2026-10-27 生产事故）：qB 的 setLocation 是异步的，HTTP 200 只代表请求
被接受。40 个种子返回 200 后文件仍留在 /downloads/88888，调用方据此打了
「Delivered」标签，而 groom 见到该标签就永久跳过 —— 231 GB 卡了两天，
Jellyfin 一部新片都没进。
"""
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from download_service import DownloadService

DELIVERY = "/hp/_incoming"
DOWNLOAD_DIR = "/downloads/88888"


class _SessionExpired(Exception):
    """模拟 qb_api.QBSessionExpired（按类名判断，见 DownloadService._is_session_expired）。"""


_SessionExpired.__name__ = "QBSessionExpired"
_SessionExpired.__qualname__ = "QBSessionExpired"


class _FakeQB:
    """内存版 qb_api：可配置 relocate 是否真的改变 save_path。"""

    QBSessionExpired = _SessionExpired

    def __init__(self, *, relocate_lands=True, set_location_ok=True,
                 torrents=None, expire_first=False):
        self.relocate_lands = relocate_lands
        self.set_location_ok = set_location_ok
        self.torrents = list(torrents or [])
        self.expire_first = expire_first
        self.list_calls = 0
        self.move_calls = []
        self.tag_calls = []
        self.removed_tags = []

    def get_torrents(self, session, base_url, category=None, tag=None):
        self.list_calls += 1
        if self.expire_first and self.list_calls == 1:
            raise _SessionExpired("qb_session_expired:403")
        return [dict(t) for t in self.torrents]

    def get_torrents_by_hashes(self, session, base_url, hashes):
        wanted = set(hashes or [])
        out = []
        for torrent in self.torrents:
            if torrent.get("hash") not in wanted:
                continue
            item = dict(torrent)
            if self.relocate_lands:
                item["save_path"] = DELIVERY
                item["content_path"] = f"{DELIVERY}/{torrent.get('name')}"
                item["state"] = "pausedUP"
            out.append(item)
        return out

    def set_torrent_location(self, session, base_url, hashes, location):
        self.move_calls.append((list(hashes), location))
        return self.set_location_ok

    def add_tags(self, session, base_url, hashes, tags):
        self.tag_calls.append((list(hashes), tags))
        return True

    def remove_tags(self, session, base_url, hashes, tags):
        self.removed_tags.append((list(hashes), tags))
        return True

    def pause_torrents(self, session, base_url, hashes):
        return True

    def set_share_limits(self, *args, **kwargs):
        return True


class _StopEvent:
    def __init__(self):
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, _timeout=None):
        self.stopped = True
        return True


def _service(qb, *, stop_event=None, invalidate=None, probe=None):
    def _safe_number(value, default=0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _parse_int(value, default=0, minimum=None):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = int(default)
        if minimum is not None and parsed < minimum:
            parsed = minimum
        return parsed

    service = DownloadService(
        db=None,
        qb_api=qb,
        config=SimpleNamespace(),
        task_registry=None,
        logger=logging.getLogger("test_delivery"),
        stop_event=stop_event or _StopEvent(),
        normalize_magnet_url=lambda url: url,
        get_bool_config=lambda key, fallback: fallback,
        get_automation_mode=lambda: "manual",
        get_auto_download_daily_limit=lambda: 0,
        get_today_start_ts=lambda: 0,
        get_qb_session=lambda: (object(), "http://qb.local"),
        get_disk_threshold_bytes=lambda: 0,
        safe_exception_label=lambda exc: type(exc).__name__,
        display_value_for_log=str,
        display_path_for_log=str,
        safe_number=_safe_number,
        parse_int_value=_parse_int,
        extract_movie_id_from_text=lambda text: None,
        get_qb_delivery_folder=lambda: DELIVERY,
        invalidate_qb_session=invalidate,
        delivery_path_probe=probe,
    )
    service.delivery_verify_poll_seconds = 0.01
    service.delivery_verify_timeout_seconds = 0.2
    return service


def _torrent(**overrides):
    base = {
        "hash": "a" * 40,
        "name": "SNOS-339",
        "save_path": DOWNLOAD_DIR,
        "content_path": f"{DOWNLOAD_DIR}/SNOS-339",
        "state": "pausedUP",
        "progress": 1,
    }
    base.update(overrides)
    return base


# ---------- delivered_really_landed ----------

def test_landed_true_when_save_path_in_delivery_folder():
    service = _service(_FakeQB())
    assert service.delivered_really_landed(
        _torrent(save_path=f"{DELIVERY}/SNOS-339"), DELIVERY
    ) is True


def test_landed_true_when_only_content_path_in_delivery_folder():
    service = _service(_FakeQB())
    assert service.delivered_really_landed(
        _torrent(content_path=f"{DELIVERY}/SNOS-339/SNOS-339.mp4"), DELIVERY
    ) is True


def test_landed_false_when_still_in_download_dir():
    """这就是那 40 个种子的形态：标签打了，路径没动。"""
    service = _service(_FakeQB())
    assert service.delivered_really_landed(_torrent(), DELIVERY) is False


def test_landed_false_for_sibling_dir_with_common_prefix():
    """`/hp/_incoming_old` 不能因为字符串前缀像就判成 `/hp/_incoming` 里。"""
    service = _service(_FakeQB())
    assert service.delivered_really_landed(
        _torrent(save_path="/hp/_incoming_old"), DELIVERY
    ) is False


def test_landed_true_when_delivery_folder_not_configured():
    """没配投递目录时无从判断，保持旧行为，不误摘标签。"""
    service = _service(_FakeQB())
    assert service.delivered_really_landed(_torrent(), "") is True


def test_probe_rescues_torrent_whose_files_were_moved_away():
    """qB 说还在原目录、但原文件已不在 —— 说明是被外部搬走的，不该重投。"""
    service = _service(_FakeQB(), probe=lambda path: False)
    assert service.delivered_really_landed(_torrent(), DELIVERY) is True


def test_probe_keeps_false_when_files_really_still_there():
    service = _service(_FakeQB(), probe=lambda path: True)
    assert service.delivered_really_landed(_torrent(), DELIVERY) is False


# ---------- deliver_torrent ----------

def test_deliver_returns_true_only_after_qb_reports_new_path():
    qb = _FakeQB(relocate_lands=True)
    torrent = _torrent()
    qb.torrents = [torrent]
    assert _service(qb).deliver_torrent(object(), "http://qb.local", torrent, DELIVERY) is True
    assert qb.move_calls == [(["a" * 40], DELIVERY)]


def test_deliver_returns_false_when_relocate_does_not_land():
    """setLocation 返回 200 但文件没动 —— 必须判失败，绝不能打标签。

    这正是生产上 231 GB 卡死的成因：200 被当成成功。
    """
    qb = _FakeQB(relocate_lands=False)
    torrent = _torrent()
    qb.torrents = [torrent]
    assert _service(qb).deliver_torrent(object(), "http://qb.local", torrent, DELIVERY) is False


def test_deliver_skips_verification_when_set_location_rejected():
    qb = _FakeQB(set_location_ok=False)
    torrent = _torrent()
    qb.torrents = [torrent]
    assert _service(qb).deliver_torrent(object(), "http://qb.local", torrent, DELIVERY) is False
    assert qb.move_calls == [(["a" * 40], DELIVERY)]


def test_deliver_stops_when_stop_event_set():
    stop = _StopEvent()
    stop.stopped = True
    qb = _FakeQB(relocate_lands=True)
    torrent = _torrent()
    qb.torrents = [torrent]
    assert _service(qb, stop_event=stop).deliver_torrent(
        object(), "http://qb.local", torrent, DELIVERY
    ) is False


# ---------- 会话自愈 ----------

def test_fetch_torrents_rebuilds_session_after_403():
    rebuilt = []
    qb = _FakeQB(torrents=[_torrent()], expire_first=True)
    service = _service(qb, invalidate=lambda: rebuilt.append(True))

    session, base_url, torrents = service.fetch_torrents(tag="jav")

    assert rebuilt == [True], "403 必须触发会话重建"
    assert len(torrents) == 1, "重建后要拿到真实列表，而不是空列表"


def test_fetch_torrents_reraises_unrelated_errors():
    class _Boom(Exception):
        pass

    qb = _FakeQB()
    qb.get_torrents = lambda *a, **kw: (_ for _ in ()).throw(_Boom("timeout"))
    service = _service(qb, invalidate=lambda: None)

    try:
        service.fetch_torrents(tag="jav")
    except _Boom:
        return
    raise AssertionError("非会话类异常不该被吞掉")


# ---------- 加种即选片（只下最大文件，垃圾一个字节都不下） ----------

MAGNET = "magnet:?xt=urn:btih:f62a5bd884587d0c2c3cec367d583600f3faf388&dn=JUR-836"


class _PolicyQB(_FakeQB):
    """可配置文件列表的 qb_api：记录 filePrio / resume 调用。"""

    def __init__(self, files=None, *, raise_on_files=False):
        super().__init__()
        self.files = list(files or [])
        self.raise_on_files = raise_on_files
        self.priority_calls = []
        self.resume_calls = []

    def get_torrent_files(self, session, base_url, torrent_hash):
        if self.raise_on_files:
            raise RuntimeError("qb down")
        return [dict(f) for f in self.files]

    def set_file_priority(self, session, base_url, torrent_hash, file_ids, priority):
        self.priority_calls.append((list(file_ids), priority))
        return True

    def resume_torrents(self, session, base_url, hashes):
        self.resume_calls.append(list(hashes))
        return True


def _policy_service(qb):
    service = _service(qb)
    service.post_add_metadata_timeout_seconds = 0.05
    service.post_add_metadata_poll_seconds = 0.01
    return service


def _file(index, name, size):
    return {"index": index, "name": name, "size": size, "priority": 0, "progress": 0}


def test_magnet_infohash_parses_btih():
    assert DownloadService.magnet_infohash(MAGNET) == "f62a5bd884587d0c2c3cec367d583600f3faf388"
    assert DownloadService.magnet_infohash("http://example.com/a.torrent") is None
    assert DownloadService.magnet_infohash(None) is None


def test_post_add_policy_keeps_only_largest_file_and_resumes():
    """种子里 20G 正片 + 13M 广告 + txt，只能留正片，其余 priority 归 0。"""
    qb = _PolicyQB(files=[
        _file(0, "JUR-836/489155.com@JUR-836.mp4", 25 * 1024 ** 3),
        _file(1, "JUR-836/台湾uu美少女直播.mp4", 13 * 1024 ** 2),
        _file(2, "JUR-836/最新地址.txt", 136),
    ])
    service = _policy_service(qb)

    result = service.apply_post_add_file_policy(object(), "http://qb.local",
                                                "f62a5bd884587d0c2c3cec367d583600f3faf388")

    assert result["filtered"] is True, result
    assert result["kept_file"].endswith("489155.com@JUR-836.mp4")
    disabled = [ids for ids, prio in qb.priority_calls if prio == 0][0]
    assert sorted(disabled) == [1, 2], "广告和 txt 必须被置 0"
    assert [ids for ids, prio in qb.priority_calls if prio == 1] == [[0]]
    assert qb.resume_calls == [["f62a5bd884587d0c2c3cec367d583600f3faf388"]], "选完片必须恢复"


def test_post_add_policy_resumes_when_metadata_never_arrives():
    """拿不到元数据也要恢复，否则种子永远挂在暂停态。"""
    qb = _PolicyQB(files=[])
    service = _policy_service(qb)

    result = service.apply_post_add_file_policy(object(), "http://qb.local", "b" * 40)

    assert result["filtered"] is False
    assert result["reason"] == "metadata_timeout"
    assert qb.resume_calls == [["b" * 40]]


def test_post_add_policy_resumes_when_file_list_read_fails():
    """读文件列表报错不该干等 30 秒，立刻恢复并交给周期过滤。"""
    qb = _PolicyQB(raise_on_files=True)
    service = _policy_service(qb)

    result = service.apply_post_add_file_policy(object(), "http://qb.local", "c" * 40)

    assert result["reason"] == "metadata_read_failed"
    assert qb.resume_calls == [["c" * 40]]


def test_post_add_policy_disables_everything_when_no_file_big_enough():
    """整包都是广告时把文件全置 0，交给 junk 种子删除逻辑。"""
    qb = _PolicyQB(files=[
        _file(0, "SNOS-001/ad.mp4", 10 * 1024 ** 2),
        _file(1, "SNOS-001/readme.txt", 100),
    ])
    service = _policy_service(qb)

    result = service.apply_post_add_file_policy(object(), "http://qb.local", "d" * 40)

    assert result["filtered"] is False
    assert result["reason"] == "no_video_over_400mb"
    assert [ids for ids, prio in qb.priority_calls if prio == 0] == [[0, 1]]
    assert qb.resume_calls == [["d" * 40]]
