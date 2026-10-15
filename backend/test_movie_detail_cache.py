"""详情页「缓存优先」策略：先渲染库里已存的结构化详情，标题沿用已有译文。"""

import pytest

import scraper
from scraper import JavBusScraper


class FakeDb:
    def __init__(self, movie=None, translations=None):
        self.movie = movie
        self.translations = translations or {}
        self.queued = []

    def get_movie(self, code):
        return self.movie

    def get_translation(self, text):
        return self.translations.get(text)


def _scraper(monkeypatch, movie, translations=None):
    """绕过 __init__（它会去探测域名），只装配本次要用的依赖。"""
    instance = JavBusScraper.__new__(JavBusScraper)
    fake = FakeDb(movie, translations)
    monkeypatch.setattr(scraper, "db", fake)
    instance.queue_title_translations = lambda titles, priority=True: (
        fake.queued.extend(titles) or len(titles)
    )
    return instance, fake


def test_strip_title_code_prefix():
    strip = JavBusScraper.strip_title_code_prefix
    assert strip("ZSMX-005 『そんなつもりじゃ…』/川原紬", "ZSMX-005") == "『そんなつもりじゃ…』/川原紬"
    # 标题本身不以番号开头，不能动
    assert strip("标题本身不以番号开头", "ABC-123") == "标题本身不以番号开头"
    # 剥光了就保留原样，别返回空标题
    assert strip("ABC-123", "ABC-123") == "ABC-123"
    assert strip("", "ABC-123") == ""
    assert strip("ABC-123", "") == "ABC-123"


def test_cache_hit_uses_existing_translation(monkeypatch):
    raw = "ZSMX-005 『そんなつもりじゃ無かったのに…』/川原紬"
    pure = "『そんなつもりじゃ無かったのに…』/川原紬"
    movie = {
        "id": "ZSMX-005",
        "title": raw,
        "detail": {"title": raw, "original_title": raw, "magnets": []},
    }
    s, _ = _scraper(monkeypatch, movie, {pure: "《并非本意……》被诱导/川原䌷"})

    detail = s.get_movie_detail_cached_first("https://www.javbus.com/ZSMX-005", "ZSMX-005")

    assert detail["title"] == "《并非本意……》被诱导/川原䌷"
    assert detail["original_title"] == pure
    assert detail["translation_source"] == "cache"


def test_falls_back_to_library_title(monkeypatch):
    """没有译文缓存，但库里的 title 已经翻过（列表页就是这么展示的）。"""
    raw = "ZSMX-004 自覚のないまま発するフェロモンに引き寄せられる/浜野つむぐ"
    movie = {
        "id": "ZSMX-004",
        "title": "ZSMX-004 她被她散发的信息素所吸引的男人们玩弄着/浜野迹",
        "detail": {"title": raw, "original_title": raw},
    }
    s, _ = _scraper(monkeypatch, movie)

    detail = s.get_movie_detail_cached_first("https://www.javbus.com/ZSMX-004", "ZSMX-004")

    assert detail["title"] == "她被她散发的信息素所吸引的男人们玩弄着/浜野迹"
    assert detail["translation_source"] == "library"


def test_missing_translation_queues_translation(monkeypatch):
    """没有译文就显示原文，同时把原文排进翻译队列，下次打开就是中文。"""
    raw = "ZSMX-009 まだ翻訳されていないタイトル/新人娘"
    movie = {"id": "ZSMX-009", "title": raw, "detail": {"title": raw, "original_title": raw}}
    s, fake = _scraper(monkeypatch, movie)

    detail = s.get_movie_detail_cached_first("https://www.javbus.com/ZSMX-009", "ZSMX-009")

    assert detail["title"] == "まだ翻訳されていないタイトル/新人娘"
    assert detail["translation_source"] == "pending"
    assert fake.queued == ["まだ翻訳されていないタイトル/新人娘"]


def test_unknown_movie_returns_none(monkeypatch):
    """库里没有这部片子，交给调用方回退实时抓取。"""
    s, _ = _scraper(monkeypatch, None)
    assert s.get_movie_detail_cached_first("https://www.javbus.com/NOPE-001", "NOPE-001") is None


R18_DETAIL = {
    "code": "GHKO-96",
    "source": "r18",
    "title_ja": "ヒロインピンチ 新村あかり",
    "runtime_mins": "70",
    "maker": {"name_ja": "GIGA"},
    "label": {"name_ja": "GIGA"},
    "series": {"name_ja": "ヒロインピンチ"},
    "tags": ["拘束", "女戦士"],
    "stars": [{"id": "r18:1036352", "name": "新村あかり"}],
    "gallery": {"full_first": "https://pics.dmm.co.jp/x.jpg", "thumb_first": None},
    "r18_content_id": "n_600ghko96",
}


def test_r18_detail_is_adapted_for_frontend(monkeypatch):
    """r18 源占全库 99.5%，它的详情是另一套结构，必须映射成前端能渲染的形状。"""
    movie = {"id": "GHKO-96", "title": "GHKO-96 女英雄危机", "detail": dict(R18_DETAIL)}
    s, _ = _scraper(monkeypatch, movie)

    detail = s.get_movie_detail_cached_first("https://r18.dev/x", "GHKO-96")

    assert detail["cover"] == "https://pics.dmm.co.jp/x.jpg"
    assert detail["genres"] == ["拘束", "女戦士"]
    assert detail["magnets"] == []
    assert detail["info"]["识别码"] == "GHKO-96"
    assert detail["info"]["时长"] == "70分钟"
    assert detail["info"]["制作商"] == "GIGA"
    assert detail["info"]["系列"] == "ヒロインピンチ"


def test_r18_detail_uses_title_ja_as_original(monkeypatch):
    """r18 结构里原文在 title_ja；不认它就没法查译文缓存，也没法入队翻译。"""
    movie = {"id": "GHKO-96", "title": "GHKO-96", "detail": dict(R18_DETAIL)}
    s, fake = _scraper(monkeypatch, movie, {"ヒロインピンチ 新村あかり": "女英雄危机 新村明里"})

    detail = s.get_movie_detail_cached_first("https://r18.dev/x", "GHKO-96")

    assert detail["original_title"] == "ヒロインピンチ 新村あかり"
    assert detail["title"] == "女英雄危机 新村明里"
    assert detail["translation_source"] == "cache"


def test_r18_detail_without_gallery_falls_back_to_movie_cover(monkeypatch):
    """gallery 全空的片子（约一半）用库里已存的封面，别让详情页连图都没有。"""
    bare = dict(R18_DETAIL, gallery={"full_first": None, "thumb_first": None})
    movie = {"id": "GHKO-96", "title": "GHKO-96", "cover": "https://cdn/x.jpg", "detail": bare}
    s, _ = _scraper(monkeypatch, movie)

    detail = s.get_movie_detail_cached_first("https://r18.dev/x", "GHKO-96")

    assert detail["cover"] == "https://cdn/x.jpg"


def test_non_r18_detail_is_left_alone(monkeypatch):
    """javbus 结构本来就能渲染，不能被适配逻辑改坏。"""
    raw = "ZSMX-005 『そんなつもり…』/川原紬"
    movie = {
        "id": "ZSMX-005",
        "title": raw,
        "detail": {"title": raw, "original_title": raw, "cover": "https://javbus/c.jpg", "info": {"識別碼": "ZSMX-005"}},
    }
    s, _ = _scraper(monkeypatch, movie)

    detail = s.get_movie_detail_cached_first("https://www.javbus.com/ZSMX-005", "ZSMX-005")

    assert detail["cover"] == "https://javbus/c.jpg"
    assert detail["info"] == {"識別碼": "ZSMX-005"}


# ------------------------------------------------------------------
# 译文判定：英文标题不能被当成"中文标题"
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate,original,expected",
    [
        # 回归：英文标题 != 日文原文，早期逻辑会误判为译文
        ("Momo Sakai Perfect Satisfaction", "Momo Sakai Perfect Satisfaction", False),
        ("Momo Sakai Perfect Satisfaction", "日本語のタイトル", False),
        # 真正的中文译文应被接受
        ("桃井杏奈 初次体验", "Momo Sakai", True),
        # 空值
        ("", "x", False),
        (None, "x", False),
        # 与原文相同不算译文
        ("桃井杏奈", "桃井杏奈", False),
        # 纯数字/符号不算
        ("2024-01-01", "日本語", False),
    ],
)
def test_usable_translation_requires_chinese(candidate, original, expected):
    from scraper import JavBusScraper

    assert JavBusScraper._is_usable_translation(candidate, original) is expected


def test_english_library_title_is_not_used_as_display(monkeypatch):
    """库里存的是英文标题时，详情页应回落到原文并标记 pending，而不是显示英文。"""
    original = "Momo Sakai Perfect Satisfaction Fresh Face Soapland Deluxe"
    movie = {
        "id": "ABP-123",
        # 库标题是英文（抓取时上游给的），并非译文
        "title": original,
        "detail": {"title": original, "original_title": original, "cover": "https://c/x.jpg"},
    }
    s, _ = _scraper(monkeypatch, movie)

    detail = s.get_movie_detail_cached_first("https://www.javbus.com/ABP-123", "ABP-123")

    # 英文不算译文 → 标记 pending，且显示的是原文
    assert detail["translation_source"] == "pending"
    assert detail["title"] == original


def test_title_falls_back_when_only_english_available(monkeypatch):
    """回归：只有 title_en 的条目，早期会算出空标题，页面标题直接消失。

    宁可显示英文，也不要显示空白 —— 英文至少还能认出来是什么片。
    """
    movie = {
        "id": "ABP-123",
        "title": "Momo Sakai Perfect Satisfaction",
        "detail": {
            "title_ja": None,
            "title_en": "Momo Sakai Perfect Satisfaction Fresh Face Soapland Deluxe",
            "cover": "https://c/x.jpg",
            "info": {"識別碼": "ABP-123"},
        },
    }
    s, _ = _scraper(monkeypatch, movie)

    detail = s.get_movie_detail_cached_first("https://www.javbus.com/ABP-123", "ABP-123")

    assert detail["title"], "标题不能为空"
    assert "Momo Sakai" in detail["title"]
    assert detail["translation_source"] == "pending"


def test_title_never_blank_even_without_any_title_field(monkeypatch):
    """兜底：连 title_en 都没有时，至少退回番号，不能空。"""
    movie = {
        "id": "ABP-999",
        "title": "",
        "detail": {"cover": "https://c/x.jpg", "info": {"識別碼": "ABP-999"}},
    }
    s, _ = _scraper(monkeypatch, movie)

    detail = s.get_movie_detail_cached_first("https://www.javbus.com/ABP-999", "ABP-999")

    assert detail["title"]
