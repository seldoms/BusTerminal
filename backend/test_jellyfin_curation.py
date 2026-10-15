"""Jellyfin 清洗服务单测。

重点覆盖 2026-10-13 生产实测踩到的三个坑：
1. update_item_metadata 必须走完整 payload（局部 payload 会写坏 Genres）；
2. upload_item_image 的 body 必须是 base64；
3. 扫描纯只读，不发任何写请求。
"""
import base64
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))

import pytest

from jellyfin_api import (
    CURATION_ITEM_FIELDS,
    FULL_ITEM_UPDATE_FIELDS,
    build_full_item_update_payload,
    is_valid_jellyfin_item_id,
)
import jellyfin_curation_service as curation
from jellyfin_curation_service import (
    JELLYFIN_CURATION_CONFIRMATION,
    JellyfinCurationService,
    extract_item_number,
    has_kana,
    normalize_code,
    normalize_item_ids,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""
        self.content = b""

    def json(self):
        return self._payload if self._payload is not None else {}


class FakeSession:
    """记录所有请求，便于断言「有没有发写请求」。"""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def _resolve(self, method, url):
        self.calls.append((method, url))
        for (m, fragment), response in self.responses.items():
            if m == method and fragment in url:
                return response
        return FakeResponse(200, {})

    def get(self, url, headers=None, params=None, timeout=None):
        return self._resolve("GET", url)

    def post(self, url, headers=None, params=None, json=None, data=None, timeout=None):
        return self._resolve("POST", url)


@pytest.fixture
def fake_client(monkeypatch):
    client = curation.jellyfin_client.__class__()
    client.host = "http://jf.local:8096"
    client.api_key = "key"
    client.user_id = "user1"
    client.reload_config = lambda: None
    return client


def test_normalize_code_matches_movies_code_norm():
    # 生产实测：movies.code_norm 走 movie_code.normalize_movie_code，数字段要去前导零。
    # 这里必须复用同一函数，自己另写一套会把 MP-BD0006 算成 MPBD0006 而永远匹配不上。
    assert normalize_code("MP-BD0006") == "MPBD6"
    assert normalize_code("sns-461dod") == "SNS461DOD"
    assert normalize_code("SSD-000003") == "SSD3"
    assert normalize_code(None) == ""


def test_extract_item_number_prefers_path_over_name():
    item = {
        "Name": "友達の母親〜最終章〜 坂元ななせ",
        "MediaSources": [{"Path": "/hp/HTHD-170/HTHD-170.mp4"}],
    }
    assert extract_item_number(item) == "HTHD-170"


def test_extract_item_number_falls_back_to_name():
    assert extract_item_number({"Name": "JUQ-873"}) == "JUQ-873"
    assert extract_item_number({"Name": "无法识别番号的标题"}) is None


def test_has_kana_detects_long_vowel_mark():
    # U+30FC 长音符必须算假名，否则「映画」类标题会漏判
    assert has_kana("ムadder")
    assert has_kana("ム team's ー")
    assert not has_kana("已经翻译好的中文标题")


def test_build_full_item_update_payload_never_leaves_collections_null():
    """坑 1：缺字段会导致 Jellyfin 500 并写坏数据，所以每个集合字段都要兜底。"""
    payload = build_full_item_update_payload({"Name": "X", "Genres": ["a", None]})
    for field in ("Taglines", "Genres", "Tags", "Studios", "LockedFields"):
        assert isinstance(payload[field], list), field
    assert payload["ProviderIds"] == {}
    # Genres 里的 null 元素要剔掉，它就是 AttachGenreItems 500 的直接原因
    assert payload["Genres"] == ["a"]
    for field in FULL_ITEM_UPDATE_FIELDS:
        assert field in payload, field


def test_build_full_item_update_payload_keeps_all_fields():
    source = {field: "v" for field in FULL_ITEM_UPDATE_FIELDS}
    payload = build_full_item_update_payload(source)
    assert set(payload) == set(FULL_ITEM_UPDATE_FIELDS)


def test_curation_fields_exclude_heavy_relations():
    # 带上 People/MediaSources 会让每页慢 3~4 秒，全库扫描扛不住
    assert "People" not in CURATION_ITEM_FIELDS
    assert "Name" in CURATION_ITEM_FIELDS
    assert "ImageTags" in CURATION_ITEM_FIELDS


def test_normalize_item_ids_rejects_invalid():
    assert normalize_item_ids("not-a-list") == ([], "missing_or_invalid_item_ids")
    assert normalize_item_ids(["ok", ""]) == ([], "invalid_item_ids")
    assert normalize_item_ids(["a", "a", "b"]) == (["a", "b"], None)


def test_validate_execute_request_blocks_without_switch():
    service = JellyfinCurationService(client=object())
    result = service.validate_execute_request(
        ["item1"], confirmation=JELLYFIN_CURATION_CONFIRMATION,
        enabled=False, dry_run=False,
    )
    assert result["success"] is False
    assert result["message"] == "jellyfin_curation_disabled"


def test_validate_execute_request_requires_confirmation():
    service = JellyfinCurationService(client=object())
    result = service.validate_execute_request(
        ["item1"], confirmation="wrong", enabled=True, dry_run=False,
    )
    assert result["success"] is False
    assert "missing_confirmation_phrase" in result["message"]


def test_validate_execute_request_enforces_batch_limit():
    service = JellyfinCurationService(client=object())
    ids = [f"item{i}" for i in range(101)]
    result = service.validate_execute_request(
        ids, confirmation=JELLYFIN_CURATION_CONFIRMATION, enabled=True, dry_run=False,
    )
    assert result["success"] is False
    assert "curation_item_limit_exceeded" in result["message"]


def test_validate_execute_request_allows_dry_run_without_switch():
    service = JellyfinCurationService(client=object())
    result = service.validate_execute_request(
        ["item1"], confirmation="", enabled=False, dry_run=True,
    )
    assert result["success"] is True
    assert result["item_ids"] == ["item1"]


def test_upload_item_image_sends_base64_body(fake_client, monkeypatch):
    """坑 2：body 必须是 base64 文本，裸二进制会让服务端 FromBase64Transform 崩。"""
    captured = {}

    class CaptureSession:
        def post(self, url, headers=None, params=None, json=None, data=None, timeout=None):
            captured["url"] = url
            captured["content_type"] = (headers or {}).get("Content-Type")
            captured["data"] = data
            captured["params"] = params
            return FakeResponse(204)

    monkeypatch.setattr(curation, "jellyfin_client", fake_client)
    monkeypatch.setattr("jellyfin_api.internal_session", lambda: CaptureSession())

    raw = b"\xff\xd8\xff\xe0 fake jpeg bytes"
    result = fake_client.upload_item_image("item123", raw)

    assert result["success"] is True
    assert captured["content_type"] == "image/jpeg"
    # body 必须是 base64 文本，不是二进制
    assert isinstance(captured["data"], str)
    assert base64.b64decode(captured["data"]) == raw
    assert captured["params"] == {"index": 0}


def test_upload_item_image_rejects_empty_and_invalid(fake_client, monkeypatch):
    assert fake_client.upload_item_image("item123", b"")["message"] == "empty_image"
    assert fake_client.upload_item_image("bad id!", b"x")["message"] == "invalid_item_id"


def test_update_item_metadata_posts_full_payload(fake_client, monkeypatch):
    captured = {}

    class CaptureSession:
        def post(self, url, headers=None, params=None, json=None, data=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse(204)

    monkeypatch.setattr(curation, "jellyfin_client", fake_client)
    monkeypatch.setattr("jellyfin_api.internal_session", lambda: CaptureSession())

    result = fake_client.update_item_metadata(
        "item123", {"Name": "新片名", "Genres": ["有码"]},
    )
    assert result["success"] is True
    assert set(captured["json"]) == set(FULL_ITEM_UPDATE_FIELDS)
    assert captured["json"]["Name"] == "新片名"


def test_update_item_metadata_reports_failure(fake_client, monkeypatch):
    class FailSession:
        def post(self, url, headers=None, params=None, json=None, data=None, timeout=None):
            return FakeResponse(400, text="Error processing request.")

    monkeypatch.setattr(curation, "jellyfin_client", fake_client)
    monkeypatch.setattr("jellyfin_api.internal_session", lambda: FailSession())

    result = fake_client.update_item_metadata("item123", {"Name": "x"})
    assert result["success"] is False
    assert result["status_code"] == 400


def test_item_snapshot_falls_back_to_library_route(fake_client, monkeypatch):
    """单条详情可能整体 500，列表路由要能兜住。"""
    class SnapshotSession:
        def get(self, url, headers=None, params=None, timeout=None):
            if "/Users/user1/Items/" in url:
                return FakeResponse(500, text="Error processing request.")
            return FakeResponse(200, {"Id": "item1", "Name": "n"})

    monkeypatch.setattr(curation, "jellyfin_client", fake_client)
    monkeypatch.setattr("jellyfin_api.internal_session", lambda: SnapshotSession())
    fake_client.forget_items(["item1"])

    assert fake_client.item_snapshot("item1")["Name"] == "n"


def test_item_snapshot_returns_none_for_bad_id(fake_client):
    assert fake_client.item_snapshot("has space") is None
    assert fake_client.item_snapshot("") is None


def test_is_valid_jellyfin_item_id_rejects_path_traversal():
    assert is_valid_jellyfin_item_id("a" * 32)
    assert not is_valid_jellyfin_item_id("../etc/passwd")
    assert not is_valid_jellyfin_item_id("a" * 200)


def test_build_target_name_strips_translation_suffixes():
    build = JellyfinCurationService._build_target_name
    # 生产实测译文里常拼着 番号/日期尾巴，不剥会产出重复番号
    assert build("X", "IPZZ-912", "最高级M奴 樱空桃高清字幕IPZZ-912/2026-09-25") == \
        "IPZZ-912 最高级M奴 樱空桃"
    assert build("X", "ABF-386", "被性欲支配的变态情侣高清字幕ABF-386/2026-10-02") == \
        "ABF-386 被性欲支配的变态情侣"
    # 「今日新物种 / 昨天新種」是抓取站点水印，也要剥
    assert build("X", "STAR-297", "【AI重制师】名流羽田爱中出超高级肥皂女今日新物种STAR-297") == \
        "STAR-297 【AI重制师】名流羽田爱中出超高级肥皂女"
    assert build("X", "300MIUM-1306", "抓住精心化妆的美腰用力抽插昨天新物种300MIUM-1306/2026-01-13") == \
        "300MIUM-1306 抓住精心化妆的美腰用力抽插"
    # 没东西可洗时不要硬拼一个残缺标题
    assert build("X", "MFCW-065", None) is None
    assert build("X", "ABC-123", "短") is None


def test_rename_only_fires_when_current_name_is_not_chinese():
    """已经是中文的条目不该被重写 —— 库内译文与 amane 写的 NFO 用词不同，重写只会把好名字改坏。"""
    service = JellyfinCurationService(client=object())
    movies = {"ABF386": {
        "id": "m1", "title": "x", "title_cn": "另一个译法但仍然是中文标题",
        "original_title": "性欲に支配された", "cover": "", "release_date": "",
    }}
    translations = {}

    good = service._plan_item(
        {"Id": "i1", "Name": "ABF-386 原本就已经是中文的片名",
         "ImageTags": {"Primary": "t"}, "Overview": "已有简介", "ProviderIds": {}},
        movies, translations,
    )
    assert not [a for a in good["actions"] if a["action"] == "rename"]

    kana = service._plan_item(
        {"Id": "i2", "Name": "ABF-386 性欲に支配された倒錯カップル",
         "ImageTags": {"Primary": "t"}, "Overview": "已有简介", "ProviderIds": {}},
        movies, translations,
    )
    assert [a for a in kana["actions"] if a["action"] == "rename"]


def test_pick_translation_prefers_title_cn_over_cache():
    translations = {"原題": "缓存译文内容足够长"}
    movie = {"title_cn": "库内中文标题内容", "original_title": "原題"}
    picked = JellyfinCurationService._pick_translation(movie, translations)
    assert picked == "库内中文标题内容"


def test_pick_translation_rejects_kana_and_bad_text():
    # 含假名的不算「已中文」
    assert JellyfinCurationService._pick_translation(
        {"title_cn": "まだ日本語", "original_title": "x"}, {}
    ) is None
    # 坏译文闸门同样适用
    assert JellyfinCurationService._pick_translation(
        {"title_cn": "该内容无法识别为有效文本", "original_title": "x"}, {}
    ) is None


def test_pick_translation_falls_back_to_translations_table():
    translations = {"友達の母親": "朋友的母亲最终章"}
    movie = {"title_cn": None, "original_title": "友達の母親"}
    assert JellyfinCurationService._pick_translation(movie, translations) == "朋友的母亲最终章"


def test_pick_translation_rejects_too_short_brand_name():
    """实测：まあや（厂牌名）直译成「出色地」，写进片名比原文更糟，必须拦。"""
    assert JellyfinCurationService._pick_translation(
        {"title_cn": "出色地", "original_title": "まあや"}, {}
    ) is None


def test_pick_translation_rejects_katakana_only_original():
    """原题是纯片假名 → 多半是厂牌/系列名，翻译它没意义。"""
    assert JellyfinCurationService._pick_translation(
        {"title_cn": None, "original_title": "ソ"}, {}
    ) is None


def test_scan_is_read_only_and_reports_plans(monkeypatch):
    """扫描必须只发 GET，且对每条给出可执行的动作清单。"""
    session = FakeSession()

    def fake_get(url, headers=None, params=None, timeout=None):
        session.calls.append(("GET", url))
        if "/Users/user1/Items" in url:
            if params and params.get("StartIndex"):
                return FakeResponse(200, {"Items": [], "TotalRecordCount": 1})
            return FakeResponse(200, {"Items": [{
                "Id": "item1",
                "Name": "ABF-386 性欲に支配された倒錯カップル",
                "ImageTags": {},
                "Overview": "",
                "ProviderIds": {},
                "MediaSources": [{"Path": "/hp/ABF-386/ABF-386.mp4"}],
            }], "TotalRecordCount": 1})
        return FakeResponse(200, {"Items": []})

    session.get = fake_get
    monkeypatch.setattr("jellyfin_api.internal_session", lambda: session)
    monkeypatch.setattr(
        "jellyfin_api.JellyfinClient.get_libraries",
        lambda self: [{"id": "lib1", "name": "黄片", "type": "movies"}],
    )
    monkeypatch.setattr("jellyfin_api.JellyfinClient._discover_user_id", lambda self: "user1")
    monkeypatch.setattr(curation, "load_movies_by_codes", lambda codes: {
        "ABF386": {"id": "m1", "title": "x", "title_cn": "被性欲支配的变态情侣",
                   "original_title": "性欲に支配された", "cover": "", "release_date": ""},
    })
    monkeypatch.setattr(curation, "load_translations_by_titles", lambda titles: {})
    monkeypatch.setattr(curation.image_cache_service, "find_cached_original", lambda url: None)

    client = curation.jellyfin_client.__class__()
    client.host = "http://jf.local:8096"
    client.api_key = "key"
    client.reload_config = lambda: None
    service = JellyfinCurationService(client=client)

    result = service.scan()

    assert result["success"] is True, result.get("message")
    assert result["dry_run"] is True
    # 全程只有 GET，绝不能出现写请求
    assert all(method == "GET" for method, _ in session.calls), session.calls
    plan = result["items"][0]
    assert plan["jellyfin_id"] == "item1"
    kinds = {a["action"] for a in plan["actions"]}
    assert "rename" in kinds
    assert plan["target_name"] == "ABF-386 被性欲支配的变态情侣"
    # 缺口统计要对得上
    assert result["stats"]["missing_cover"] == 1
    assert result["stats"]["name_has_kana"] == 1


def test_scan_reports_not_configured(fake_client):
    fake_client.host = ""
    result = JellyfinCurationService(client=fake_client).scan()
    assert result["success"] is False
    assert result["message"] == "jellyfin_not_configured"