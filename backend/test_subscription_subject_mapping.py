# -*- coding: utf-8 -*-
"""订阅 subject → 演员行的 id 映射回归测试。

2026-11-07 实测的断链点：订阅 url 的前缀和 `actresses.id` 的前缀对不上，
导致 161 条 person 订阅 + 26 条 netflav 订阅每轮都是
``actress_url_unresolved`` 秒跳过 —— 订阅功能对这些人从来没真正跑过。

| 订阅 url                              | actresses.id            |
|---------------------------------------|-------------------------|
| ``jellyfin:person:<hash>``             | ``jf_person:<hash>``     |
| ``jellyfin:actor:<名字>``              | ``jf_actor:<名字>``      |
| ``jellyfin:actress:netflav:<hash>``    | ``netflav:<hash>``       |
| ``jellyfin:actress:r18:<id>``          | ``r18:<id>``（走 source 查） |
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from subscription_service import SubscriptionService


class _FakeDb:
    """只实现被测到的几个查询方法。"""

    def __init__(self, rows=None):
        # rows: {actresses.id: row}
        self.rows = rows or {}

    def get_actress_by_source(self, source, source_id):
        for row in self.rows.values():
            if row.get("source") == source and row.get("source_id") == source_id:
                return row
        return None

    def get_actress(self, actress_id):
        return self.rows.get(actress_id)

    def get_actress_by_name(self, name):
        for row in self.rows.values():
            if row.get("name") == name:
                return row
        return None


def _service(rows):
    svc = SubscriptionService.__new__(SubscriptionService)
    svc.db = _FakeDb(rows)
    svc.logger = None
    svc.safe_exception_label = lambda exc: type(exc).__name__
    svc.display_value_for_log = lambda v: str(v)
    return svc


def test_extract_internal_subject():
    svc = _service({})
    assert svc._extract_internal_subject("jellyfin:person:abc") == "abc"
    assert svc._extract_internal_subject("jellyfin:actress:netflav:xyz") == "netflav:xyz"
    assert svc._extract_internal_subject("jellyfin:actor:Name") == "Name"
    assert svc._extract_internal_subject("https://example.com/x") is None


def test_person_prefix_maps_to_jf_person():
    """`jellyfin:person:<hash>` → `jf_person:<hash>`。"""
    rows = {"jf_person:abc123": {"id": "jf_person:abc123", "name": "三佳詩"}}
    svc = _service(rows)
    found = svc._actress_for_internal_subject("abc123")
    assert found is not None
    assert found["id"] == "jf_person:abc123"


def test_actor_prefix_maps_to_jf_actor():
    rows = {"jf_actor:SomeName": {"id": "jf_actor:SomeName", "name": "SomeName"}}
    svc = _service(rows)
    found = svc._actress_for_internal_subject("SomeName")
    assert found is not None
    assert found["id"] == "jf_actor:SomeName"


def test_netflav_prefix_maps_to_netflav_id():
    """`jellyfin:actress:netflav:<hash>` → `netflav:<hash>`。"""
    rows = {"netflav:xyz": {"id": "netflav:xyz", "name": "流川はる香"}}
    svc = _service(rows)
    found = svc._actress_for_internal_subject("netflav:xyz")
    assert found is not None
    assert found["id"] == "netflav:xyz"


def test_source_lookup_still_works():
    """`r18:<id>` 这类走 source:source_id 查询的老路径不能被破坏。"""
    rows = {
        "r18:1003052": {
            "id": "r18:1003052",
            "name": "森ゆきな",
            "source": "r18",
            "source_id": "1003052",
        }
    }
    svc = _service(rows)
    found = svc._actress_for_internal_subject("r18:1003052")
    assert found is not None
    assert found["id"] == "r18:1003052"


def test_name_fallback_still_works():
    """老格式按名字兜底不能被破坏。"""
    rows = {"r18:999": {"id": "r18:999", "name": "楪カレン"}}
    svc = _service(rows)
    found = svc._actress_for_internal_subject("楪カレン")
    assert found is not None
    assert found["name"] == "楪カレン"


def test_unresolvable_returns_none():
    svc = _service({})
    assert svc._actress_for_internal_subject("nope") is None


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("all passed")
