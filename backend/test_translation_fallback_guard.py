"""兜底翻译路径的占位护栏 + 「整条即艺名」跳过（2026-11-09 生产实测）。

背景：v2.17 上线后生产日志反复出现

    译文未过质量闸门(too_similar): 香椎花乃
    译文未过质量闸门(too_similar): 奥田咲
    译文未过质量闸门(too_similar): 乳液

追下去是一条**因果链**，不是三个孤立现象：

1. `is_artist_name_only` 靠「假名段是否被艺人名覆盖」判断「整条标题就是艺人名」，
   而 `香椎花乃` / `奥田咲` / `榊原萌` / `新谷未来` 是**全汉字、零假名**的名字 ——
   假名段是空的 → 直接漏判 → ollama 路径把正确译文判成 `too_similar` 拦下。
2. 就算闸门放行，抓取侧的 `normalized == original` 也会把它**踢出** ollama 路径。
3. 于是它落到**完全没有护栏**的百度 / Google 兜底 → 日文艺名被音译成中文
   （实测 `いおりさん` → `伊吕波`、`美々香さん` → `米米卡桑`，**已入库**），
   违反用户「艺人姓名保留日文假名」的口径。
4. 因为音译后的译文 ≠ 原文，它被当成有效译文保存；下一轮原文变了也不会命中缓存，
   于是每轮都重烧一次 LLM 调用 + 一次兜底调用。

修法（本文件锁住的行为）：
- 护栏**独立于翻译源**构建，兜底路径前后也套占位保护（翻译源看不到名字）；
- 整条标题就是艺人名的条目**不进翻译链路** —— 正确结果就是它自己。
"""
import threading
from unittest.mock import MagicMock, patch

import scraper as scraper_module
from scraper import JavBusScraper
from translation_guard import TitleGuard, is_low_quality


class _RecordingTranslator:
    """记录调用次数的假 ollama 翻译器。"""

    batch_size = 10

    def __init__(self):
        self.calls = 0
        self.seen = []

    def translate_batch(self, texts):
        self.calls += 1
        self.seen.append(list(texts))
        return {}


def _make_scraper(ollama=None, guard=None):
    s = JavBusScraper.__new__(JavBusScraper)
    s._translation_lock = threading.Lock()
    s.ollama_translator = ollama
    s.translation_guard = guard
    s.has_baidu = False
    s.translator = None
    s.proxies = None
    s._ollama_disabled_until = 0.0
    s._ollama_fail_log_at = 0.0
    s._google_endpoint_fail_log_at = 0.0
    s._google_disabled_until = 0.0
    s._google_cooldown_log_at = 0.0
    s.session = MagicMock()
    return s


def _fake_db():
    db = MagicMock()
    db.get_translation.return_value = None
    db.get_translations_batch.return_value = {}
    return db


# ---------- 闸门：全汉字艺名不再被误杀 ----------

def test_kanji_artist_name_only_is_accepted():
    """`香椎花乃` 全汉字零假名 —— 译文与原文相同是**正确**结果，不能判 `too_similar`。"""
    assert is_low_quality("香椎花乃", "香椎花乃", allowed=["香椎花乃"]) is None


def test_kanji_artist_name_with_honorific_is_accepted():
    """`香椎花乃さん`（汉字艺名 + 假名敬称）同样放行。

    它还多踩一个坑：译文里有一段假名（`さん`），不放行会被当成
    「原文没有的假名」判成 `kana_in_output`。
    """
    assert is_low_quality("香椎花乃さん", "香椎花乃さん", allowed=["香椎花乃"]) is None


def test_kanji_artist_name_still_needs_to_match_the_name_table():
    """库里没有这条名字时，「原文照传」仍然判失败（无法证明它是专名）。"""
    assert is_low_quality("香椎花乃", "香椎花乃") is not None


def test_name_only_exemption_does_not_leak_to_longer_titles():
    """带作品名的标题不能被当成「整条即艺名」放行。

    `香椎花乃`（艺名本身）和 `香椎花乃の専属`（艺名 + 作品名）必须区别对待 ——
    后者的正确译文应该把作品名翻出来，原文照传就是没翻。
    """
    assert is_low_quality("香椎花乃の専属", "香椎花乃の専属", allowed=["香椎花乃"]) is not None


# ---------- 入口：整条即艺名的标题不进翻译链路 ----------

def test_name_only_title_skips_the_whole_pipeline():
    """整条标题就是艺人名 → 不发 LLM、不走兜底，直接返回原文。"""
    ollama = _RecordingTranslator()
    guard = TitleGuard({"香椎花乃", "いおり"})
    s = _make_scraper(ollama, guard)
    db = _fake_db()

    with patch.object(scraper_module, "db", db), \
         patch.object(s, "_translate_via_google_endpoint", return_value={}) as google:
        results = s.batch_translate_to_chinese(["香椎花乃", "いおりさん"])

    assert ollama.calls == 0, "整条即艺名的标题不该发 LLM 请求"
    assert google.call_count == 0, "整条即艺名的标题不该走兜底翻译"
    assert results == ["香椎花乃", "いおりさん"]


def test_normal_title_still_goes_through_the_pipeline():
    """普通标题不受影响 —— 跳过规则只针对「整条就是艺名」。"""
    ollama = _RecordingTranslator()
    guard = TitleGuard({"香椎花乃"})
    s = _make_scraper(ollama, guard)
    db = _fake_db()

    with patch.object(scraper_module, "db", db), \
         patch.object(s, "_translate_via_google_endpoint", return_value={}):
        s.batch_translate_to_chinese(["香椎花乃の専属"])

    assert ollama.calls == 1
    assert ollama.seen[0] == ["香椎花乃の専属"]


def test_single_character_name_is_not_skipped():
    """1 字的名字**不跳过** —— 护栏本来就不保护它（`桜` 既可能是人名也可能是普通词）。

    跳过规则与护栏的 `min_len` 对齐：护栏不保护的，跳过规则也不该插手。
    """
    guard = TitleGuard({"桜"})
    s = _make_scraper(_RecordingTranslator(), guard)
    db = _fake_db()

    with patch.object(scraper_module, "db", db), \
         patch.object(s, "_translate_via_google_endpoint", return_value={}):
        s.batch_translate_to_chinese(["桜"])

    assert "桜" not in guard.names, "1 字名不该进护栏名字表"
    assert db.get_translation.called, "1 字名照常走翻译链路"


# ---------- 兜底路径：护栏同样生效 ----------

def test_fallback_translator_never_sees_the_artist_name():
    """兜底翻译源收到的必须是**保护后**的文本 —— 它看不到名字，也就没动机改写。"""
    guard = TitleGuard({"いおり"})
    s = _make_scraper(None, guard)
    db = _fake_db()
    seen = {}

    def _fake_translate(texts, keys=None):
        seen["sent"] = list(texts)
        return {
            key: text.replace("の吸血", "的吸血")
            for key, text in zip(keys or texts, texts)
        }

    with patch.object(scraper_module, "db", db), \
         patch.object(s, "_translate_via_google_endpoint", side_effect=_fake_translate):
        s.batch_translate_to_chinese(["いおりさんの吸血"])

    # 护栏把「名字 + 敬称」**整体**保护了 —— 翻译源只看到 ZZ0ZZ，压根不知道名字是什么
    assert seen["sent"] == ["ZZ0ZZの吸血"], seen
    db.save_translation.assert_called_once()
    orig, trans = db.save_translation.call_args[0]
    assert orig == "いおりさんの吸血"
    assert trans == "いおりさん 的吸血".replace(" ", ""), trans


def test_fallback_translation_that_loses_the_name_is_dropped():
    """翻译源把占位符拆散了 → 艺人名丢了，这条译文丢弃，不能写进库。

    注意判据是「还原后**艺人名还在不在**」，不是「占位符还在不在」——
    `ZZ0ZZ` 被拆成 `ZZ0 ZZ` 时占位符检测不到，但名字已经丢了。
    """
    guard = TitleGuard({"いおり"})
    s = _make_scraper(None, guard)
    db = _fake_db()

    def _mangle(texts, keys=None):
        return {
            key: text.replace("ZZ0ZZ", "ZZ0 ZZ")
            for key, text in zip(keys or texts, texts)
        }

    with patch.object(scraper_module, "db", db), \
         patch.object(s, "_translate_via_google_endpoint", side_effect=_mangle):
        s.batch_translate_to_chinese(["いおりさんの吸血"])

    db.save_translation.assert_not_called()


def test_google_endpoint_uses_the_given_keys():
    """占位保护下，Google 端点要用**原文**做键返回（pending 索引靠原文对齐）。"""
    s = _make_scraper(None, TitleGuard({"いおり"}))
    response = MagicMock()
    response.json.return_value = [[
        ["ZZ0ZZ的吸血", "ZZ0ZZ的吸血"],
    ]]
    response.raise_for_status.return_value = None

    with patch.object(s, "session") as session:
        session.get.return_value = response
        out = s._translate_via_google_endpoint(["ZZ0ZZの吸血"], keys=["いおりの吸血"])

    assert out == {"いおりの吸血": "ZZ0ZZ的吸血"}
