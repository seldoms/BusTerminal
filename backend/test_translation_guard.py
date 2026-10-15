"""占位保护与质量闸门的用例（backend/test_translation_guard.py 平铺风格）。

覆盖用户口径「作品名意译、艺人姓名保留日文假名」在代码层的保证，
以及坏译文必须被拦住、不许进 translations 表。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import is_bad_translation_text  # noqa: E402
from translation_guard import (  # noqa: E402
    TitleGuard,
    is_low_quality,
    looks_untranslated,
    too_similar,
)
from ollama_translator import OllamaTranslator  # noqa: E402

NAMES = ["水野アコ", "小坂ひまり", "椎名由奈", "五条恋", "清原みゆう"]


def _guard():
    return TitleGuard(NAMES)


# ---------- 占位保护 ----------

def test_protect_replaces_known_kana_name():
    guard = _guard()
    protected, mapping = guard.protect("100分フェラチオ 水野アコ")
    assert "水野アコ" not in protected
    assert mapping and list(mapping.values()) == ["水野アコ"]
    assert "ZZ0ZZ" in protected


def test_protect_leaves_unknown_names_untouched():
    guard = _guard()
    text = "【プレコレ】原石人名"
    protected, mapping = guard.protect("【プレコレ】原石人名")
    assert mapping == {}
    assert protected == text


def test_restore_is_exact_inverse():
    guard = _guard()
    text = "100分フェラチオ 水野アコ"
    protected, mapping = guard.protect(text)
    restored = guard.restore(protected, mapping)
    assert restored == text


def test_longer_name_wins_over_shorter_prefix():
    """长名优先替换，避免短名是长名子串时把长名拆坏。"""
    guard = TitleGuard(["桃乃木かな", "桃乃木"])
    protected, mapping = guard.protect("桃乃木かな 作品")
    assert "桃乃木かな" not in protected
    # 长名先被换成占位，短名就不该再单独出现（否则会把长名拆坏）
    assert list(mapping.values()) == ["桃乃木かな"]
    assert "ZZ0ZZ" in protected


def test_guard_not_ready_without_names():
    guard = TitleGuard([])
    assert guard.ready is False
    text = "100分フェラチオ 水野アコ"
    protected, mapping = guard.protect(text)
    assert protected == text and mapping == {}


def test_leftover_tokens_detected_when_model_mangles_token():
    guard = _guard()
    protected, mapping = guard.protect("100分フェラチオ 水野アコ")
    mangled = protected.replace("ZZ0ZZ", "Z Z 0 Z Z")
    assert guard.leftover_tokens(mangled, mapping) == []
    assert guard.leftover_tokens(protected, mapping) == ["ZZ0ZZ"]


# ---------- 质量闸门 ----------

def test_empty_translation_rejected():
    assert is_low_quality("原文", "") == "empty"
    assert is_low_quality("原文", "   ") == "empty"


def test_kana_in_output_rejected():
    assert is_low_quality("【プレコレ】四季", "【预售】四季") is None
    assert is_low_quality("【プレコレ】四季", "【プレコレ】四季") == "kana_in_output"


def test_identical_output_rejected_as_untranslated():
    assert is_low_quality(" DAS S-892 完全版", " DAS S-892 完全版") == "too_similar"
    assert too_similar("ABC-123 作品", "ABC-123 作品") is True


def test_placeholder_lost_rejected():
    reason = is_low_quality("100分フェラチオ 水野アコ", "100分ZZ0ZZ", leftover_tokens=["ZZ0ZZ"])
    assert reason and reason.startswith("placeholder_lost")


def test_good_translation_passes():
    assert is_low_quality("【プレコレ】元現役女子○生グラドル", "【预售】前职业女团") is None
    assert looks_untranslated(" Plumbing 修理", "水管修理") is False


# ---------- 与翻译器集成 ----------

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        import json as _json
        return _json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run_with_content(content, *, strict):
    """把 urlopen 换成返回固定内容，跑一次 translate_batch。"""
    import ollama_translator as module

    tr = OllamaTranslator("http://x", "m", guard=_guard(), strict_quality_gate=strict)
    original = module.urllib.request.urlopen
    module.urllib.request.urlopen = lambda request, timeout=None: _Resp(
        {"message": {"content": content}}
    )
    try:
        return tr.translate_batch(["100分フェラチオ 水野アコ"])
    finally:
        module.urllib.request.urlopen = original


def test_translator_restores_name_and_passes_gate():
    out = _run_with_content("100分ZZ0ZZ", strict=True)
    # 占位符必须被原样还原成日文假名，这正是「艺人名保留假名」口径的代码保证
    assert out == {"100分フェラチオ 水野アコ": "100分水野アコ"}


def test_translator_drops_bad_translation_when_strict():
    # 模型把含假名的原文原样透传 → 闸门应拦下，不进结果
    out = _run_with_content("100分フェラチオ ZZ0ZZ", strict=True)
    assert out == {}


def test_translator_keeps_translation_when_not_strict():
    # 灰度模式：不合格也返回，保持旧行为便于对比
    out = _run_with_content("100分フェラチオ ZZ0ZZ", strict=False)
    assert out and out["100分フェラチオ 水野アコ"] == "100分フェラチオ 水野アコ"


def test_list_prefix_rejected():
    """模型把输入当清单逐条编号输出时必须拦下——否则整批行对齐会被带偏。"""
    assert is_low_quality("近親相姦 タイトル", "1. 近亲乱伦：完整版") == "list_prefix"
    assert is_low_quality("タイトル", "2、另一个标题") == "list_prefix"
    assert is_low_quality("タイトル", "第3条 某作品") == "list_prefix"
    assert is_low_quality("タイトル", "③ 某作品") == "list_prefix"


def test_normal_translation_not_rejected_by_list_prefix_rule():
    # 正常译文不该被序号规则误杀
    assert is_low_quality("【プレコレ】完整版", "预售 完整版") is None
    assert is_low_quality("S級熟女 6時間", "S级熟女 6小时") is None

# ---------- 上游拒答文案 ----------

def test_refusal_comments_are_bad_translation_text():
    """已中文的文本送进翻译器，上游回的是元评论而不是译文（订阅页实测），必须拦下。"""
    assert is_bad_translation_text("该内容无法识别为有效影视标题。")
    assert is_bad_translation_text("该内容无实际意义，无法进行翻译。")
    assert is_bad_translation_text("该内容无实际意义，无法翻译。")
    assert is_bad_translation_text("该文本无实际意义")
    assert is_bad_translation_text("请提供要翻译的文本")


def test_legitimate_titles_survive_refusal_rules():
    """别拿「抱歉/我无法/请提供」做宽泛匹配：「ごめんなさい」的合法译文就是「对不起」。"""
    assert not is_bad_translation_text("对不起，我爱你 视觉原声带DVD")
    assert not is_bad_translation_text("我无法忘记那个强奸我的我讨厌的男人")
    assert not is_bad_translation_text("抱歉射太多了…给天真的身体带来的强烈快感")
    assert not is_bad_translation_text("请确认！这个女孩的色气")
    assert not is_bad_translation_text("无法识别的男人")
    assert not is_bad_translation_text("该上课了，老师")
    assert not is_bad_translation_text("赤井美纪")


def test_control_tokens_are_stripped_not_rejected():
    """模型吐出的结束控制符要剥掉，而不是把整条译文判成垃圾。"""
    from scraper import normalize_simplified_title

    assert normalize_simplified_title("巨乳新人教师</|im_end|>") == "巨乳新人教师"
    assert normalize_simplified_title("巨乳新人教师<|im_end|>", "原标题") == "巨乳新人教师"
    # 剥掉之后若什么都没剩，按坏译文回落到原文
    assert normalize_simplified_title("</|im_end|>", "原标题") == "原标题"


def test_malformed_control_tokens_are_stripped():
    """模型吐过畸形控制符（实测 <|imim_end|> / <|im|_end|>），精确匹配会漏，改用正则。"""
    from database import strip_translation_control_tokens

    assert strip_translation_control_tokens("巨乳新人教师</|im_end|>") == "巨乳新人教师"
    assert strip_translation_control_tokens("052020_001 性感西園さくや<|imim_end|>") == "052020_001 性感西園さくや"
    assert strip_translation_control_tokens("菜月RBC-158/2026-10-10<|im|_end|>") == "菜月RBC-158/2026-10-10"
    # 控制符在中间也要剥干净
    assert strip_translation_control_tokens("前<|im_end|>后") == "前后"
    # 正常标题不能被误伤
    assert strip_translation_control_tokens("巨乳新人教师") == "巨乳新人教师"
    assert strip_translation_control_tokens("A|B 特别篇") == "A|B 特别篇"
