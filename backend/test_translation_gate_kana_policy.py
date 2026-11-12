"""质量闸门「假名残留」新口径的锁定测试（2026-11-09 生产实测改）。

旧口径「译文里有任何假名就判失败」把两件事混为一谈：
  A. 模型保留了原文里的专名（副标题、演员名）—— 用户口径「艺人姓名保留日文假名」
     明确要这种，判失败是**误杀**（实测开回填后 33% 的条目被误杀）
  B. 模型把片名**翻残**了（`アメイジング`→`アメイジ音`）—— 真失败，必须拦

新判据：译文里的每一段假名，都必须能在**原文里找到**（整段相等，或互为子串）。
原文整段照传单独判失败（压根没翻）。
"""

from translation_guard import is_low_quality, looks_untranslated


# ---------- 真失败：必须拦 ----------

def test_mangled_kana_is_rejected():
    """片名被翻残（`アメイジング`→`アメイジ音`）必须拦下。"""
    reason = is_low_quality("DOPAMIX-WORLD アメイジング", "DOPAMIX-WORLD アメイジ音")
    assert reason == "kana_in_output", reason


def test_whole_title_passthrough_is_rejected():
    """原文整段照传 → 压根没翻，必须拦。"""
    assert is_low_quality("【プレコレ】四季", "【プレコレ】四季") == "kana_in_output"


def test_kana_not_in_source_is_rejected():
    """译文里冒出原文没有的假名 → 模型自己造词，必须拦。"""
    assert is_low_quality("素人 C 服生中出", "素人 C 服生中出 クリクリ") == "kana_in_output"


def test_replaced_kana_is_rejected():
    """原文的假名被换成另一段假名 → 不是保留，是改写，必须拦。"""
    reason = is_low_quality("超かわクリコス娘たち", "超可爱クリクリ小女生")
    assert reason == "kana_in_output", reason


# ---------- 专名保留：必须放行 ----------

def test_subtitle_kana_preserved_is_accepted():
    """副标题保留原文假名 —— 正确的译文，不能误杀。"""
    assert is_low_quality("First Memory 〜ひめごと〜", "初次记忆 〜ひめごと〜") is None


def test_actress_kana_preserved_is_accepted():
    """演员名保留日文（库里没这条记录，护栏管不到）—— 译文正确，不能误杀。"""
    reason = is_low_quality(
        "私立恵比寿中学 小林歌穂 卒業式「ぽ〜EVER」",
        "私立惠比寿中学 小林歌穂 退学式「ポ〜EVER」",
    )
    assert reason is None, reason


def test_mixed_kanji_kana_name_is_accepted():
    """演员名是汉字+假名混合（`小坂ひまり`）时也要放行。

    护栏按连续段切原文，`小坂ひまり` 的假名段是 `ひまり`；
    译文里那一段是 `小坂ひまり` —— 整段相等会误判，必须允许互为子串。
    """
    assert looks_untranslated("小坂ひまりさんの紙パンティ", "小坂ひまり的纸丁字裤") is False


def test_allowed_name_makes_kana_legal():
    """`allowed` 里的艺人名，其假名在译文里出现是合规的。"""
    assert looks_untranslated(
        "100分フェラチオ 水野アコ", "100分手活 水野アコ", allowed=["水野アコ"]
    ) is False


def test_source_kana_kept_whole_is_accepted():
    """原文整段假名被原样保留 → 合规。"""
    assert looks_untranslated("M.Kちゃん", "M.Kちゃん", allowed=["M.Kちゃん"]) is False


def test_title_that_is_only_artist_name_is_accepted():
    """整条标题就是艺人名（`いおりさん`）→ 译文与原文相同是**正确**结果。

    2026-11-09 生产实测踩到：整条被护栏换成 `ZZ0ZZ`，模型如实回 `ZZ0ZZ`，
    还原后译文 = 原文 → `too_similar` 判失败 → 这条永远翻不出来。
    """
    assert is_low_quality("いおりさん", "いおりさん", allowed=["いおりさん"]) is None


def test_title_that_is_only_artist_name_is_accepted_with_honorific():
    """带敬称的艺名同理（`あいちゃん`）。"""
    assert is_low_quality("あいちゃん", "あいちゃん", allowed=["あいちゃん"]) is None


def test_plain_passthrough_without_allowed_name_is_still_rejected():
    """库里没有这条演员名时，「原文照传」仍然判失败（无法证明它是专名）。"""
    assert is_low_quality("いおりさん", "いおりさん") is not None


def test_normal_chinese_translation_still_passes():
    """纯中文译文当然放行。"""
    assert is_low_quality("巨乳人妻の誘惑", "巨乳人妻的诱惑") is None
    assert is_low_quality("【プレコレ】完全版", "预售 完整版") is None


def test_empty_still_rejected():
    """空译文仍然拦。"""
    assert is_low_quality("原文", "") == "empty"
    assert is_low_quality("原文", "   ") == "empty"


# ---------- 复合词断段（2026-11-09 生产实测：`ヴィーナス・コード` 误杀） ----------

def test_compound_word_split_lets_brand_name_through():
    """模型保留品牌名、只翻译后半段 → 合规，不能误杀。

    `ヴィーナス・コード` → `ヴィーナス·代码`：库里没有 `ヴィーナス` 这条演员记录，
    护栏管不到，但译文是对的。旧口径把它当成「原文没有的假名」→ 拦。
    """
    assert looks_untranslated("ヴィーナス・コード", "ヴィーナス·代码") is False


def test_compound_word_split_still_rejects_mangled_kana():
    """断段不能放松到「翻残也放过」。

    `アメイジング` 被翻成 `アメイジ`（差 `ング`）仍必须拦 —— 哪怕它后面跟着分隔符。
    """
    assert looks_untranslated("DOPAMIX-WORLD アメイジング・コード", "DOPAMIX-WORLD アメイジ·代码") is True


def test_compound_separator_is_not_kana():
    """`・` 落在片假名码位区间里，归一时不能把它转成 `゛` 混进假名段。

    同时锁住「浊音/半浊音归一」这条：组合符必须真被删掉（`str.translate`
    按码位查表，字典用字符串键就一条都命中不了）。
    """
    from translation_guard import _normalize_kana

    assert _normalize_kana("・") == "・"
    assert "・" in _normalize_kana("ヴィーナス・コード")
    assert _normalize_kana("ヴ") == _normalize_kana("ゔ") == _normalize_kana("う")
    assert _normalize_kana("ポ") == _normalize_kana("ぽ") == _normalize_kana("ほ")


# ---------- 敬称 / の：只差这些才放行 ----------

def test_honorific_suffix_difference_is_allowed():
    """原文 `ひまりさんの` → 译文 `ひまり`：差的是敬称 + の → 放行（模型保留专名）。"""
    assert looks_untranslated("小坂ひまりさんの紙パンティ", "小坂ひまり的纸尿裤") is False


def test_honorific_difference_is_allowed_both_ways():
    """「只差敬称 / の」是双向的：原文带敬称、译文不带，也该放行。"""
    assert looks_untranslated("小坂ひまりさんの紙パンティ", "小坂ひまりの纸尿裤") is False


def test_mangled_word_is_not_allowed():
    """原文 `あめいじんぐ` → 译文 `あめいじ`：差的是 `んぐ` → 翻残，必须拦。"""
    assert looks_untranslated("あめいじんぐなタイトル", "あめいじな标题") is True


def test_middle_no_is_not_dropped():
    """中间的 `の` 不能摘 —— `あのね` → `あね` 是把词腰斩了，必须拦。"""
    assert looks_untranslated("あのねなタイトル", "あねな标题") is True
