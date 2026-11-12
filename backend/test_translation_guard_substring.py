"""护栏「段内子串 + 敬称后缀」匹配的锁定测试（2026-11-09）。

背景：开回填后实测 **33%** 的条目被质量闸门误杀（`kana_in_output`），
根因是护栏只做**整段精确匹配** —— 标题里的演员名往往嵌在更长的连续段里
（`小坂ひまりさんの紙パンティとチェキ付き`），整段查表必然查不到 → 名字没被保护
→ 模型译文里留下假名 → 闸门拦下。

三条匹配路（护栏类文档有详述）：
  1. 整段精确匹配
  2. 段内子串匹配（名字 >= 3 字）
  3. 敬称后缀（2 字名的通路：`あいちゃん` / `なつさん`）
"""

from translation_guard import TitleGuard, is_low_quality

NAMES = [
    "小坂ひまり",
    "小鈴みかん",
    "美木ひなの",
    "麻倉まりな",
    "水野アコ",
    "天音エミル",
    "愛瀬まりな",
    "あい",
    "なつ",
    "ちあき",
    "みさ",
]


def _guard():
    return TitleGuard(NAMES)


# ---------- 路 2：段内子串 ----------

def test_name_inside_long_segment_is_protected():
    """名字嵌在长段里也要能保护住（这是 33% 误杀的主因）。"""
    g = _guard()
    text = "【数量限定】乳首開発電車8 小坂ひまりさんの紙パンティとチェキ付き"
    protected, mapping = g.protect(text)
    assert mapping, "应命中 小坂ひまり"
    assert "小坂ひまり" not in protected
    assert "ZZ0ZZ" in protected


def test_multiple_names_in_one_segment():
    """一段里多个名字都要保护。"""
    g = _guard()
    text = "小坂ひまりと小鈴みかんの共演"
    protected, mapping = g.protect(text)
    assert len(mapping) == 2
    assert "小坂ひまり" not in protected
    assert "小鈴みかん" not in protected


def test_exact_match_still_works():
    """整段精确命中的老行为不能退化。"""
    g = _guard()
    text = "100分フェラチオ 水野アコ"
    protected, mapping = g.protect(text)
    assert mapping == {"ZZ0ZZ": "水野アコ"}
    assert protected == "100分フェラチオ ZZ0ZZ"


def test_unknown_names_not_protected():
    """不在表里的名字不该被保护。"""
    g = _guard()
    text = "未知女優xyz Pilgrim 作品"
    protected, mapping = g.protect(text)
    assert not mapping
    assert protected == text


def test_restore_roundtrip_keeps_kana():
    """保护 → 还原后，译文里的假名必须原样回来（用户口径：艺人名保留日文假名）。"""
    g = _guard()
    text = "【数量限定】乳首開発電車8 小坂ひまりさんの紙パンティ"
    protected, mapping = g.protect(text)
    translated = "【数量限定】乳头开发电车8 ZZ0ZZ的纸丁字裤"
    restored = g.restore(translated, mapping)
    assert "小坂ひまり" in restored
    # 还原后不该被判"未翻译"（假名是允许的艺人名）
    reason = is_low_quality(text, restored, allowed=list(mapping.values()))
    assert reason is None, "不该被质量闸门拦下：%s" % reason


def test_longer_name_wins_over_shorter():
    """长短名重叠时，长的优先（避免把长名拆坏）。"""
    g = TitleGuard(["愛瀬まりな", "まりな"])
    text = "愛瀬まりな出演"
    protected, mapping = g.protect(text)
    assert "愛瀬まりな" not in protected
    assert list(mapping.values()) == ["愛瀬まりな"]


def test_no_dead_mapping_entries():
    """被更长名字覆盖掉的短名不该留死映射。"""
    g = TitleGuard(["愛瀬まりな", "まりな"])
    text = "愛瀬まりな"
    protected, mapping = g.protect(text)
    restored = g.restore(protected, mapping)
    assert restored == "愛瀬まりな"


def test_update_names_reindexes():
    """动态补名后要重建索引，否则新名字匹配不上。"""
    g = TitleGuard(["水野アコ"])
    assert not g.protect("小坂ひまり作品")[1]
    g.update_names(["小坂ひまり"])
    assert g.protect("小坂ひまり作品")[1], "新增名字应立即生效"


def test_empty_input_is_safe():
    g = _guard()
    assert g.protect("")[0] == ""
    assert g.protect("")[1] == {}
    assert g.protect("タイトル")[1] == {}


# ---------- 2 字名不能进子串匹配 ----------

def test_two_char_name_not_matched_as_substring():
    """2 字名不能当子串匹配 —— 会把公共词当人名。"""
    g = _guard()
    # `りさ` 是 2 字名，但 `りさまる` 里的 `りさ` 不是人名
    protected, mapping = g.protect("あざとすぎるよ、りさまるちゃん！")
    assert not mapping, "公共词里的 2 字片段不该被保护"


def test_two_char_name_not_matched_inside_common_word():
    """`えっちな女` 里的 `ちな` 也不该被保护。"""
    g = _guard()
    protected, mapping = g.protect("えっちな女でごめんなさい")
    assert not mapping


def test_two_char_name_matched_when_exact_segment():
    """标题恰好就是 2 字名时，整段精确匹配仍然生效。"""
    g = _guard()
    protected, mapping = g.protect("あい")
    assert mapping == {"ZZ0ZZ": "あい"}


# ---------- 路 3：敬称后缀 ----------

def test_two_char_name_with_honorific_is_protected():
    """`あいちゃん` 这种「2 字名 + 敬称」必须保护住。"""
    g = _guard()
    text = "【AIリマスター版】部活帰り あいちゃん（18）"
    protected, mapping = g.protect(text)
    assert mapping, "あいちゃん 应被保护"
    assert "あい" not in protected.replace("ZZ0ZZ", "")
    assert "ZZ0ZZ" in protected


def test_honorific_protection_keeps_honorific_out_of_translation():
    """保护单位是「名字+敬称」整体，模型看不到就不会乱改。"""
    g = _guard()
    text = "部活帰り あいちゃん（18）"
    protected, mapping = g.protect(text)
    restored = g.restore(protected, mapping)
    assert restored == text, "保护+还原必须完全还原原文"


def test_name_with_san_honorific_is_protected():
    """`なつさん` 这种也一样。"""
    g = _guard()
    text = "人妻斬り なつさん"
    protected, mapping = g.protect(text)
    assert mapping


def test_common_word_before_honorific_not_protected():
    """敬称前不是库里的名字时，不该乱保护（否则把该翻译的词冻住）。"""
    g = _guard()
    text = "いい迷惑 趣味さん"
    protected, mapping = g.protect(text)
    assert not mapping


def test_honorific_with_long_kanji_name_is_protected():
    """`吉野れなさん` 这种长名 + 敬称，取最长命中。"""
    g = TitleGuard(["吉野れな", "れな"])
    text = "素人 吉野れなさん"
    protected, mapping = g.protect(text)
    assert list(mapping.values()) == ["吉野れなさん"], mapping


def test_common_phrase_before_honorific_not_protected():
    """`綺麗なお姉さん` 里的 `なお`（恰是库里 2 字名）不是人名+敬称，不该保护。

    保护错的后果比不保护更重：整个词被换成占位符，模型看不到就没法翻译，
    译文会出现 `お姉小姐` 这种拼接（2026-11-09 生产 A/B 实测发现）。
    """
    g = _guard()
    text = "同じマンションに住む綺麗なお姉さんのチ●ポ"
    protected, mapping = g.protect(text)
    assert not mapping, mapping


def test_name_at_segment_start_with_honorific_is_protected():
    """名字在段首（前面是空格/标点）时，敬称路径必须命中。"""
    g = _guard()
    text = "部活帰り あいちゃん（18）"
    protected, mapping = g.protect(text)
    assert mapping, "段首的 あいちゃん 应被保护"
    assert "あい" not in protected
