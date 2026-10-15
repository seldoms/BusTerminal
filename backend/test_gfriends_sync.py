"""gfriends 头像同步单测（全部离线，不发网络请求）。"""

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from gfriends_sync import GfriendsAvatarSyncer, build_index, normalize_actress_name

_TREE = {
    "Content": {
        "1-A": {"森沢かな.jpg": "森沢かな.jpg", "大槻ひびき.jpg": "大槻ひびき.jpg"},
        "0-Z": {"dup.jpg": "dup.jpg"},
    }
}


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []

    def get_config(self, key):
        return None

    def get_actresses_missing_avatar(self, limit=None):
        return self.rows if not limit else self.rows[:limit]

    def update_actress_avatars(self, updates):
        self.updates = list(updates)
        return len(updates)


def test_normalize_strips_parentheses_and_spaces():
    assert normalize_actress_name("森沢かな（飯岡かなこ）") == "森沢かな"
    assert normalize_actress_name(" めぐり (藤浦めぐ) ") == "めぐり"
    assert normalize_actress_name("AI KA") == "AIKA"
    assert normalize_actress_name("") == ""
    assert normalize_actress_name(None) == ""


def test_build_index_maps_names_and_first_folder_wins():
    index = build_index(_TREE)
    assert set(index) == {"森沢かな", "大槻ひびき", "dup"}
    assert index["森沢かな"].endswith("/Content/1-A/%E6%A3%AE%E6%B2%A2%E3%81%8B%E3%81%AA.jpg")
    assert index["dup"].endswith("/Content/0-Z/dup.jpg")


def test_build_index_handles_malformed_input():
    assert build_index({}) == {}
    assert build_index({"Content": []}) == {}
    assert build_index({"Content": {"x": "not-a-dict"}}) == {}


def test_sync_dry_run_does_not_write(tmp_path):
    db = _FakeDb(
        [
            {"id": "a1", "name": "森沢かな（飯岡かなこ）", "alias_names": ""},
            {"id": "a2", "name": "未知演员", "alias_names": "大槻ひびき"},
            {"id": "a3", "name": "查无此人", "alias_names": ""},
        ]
    )
    syncer = GfriendsAvatarSyncer(db, data_dir=str(tmp_path), fetch_index=lambda: _TREE)
    report = syncer.sync(dry_run=True)

    assert report["candidates"] == 3
    assert report["matched"] == 2
    assert report["applied"] == 0
    assert db.updates == []


def test_sync_applies_updates_and_writes_cache(tmp_path):
    db = _FakeDb([{"id": "a1", "name": "森沢かな", "alias_names": ""}])
    syncer = GfriendsAvatarSyncer(db, data_dir=str(tmp_path), fetch_index=lambda: _TREE)
    report = syncer.sync()

    assert report["matched"] == 1
    assert report["applied"] == 1
    assert db.updates == [("a1", report["samples"][0]["avatar"])]
    assert os.path.exists(syncer.cache_path)


def test_load_index_reuses_cache_across_instances(tmp_path):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return _TREE

    db = _FakeDb([])
    first = GfriendsAvatarSyncer(db, data_dir=str(tmp_path), fetch_index=fetch)
    index = first.load_index()
    second = GfriendsAvatarSyncer(db, data_dir=str(tmp_path), fetch_index=fetch)
    cached = second.load_index()

    assert calls["n"] == 1
    assert "森沢かな" in index and "森沢かな" in cached


def test_sync_honours_limit(tmp_path):
    db = _FakeDb(
        [
            {"id": "a1", "name": "森沢かな", "alias_names": ""},
            {"id": "a2", "name": "大槻ひびき", "alias_names": ""},
        ]
    )
    syncer = GfriendsAvatarSyncer(db, data_dir=str(tmp_path), fetch_index=lambda: _TREE)
    report = syncer.sync(limit=1)

    assert report["candidates"] == 1
    assert report["matched"] == 1
