"""400MB 碎片策略的跨环节测试（media_policy / junk_sweep_service / qB 补扫）。"""
import logging
from types import SimpleNamespace

import media_policy
from junk_sweep_service import (
    JunkSweepService,
    pick_undersized_jellyfin_items,
    pick_undersized_media,
)

MB = 1024 * 1024


# ----------------------------- 策略本体 -----------------------------

def test_undersized_video_boundaries():
    assert media_policy.is_undersized_video("ad.mp4", 100 * MB) is True
    assert media_policy.is_undersized_video("ad.MP4", 399 * MB) is True
    # 正好 400MB 不算碎片（阈值是「小于」）
    assert media_policy.is_undersized_video("movie.mp4", 400 * MB) is False
    assert media_policy.is_undersized_video("movie.mkv", 2 * 1024 * MB) is False


def test_undersized_video_ignores_unknown_size_and_non_video():
    assert media_policy.is_undersized_video("movie.mp4", 0) is False
    assert media_policy.is_undersized_video("movie.mp4", None) is False
    assert media_policy.is_undersized_video("readme.txt", 1 * MB) is False
    assert media_policy.is_undersized_video("poster.jpg", 2 * MB) is False


def test_undersized_video_ignores_incomplete_downloads():
    assert media_policy.is_undersized_video("movie.mp4.!qB", 100 * MB) is False
    assert media_policy.is_undersized_video("movie.mkv.part", 100 * MB) is False


def test_min_video_bytes_follows_env(monkeypatch):
    monkeypatch.setenv("JUNK_MIN_VIDEO_MB", "1024")
    assert media_policy.min_video_bytes() == 1024 * MB
    monkeypatch.setenv("JUNK_MIN_VIDEO_MB", "abc")
    assert media_policy.min_video_bytes() == media_policy.MIN_PLAYABLE_VIDEO_BYTES


# ----------------------------- 拣选（纯函数） -----------------------------

def test_pick_undersized_media_skips_unknown_size_and_non_video():
    items = [
        {"id": 1, "path": "/hp/A/ad.mp4", "size": 20 * MB, "number": "A-001"},
        {"id": 2, "path": "/hp/B/movie.mp4", "size": 900 * MB, "number": "B-002"},
        {"id": 3, "path": "/hp/C/movie.mp4", "size": None, "number": "C-003"},
        {"id": 4, "path": "/hp/D/readme.txt", "size": 1 * MB, "number": None},
    ]
    picked = pick_undersized_media(items, 400 * MB)
    assert [p["id"] for p in picked] == [1]
    assert picked[0]["size"] == 20 * MB


def test_pick_undersized_jellyfin_items_uses_media_sources_fallback():
    items = [
        {"Id": "a" * 32, "Name": "ad", "Path": "/hp/A/ad.mp4", "Size": 0,
         "MediaSources": [{"Size": 12 * MB}]},
        {"Id": "b" * 32, "Name": "movie", "Path": "/hp/B/movie.mp4", "Size": 1200 * MB},
        {"Id": "c" * 32, "Name": "unknown", "Path": "/hp/C/x.mp4", "Size": 0},
    ]
    picked = pick_undersized_jellyfin_items(items, 400 * MB)
    assert [p["id"] for p in picked] == ["a" * 32]


# ----------------------------- 索引层服务 -----------------------------

class _FakeAmane:
    def __init__(self, items, fail_ids=()):
        self.items = items
        self.deleted = []
        self.fail_ids = set(fail_ids)
        self.list_calls = []

    def list_media(self, *, limit=200, offset=0, **_kwargs):
        self.list_calls.append((limit, offset))
        return {"items": self.items[offset:offset + limit], "total": len(self.items)}

    def delete_media(self, media_id):
        if media_id in self.fail_ids:
            raise RuntimeError("amane_500")
        self.deleted.append(media_id)
        return True


class _FakeJellyfin:
    def __init__(self, items, fail_ids=()):
        self.items = items
        self.deleted = []
        self.fail_ids = set(fail_ids)

    def list_library_items_raw(self, fields=None):
        return self.items

    def delete_items(self, item_ids):
        for item_id in item_ids:
            if item_id in self.fail_ids:
                return {"success": False, "results": [{"id": item_id, "success": False}]}
            self.deleted.append(item_id)
        return {"success": True, "results": [{"id": i, "success": True} for i in item_ids]}


def test_sweep_dry_run_only_lists():
    amane = _FakeAmane([{"id": 7, "path": "/hp/A/ad.mp4", "size": 10 * MB}])
    jellyfin = _FakeJellyfin([{"Id": "f" * 32, "Path": "/hp/A/ad.mp4", "Size": 10 * MB}])
    report = JunkSweepService(
        amane_client=amane, jellyfin_client=jellyfin, logger=logging.getLogger("t")
    ).sweep(dry_run=True)

    assert report["dry_run"] is True
    assert report["matched"] == 2
    assert report["deleted"] == 0
    assert amane.deleted == [] and jellyfin.deleted == []


def test_sweep_deletes_entries_from_both_indexes():
    amane = _FakeAmane([
        {"id": 7, "path": "/hp/A/ad.mp4", "size": 10 * MB},
        {"id": 8, "path": "/hp/B/movie.mp4", "size": 800 * MB},
    ])
    jellyfin = _FakeJellyfin([
        {"Id": "f" * 32, "Path": "/hp/A/ad.mp4", "Size": 10 * MB},
        {"Id": "e" * 32, "Path": "/hp/B/movie.mp4", "Size": 800 * MB},
    ])
    report = JunkSweepService(
        amane_client=amane, jellyfin_client=jellyfin, logger=logging.getLogger("t")
    ).sweep(dry_run=False)

    assert report["deleted"] == 2
    assert amane.deleted == [7]
    assert jellyfin.deleted == ["f" * 32]
    assert report["amane"]["scanned"] == 2
    assert report["jellyfin"]["scanned"] == 2


def test_sweep_survives_single_delete_failure():
    amane = _FakeAmane(
        [{"id": 7, "path": "/hp/A/ad.mp4", "size": 10 * MB}], fail_ids={7}
    )
    report = JunkSweepService(amane_client=amane, logger=logging.getLogger("t")).sweep(
        dry_run=False
    )
    assert report["amane"]["deleted"] == 0
    assert report["amane"]["failed"] == 1
    assert report["amane"]["errors"] == ["7:RuntimeError"]


def test_sweep_pages_through_more_than_one_page():
    items = [
        {"id": i, "path": "/hp/A/movie.mp4", "size": 900 * MB} for i in range(250)
    ]
    items.append({"id": 999, "path": "/hp/A/ad.mp4", "size": 5 * MB})
    amane = _FakeAmane(items)
    report = JunkSweepService(amane_client=amane, logger=logging.getLogger("t")).sweep(
        dry_run=True
    )
    assert report["amane"]["scanned"] == 251
    assert len(amane.list_calls) >= 2
    assert report["amane"]["matched"] == 1


def test_sweep_marks_missing_clients_as_skipped():
    report = JunkSweepService(logger=logging.getLogger("t")).sweep(dry_run=False)
    assert report["amane"]["skipped"] == "amane_not_configured"
    assert report["jellyfin"]["skipped"] == "jellyfin_not_configured"


# ----------------------------- qB 补扫 -----------------------------

class _FakeQb:
    def __init__(self, torrents, files_by_hash):
        self.torrents = torrents
        self.files_by_hash = files_by_hash
        self.deleted = []

    def get_torrents(self, session, base_url, category=None, tag=None):
        return list(self.torrents)

    def get_torrent_files(self, session, base_url, torrent_hash):
        return list(self.files_by_hash.get(torrent_hash, []))

    def set_file_priority(self, session, base_url, torrent_hash, file_ids, priority):
        return True

    def add_tags(self, session, base_url, hashes, tags):
        return True

    def delete_torrents(self, session, base_url, hashes, delete_files=False):
        self.deleted.append((list(hashes), delete_files))
        return True


def _service(qb, *, delete_junk=True):
    from download_service import DownloadService

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

    return DownloadService(
        db=None,
        qb_api=qb,
        config=SimpleNamespace(JUNK_DELETE_TORRENTS=delete_junk),
        task_registry=None,
        logger=logging.getLogger("t"),
        stop_event=None,
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
        get_qb_delivery_folder=lambda: None,
    )


def test_completed_junk_only_torrent_is_deleted_with_files():
    junk_hash = "a" * 40
    good_hash = "b" * 40
    qb = _FakeQb(
        torrents=[
            {"hash": junk_hash, "name": "AD PACK", "tags": "jav", "progress": 1, "total_size": 50 * MB},
            {"hash": good_hash, "name": "ABC-123", "tags": "jav", "progress": 1, "total_size": 900 * MB},
        ],
        files_by_hash={
            junk_hash: [
                {"index": 0, "name": "ad1.mp4", "size": 20 * MB},
                {"index": 1, "name": "ad2.mp4", "size": 10 * MB},
            ],
            good_hash: [{"index": 0, "name": "ABC-123.mp4", "size": 900 * MB}],
        },
    )
    report = _service(qb).delete_junk_torrents_once()

    assert report["deleted"] == 1
    assert qb.deleted == [([junk_hash], True)]


def test_completed_torrent_with_big_file_is_kept():
    qb = _FakeQb(
        torrents=[{"hash": "b" * 40, "name": "ABC-123", "tags": "jav", "progress": 1, "total_size": 900 * MB}],
        files_by_hash={"b" * 40: [{"index": 0, "name": "ABC-123.mp4", "size": 900 * MB}]},
    )
    assert _service(qb).delete_junk_torrents_once()["deleted"] == 0
    assert qb.deleted == []


def test_in_progress_and_tagged_torrents_are_skipped():
    qb = _FakeQb(
        torrents=[
            {"hash": "c" * 40, "name": "downloading", "tags": "jav", "progress": 0.3, "total_size": 50 * MB},
            {"hash": "d" * 40, "name": "already", "tags": "jav,smart_filter_400mb", "progress": 1, "total_size": 50 * MB},
        ],
        files_by_hash={
            "c" * 40: [{"index": 0, "name": "x.mp4", "size": 10 * MB}],
            "d" * 40: [{"index": 0, "name": "x.mp4", "size": 10 * MB}],
        },
    )
    report = _service(qb).delete_junk_torrents_once()
    assert report["scanned"] == 0
    assert qb.deleted == []


def test_junk_torrent_kept_when_auto_delete_disabled():
    junk_hash = "a" * 40
    qb = _FakeQb(
        torrents=[{"hash": junk_hash, "name": "AD", "tags": "jav", "progress": 1, "total_size": 50 * MB}],
        files_by_hash={junk_hash: [{"index": 0, "name": "ad.mp4", "size": 10 * MB}]},
    )
    report = _service(qb, delete_junk=False).delete_junk_torrents_once()
    assert report["deleted"] == 0
    assert report["scanned"] == 1
    assert qb.deleted == []
