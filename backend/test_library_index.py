"""已入库索引（番号 -> Jellyfin 条目 ID）：卡片靠它决定显示"播放"还是"下载"。"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import main


class FakeJellyfinClient:
    """按调用次序返回预置分页，并记录每次调用的 offset。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_library_items(
        self, _library_id, _played, _favorite, _sort_by, _sort_order, _search, limit, offset, fields=None,
    ):
        self.calls.append({"limit": limit, "offset": offset, "fields": fields})
        index = len(self.calls) - 1
        return self.pages[index] if index < len(self.pages) else {"items": [], "total": 0}


def _item(title, jellyfin_id):
    return {"jellyfin_id": jellyfin_id, "title": title}


@pytest.fixture(autouse=True)
def reset_index_state():
    main._library_index_state.update({"at": 0.0, "index": {}, "building": False})
    yield
    main._library_index_state.update({"at": 0.0, "index": {}, "building": False})


def test_build_index_maps_number_to_jellyfin_id(monkeypatch):
    client = FakeJellyfinClient([{
        "items": [
            _item("MBMH-097 デカい！デカ過ぎる！！", "id-097"),
            _item("FHD_6M-SSNI-658A", "id-658"),
            _item("JUR-595_UNC", "id-595"),
        ],
        "total": 3,
    }])
    monkeypatch.setattr(main, "jellyfin_client", client)

    index = main._build_library_index()

    assert index == {"MBMH-097": "id-097", "SSNI-658": "id-658", "JUR-595": "id-595"}
    # 只要 Id + Name：带上 People/MediaSources 会让每页多花几秒
    assert client.calls[0]["fields"] == "Name"


def test_build_index_paginates_until_total_reached(monkeypatch):
    client = FakeJellyfinClient([
        {"items": [_item("AAA-001", "id-1")] * main.LIBRARY_INDEX_PAGE_SIZE, "total": 1200},
        {"items": [_item("AAA-002", "id-2")] * main.LIBRARY_INDEX_PAGE_SIZE, "total": 1200},
        {"items": [_item("AAA-003", "id-3")] * 200, "total": 1200},
    ])
    monkeypatch.setattr(main, "jellyfin_client", client)

    index = main._build_library_index()

    assert [call["offset"] for call in client.calls] == [0, 500, 1000]
    assert index == {"AAA-001": "id-1", "AAA-002": "id-2", "AAA-003": "id-3"}


def test_build_index_skips_unparsable_and_id_less_items(monkeypatch):
    """解析不出番号不能把标题冒充番号；没有条目 ID 的条目播不了，一并跳过。"""
    client = FakeJellyfinClient([{
        "items": [
            _item("たけのこ", "id-jp"),
            _item("BBB-002", ""),
            _item("BBB-003", "id-3"),
        ],
        "total": 3,
    }])
    monkeypatch.setattr(main, "jellyfin_client", client)

    assert main._build_library_index() == {"BBB-003": "id-3"}


def test_endpoint_reuses_cache_within_ttl(monkeypatch):
    client = FakeJellyfinClient([{"items": [_item("CCC-001", "id-1")], "total": 1}])
    monkeypatch.setattr(main, "jellyfin_client", client)

    first = asyncio.run(main.get_library_index(refresh=False))
    assert first["success"] is True and first["count"] == 1 and first["index"] == {"CCC-001": "id-1"}

    second = asyncio.run(main.get_library_index(refresh=False))
    assert second["cached"] is True
    assert len(client.calls) == 1


def test_endpoint_falls_back_to_stale_index_on_error(monkeypatch):
    """Jellyfin 抽风时退回旧索引，不要把卡片全刷成"未入库"。"""
    client = FakeJellyfinClient([{"items": [_item("DDD-001", "id-1")], "total": 1}])
    monkeypatch.setattr(main, "jellyfin_client", client)
    asyncio.run(main.get_library_index(refresh=True))
    main._library_index_state["at"] = 0.0

    def boom(*_args, **_kwargs):
        raise RuntimeError("jellyfin_down")

    monkeypatch.setattr(client, "get_library_items", boom)
    result = asyncio.run(main.get_library_index(refresh=True))

    assert result["success"] is False
    assert result["index"] == {"DDD-001": "id-1"}
    assert main._library_index_state["building"] is False
