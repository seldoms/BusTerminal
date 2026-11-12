"""标题翻译的占位保护与质量闸门。

为什么需要（2026-10-11 实测）：
    用户口径是「作品名意译、**艺人姓名保留日文假名**」。但实测两个模型都守不住这条——
    `qwen3.8:27b` 把 `水野アコ` 译成 `水野绫子`（假名被音译成汉字），
    `hy-mt2` 也把 `水野優香` 译成 `水野优香`（汉字被转写成简体）。
    纯靠 prompt 约束不可靠，实测已给了明确指令它照样音译。所以改成：
      · 翻译**前**把命中演员表的假名串换成占位符，模型看不到它、没有动机改写；
      · 翻译**后**原样还原，口径由代码保证而不是指望模型听话。
    另配质量闸门：译文仍含假名（多半是没翻或翻残了）、与原文字符相似度过高（原文透传）、
    占位符丢失的，一律判为不合格**不入库**，宁可留待处理也不能污染译文表。

占位符用纯 ASCII（模型基本不会改写），形如 `ZZ0ZZ`、`ZZ1ZZ`；编号与还原表一一对应。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Tuple
# 连续段：假名（含浊音/半浊音的小写区间）+ 汉字 + 全角字母数字，长度 >= 2 才可能是人名
_NAME_LIKE = re.compile(r"[぀-ヿㇰ-ㇿ㐀-䶿一-鿿々〆ヵ-ヶァ-ヶｦ-ﾟ]+")
# 判定「译文里还有假名」用；刻意不含长音符 ー(U+30FC)，它会出现在正常的简体译文里吗？会，
# 例如「重—」类表述少见，但「一百零一」类不含。为稳妥，质量闸门只看**汉字假名**（平/片假名）。
_KANA = re.compile(r"[぀-ゟ゠-ヿ]")
# 连续假名**片段**（质量闸门用）：与 `_KANA` 同字集，但按连续段切出来，
# 这样能判断「译文里剩下的这一段假名，原文里是不是也整段存在」。
_KANA_RUN = re.compile(r"[぀-ゟ゠-ヿ]+")
_PLACEHOLDER = re.compile(r"ZZ(\d{1,4})ZZ")

# 复合词分隔符：片名里 `ヴィーナス・コード` 这种「品牌名 + 副标题」写法很常见，
# 模型常**只翻译后半段**（`ヴィーナス·代码`）—— 品牌名留着是刻意的，不算翻残。
# 假名段如果跨过分隔符整体比对，必然对不上 → 会把**正确**的译文误杀。
# 所以比对前先按分隔符断开（2026-11-09 生产实测）。
_COMPOUND_SEPARATORS = "・·･／/｜|"
_COMPOUND_SPLIT = re.compile("[" + re.escape(_COMPOUND_SEPARATORS) + "]")

# 浊音/半浊音归一化：模型有时会把 `ぽ`（半浊音）写成 `ポ`（浊音），
# 或 `ば`→`は`（浊音剥掉）。那是**同一个片名的两种写法**，不是翻残。
# 比对假名段前先归一，两边就归到同一形态。
#
# ⚠️ 关键：`ぽ`(U+307D) / `ポ`(U+30DA) 是**预组合单码位**，光 `translate` 删组合符
# 是拆不开的 —— 必须先 NFD 分解成「基字 + 组合符」再删组合符。
# 2026-11-09 实测：不这么做的话 `私立恵比寿中学 小林歌穂 卒業式「ぽ〜EVER」`
# → `私立惠比寿中学 小林歌穂 退学式「ポ〜EVER」` 会被误判成翻残。
#
# ⚠️ 键必须是**码位(int)**：`str.translate` 按 ordinal 查表，用字符串键的话
# 一条都命中不了，组合符会原样留在串里（2026-11-09 实测才发现）。
_COMBINING_MARKS = {c: None for c in range(0x3099, 0x309D)}
# 片假名 → 平假名的码位区间（`ァ`U+30A1 ~ `ヶ`U+30F6，对应 `ぁ`U+3041 ~ `ゖ`U+3096）
_KATAKANA_RANGE = (0x30A1, 0x30F6)
_KATAKANA_OFFSET = 0x60
# `・`(U+30FB) 也落在上面这个码位区间里，但它**不是假名字母** —— 直接按偏移转
# 会变成 `゛`(U+309B)，混进假名段里污染比对。归一时单独跳过。
_KATAKANA_SKIP = {0x30FB}


def _normalize_kana(text: str) -> str:
    """假名归一化（**仅用于比对**，不用于展示）。

    做三件事：
      1. NFD 分解（`ぽ` → `ほ` + U+309C，`ポ` → `ホ` + U+3099）
      2. 去掉浊音/半浊音组合符 → `ぽ` / `ポ` / `ぽ` 归一
      3. 片假名转平假名（`ホ` → `ほ`）→ 假名/片假名写法差异也归一

    只做 1+2 不够：`ぽ`（平假名半浊音）归一后是 `ほ`，`ポ`（片假名浊音）归一后是
    `ホ` —— 仍不相等，所以第 3 步必须做。
    """
    decomposed = unicodedata.normalize("NFD", str(text or ""))
    stripped = decomposed.translate(_COMBINING_MARKS)
    out = []
    for ch in stripped:
        code = ord(ch)
        if code in _KATAKANA_SKIP:
            # `・` 是分隔符，不是假名字母 —— 原样留着（后面会按分隔符断段）
            out.append(ch)
            continue
        if _KATAKANA_RANGE[0] <= code <= _KATAKANA_RANGE[1]:
            code -= _KATAKANA_OFFSET
        out.append(chr(code))
    return "".join(out)


def _kana_runs(text: str) -> List[str]:
    """把文本切成「连续假名段」，每段先做归一化。

    **复合词分隔符处要断开**：`ヴィーナス・コード` 是两个元素，不该当成
    一整段比 —— 模型只翻译后半段（`ヴィーナス·代码`）时，整段比对必然对不上，
    会把「保留品牌名 + 翻译后半段」这种正确译文误杀（2026-11-09 生产实测）。

    顺序是「先按分隔符切开原文，再各自归一 + 抽假名段」：
    反过来的话 `・` 会被片假名→平假名的偏移规则转成 `゛`，混进假名段。
    """
    out: List[str] = []
    for chunk in _COMPOUND_SPLIT.split(str(text or "")):
        for piece in _KANA_RUN.findall(_normalize_kana(chunk)):
            if piece:
                out.append(piece)
    return out


# 「差出来的部分」只有这些才算可原谅：演员名后面挂的敬称（`さん`/`ちゃん`/`くん`）
# 和 `の`。模型把 `〇〇さん` 译成 `〇〇` 是正常的；把 `アメイジング` 译成 `アメイジ`
# 差的是 `ング` —— 那不是原谅得了的差别，是**翻残**，必须拦。
_DROPPABLE_KANA_TOKENS = ("さん", "ちゃん", "くん", "の")


# 敬称后缀：片名里演员名常写成「艺名 + 敬称」（`あいちゃん` / `なつさん` / `みなみくん`）。
# 库里存的是艺名本身（`あい`、`なつ`），2 字名进不了子串匹配（会和公共词相撞：
# `りさまる` 里的 `りさ`、`えっちな女` 里的 `ちな`），但**敬称后缀**是干净的区分信号
# —— `まる` / `女` 不是敬称，`ちゃん` / `さん` 是。2026-11-09 生产实测：
# 修这个之前 `部活帰り あいちゃん（18）` 这类标题 100% 被质量闸门误杀。
_HONORIFICS = ("ちゃん", "さん", "くん", "様", "君", "姉", "兄", "先生", "孃")
# 敬称前最多回看几个字找名字（够覆盖「吉野れな」这类 4~5 字艺名，又不至于整段乱吞）
_HONORIFIC_NAME_MAX_LEN = 8


class TitleGuard:
    """按演员表保护标题里的人工名，翻译后原样还原。

    匹配分三条路（2026-11-09 生产实测定稿）：
      1. **整段精确匹配**：标题里的连续段本身就是库里的名字。
      2. **段内子串匹配**（名字 >= 3 字）：段里嵌着演员名。
         标题 `乳首開発電車8 小坂ひまりさんの紙パンティとチェキ付き` 的连续段是
         `小坂ひまりさんの紙パンティとチェキ付き` —— 整段查表必然查不到，
         但库里只有 `小坂ひまり` → 不保护 → 模型译文里留下假名 →
         质量闸门判 `kana_in_output` 拦下。实测开回填后 **33% 的条目**因此被误杀。
      3. **敬称后缀**（2 字名专用）：`あいちゃん` / `なつさん` 这种「艺名 + 敬称」写法。
         2 字名不能进子串匹配（会和公共词相撞：`りさまる` 里的 `りさ`、
         `えっちな女` 里的 `ちな`），但敬称是干净的区分信号 —— `まる`/`女` 不是敬称，
         `ちゃん`/`さん` 是。库里存的是艺名本身（`あい`、`なつ`），片名里带敬称。

    保护单位是「名字（+敬称）」整体：整段换成占位符，模型看不到就没有动机改写，
    还原后原样回来 —— 这正是用户口径「艺人姓名保留日文假名」的代码保证。
    """

    def __init__(
        self,
        names: Iterable[str] = (),
        *,
        min_len: int = 2,
        match_min_len: int = 3,
        honorific_name_max_len: int = 8,
    ):
        self.min_len = max(1, int(min_len))
        # 段内子串匹配的最小名字长度（2026-11-09 实测定为 3）。
        # 2 字名在标题里几乎必然和公共词片段相撞 —— 实测新护栏第一版把
        # `えっちな女` 里的 `ちな`、`丸山りさ` 里的 `りさ`、`「あやこ 53歳」` 里的 `あや`
        # 都当成了人名 → 把本该翻译的词替换成占位符，反而破坏译文。
        # 2 字名改走「敬称后缀」那条路（见类文档）。
        self.match_min_len = max(1, int(match_min_len))
        # 敬称前最多回看几个字找名字（够覆盖「吉野れな」这类 4~5 字艺名，
        # 又不至于把整段都当成名字）。
        self.honorific_name_max_len = max(1, int(honorific_name_max_len))
        self.names = {
            n for n in (str(x or "").strip() for x in names) if len(n) >= self.min_len
        }
        self._names_by_len: Dict[int, set] = {}
        self._reindex()

    def _reindex(self) -> None:
        """按名字长度建索引，供「段内子串查找」用。

        只索引长度 >= match_min_len 的名字：更短的只走「整段精确匹配」
        和「敬称后缀」两条路，不参与子串枚举（否则 2 字名会到处误命中）。
        """
        by_len: Dict[int, set] = {}
        for name in self.names:
            if len(name) < self.match_min_len:
                continue
            by_len.setdefault(len(name), set()).add(name)
        self._names_by_len = by_len

    def update_names(self, names: Iterable[str]) -> None:
        self.names.update(
            n for n in (str(x or "").strip() for x in names) if len(n) >= self.min_len
        )
        self._reindex()

    @property
    def ready(self) -> bool:
        return bool(self.names)

    def _segments(self, text: str) -> List[str]:
        """标题里所有像人名的连续段（长度达标）。"""
        return [
            seg
            for seg in _NAME_LIKE.findall(text or "")
            if len(seg) >= self.min_len
        ]

    def _spans_in_segment(self, segment: str) -> List[Tuple[int, int]]:
        """段内所有命中位置的 (起, 止)（字符下标，相对段首）。

        三条匹配路都汇到这里，返回**带位置**的区间而不是字符串 ——
        只有带位置才能在多个名字重叠时挑出最长的那个（`愛瀬まりな` 与 `まりな`），
        也才能把「名字 + 敬称」当成一个整体保护单位。
        """
        spans: List[Tuple[int, int]] = []
        seg_len = len(segment)
        if not seg_len:
            return spans

        # 1) 整段精确匹配：段本身就是库里的名字（2 字名也走这条）
        if segment in self.names:
            spans.append((0, seg_len))

        # 2) 段内子串匹配：只认 >= match_min_len 的名字
        for name_len, names in self._names_by_len.items():
            if name_len > seg_len:
                continue
            for start in range(0, seg_len - name_len + 1):
                if segment[start:start + name_len] in names:
                    spans.append((start, start + name_len))

        # 3) 敬称后缀：「艺名 + 敬称」整体命中（2 字名的主要通路）
        for honor_start, honor_end in self._honorific_spans(segment):
            # 往前回看，找**紧贴敬称**且在库里的名字。
            # 从最长候选开始（`吉野れな` 优先于 `れな`）；候选必须是敬称前紧邻的那一段，
            # 所以 `りさまるちゃん` 里的 `りさ` 不会被误当成「名字+敬称」——
            # `りさ` 与 `ちゃん` 之间还隔着 `まる`，不构成紧邻。
            low = max(0, honor_start - self.honorific_name_max_len)
            for start in range(low, honor_start):
                candidate = segment[start:honor_start]
                if len(candidate) < self.min_len or candidate not in self.names:
                    continue
                # 词边界要求：候选前面不能是假名。
                # `綺麗なお姉さん` 里的 `なお`（恰好是库里 2 字名）前面是假名 `い`
                # → 它是「なお姉さん」这个词的一部分，不是人名+敬称 → 不保护。
                # 段首（start == 0）天然满足：段与段之间是空格/标点。
                if start > 0 and _KANA.search(segment[start - 1]):
                    continue
                spans.append((start, honor_end))
                break

        return spans

    @staticmethod
    def _honorific_spans(segment: str) -> List[Tuple[int, int]]:
        """段里所有敬称出现的位置。"""
        spans: List[Tuple[int, int]] = []
        for honorific in _HONORIFICS:
            start = 0
            while True:
                idx = segment.find(honorific, start)
                if idx < 0:
                    break
                spans.append((idx, idx + len(honorific)))
                start = idx + 1
        return spans

    def _names_in_segment(self, segment: str) -> List[str]:
        """段内所有命中的名字（去重叠后按长度降序）。

        保留这个方法是为了兼容既有调用与测试；`protect()` 走的是带位置的
        `_spans_in_segment()`，能正确处理重叠与「名字+敬称」整体保护。
        """
        return sorted(
            {segment[start:end] for start, end in self._spans_in_segment(segment)},
            key=len,
            reverse=True,
        )

    def protect(self, text: str) -> Tuple[str, Dict[str, str]]:
        """返回 (替换后的文本, {占位符: 原串})。未命中任何演员名时原样返回。"""
        original = str(text or "")
        if not original or not self.names:
            return original, {}

        # 收集命中区间：段内位置 → 全文位置
        spans: List[Tuple[int, int]] = []
        for match in _NAME_LIKE.finditer(original):
            segment = match.group(0)
            if len(segment) < self.min_len:
                continue
            offset = match.start()
            for start, end in self._spans_in_segment(segment):
                spans.append((offset + start, offset + end))

        if not spans:
            return original, {}

        # 去重叠：同一处命中取**最长**的（`愛瀬まりな` 优先于 `まりな`）
        spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        kept: List[Tuple[int, int]] = []
        last_end = -1
        for start, end in spans:
            if start < last_end:
                continue
            kept.append((start, end))
            last_end = end
        if not kept:
            return original, {}

        # 从后往前替换，前面的偏移不受影响
        mapping: Dict[str, str] = {}
        out = original
        for index in range(len(kept) - 1, -1, -1):
            start, end = kept[index]
            token = "ZZ%dZZ" % index
            out = out[:start] + token + out[end:]
            mapping[token] = original[start:end]
        return out, mapping

    def restore(self, text: str, mapping: Dict[str, str]) -> str:
        """把占位符还原回原串；未识别的占位符原样保留（便于质量闸门判失败）。"""
        out = str(text or "")
        if not mapping:
            return out
        for token, name in mapping.items():
            out = out.replace(token, name)
        return out

    def leftover_tokens(self, text: str, mapping: Dict[str, str]) -> List[str]:
        """译文里仍存在本次用过的占位符 → 说明模型把占位符拆散或丢了。"""
        out = str(text or "")
        return [token for token in mapping if token in out]


_LIST_PREFIX = re.compile(r"^\s*(?:\d+\s*[.、)）:]|第\s*\d+\s*[条項个]|[①②③④⑤⑥⑦⑧⑨⑩])")

# ---------------------------------------------------------------------------
# 拒答话术（KI-054）
#
# 带内容安全对齐的模型（实测 `hunyuan-mt-7b` 12 条命中 2 条）会把整条输入换成一句
# 中文道歉语返回，行数照样对齐、一个占位符都不丢，前面几道闸门全放行。
#
# 这里**不**走「关键词同现」的路子：单查「色情」会误命中 1225 条、单查「抱歉」会误命中
# 168 条（日文 `ごめんなさい` 的正常译文就是「抱歉/对不起」）。改成**句式模板**——
# 拒答话术本来就是有限的十几种固定模板（KI-054 已逐条枚举），模板匹配天然比关键词精确：
#   · `无法拒绝的服务`、`我用无法解释的方式帮助你`、`妻子无法满足，于是向邻居提供了帮助`
#     这些正常标题里 A/B 两类词都在，但都不是模板开头，一条也不会被误杀。
# ---------------------------------------------------------------------------
_REFUSAL_PATTERNS = (
    # 抱歉，我暂时无法提供…／我很抱歉，但我无法协助翻译…
    # 必须带服务类词：正常标题里「很抱歉，我无法忘记那个男人」不算拒答。
    # 末组要含「请求」：模型最常见的整句拒答是「我很抱歉，但我无法协助完成此请求。」，
    # 只认「翻译/服务」会把它漏过去（2026-11-05 在 Jellyfin 名称里实测到这条）。
    # 前缀放宽到 16 字符：脏数据常留着番号（`ROYD-305 我很抱歉…` / `DSOD-097 这种内容…`）。
    re.compile(
        r"^[^。！？]{0,16}(抱歉|对不起)[^。！？]{0,10}(无法|不能|不便|不予)"
        r"[^。！？]{0,12}(翻译|服务|帮助|协助|处理|回答|请求)"
    ),
    # 该内容包含色情低俗信息…／此视频涉及违规内容…
    re.compile(
        r"^(该|此)?(内容|文本|标题|影片|视频|作品)[^。！？]{0,20}"
        r"(违规|违反|色情|低俗|不良|敏感|不合规)"
    ),
    # 色情低俗内容，无法提供翻译…／IPZZ-91情色内容限制，无法提供…
    # 不锚句首：库里的实测脏数据就长这样（番号残留在前面）
    re.compile(r"(色情|情色|低俗|不良|敏感)[^。！？]{0,8}内容[^。！？]{0,8}(无法|不能|限制|不予)"),
    # 我无法为你提供翻译服务／无法进行翻译／不能完成翻译
    re.compile(
        r"^(我)?(无法|不能|不便)(为你|为您|给你|给您|对此|对您)?"
        r"(提供|进行|完成|给出|协助|帮助)"
        r"[^。！？]{0,10}(翻译|服务|帮助|协助|回答|处理)"
    ),
    # 无法对性交易或色情内容进行翻译／私无法提供此类内容翻译
    # 中间不许跨句读，末尾只认「翻译」——带上「服务」就会把
    # 「无法拒绝发情客人 提供隐藏菜单性服务」这种正常标题一起吃掉（生产实测）
    re.compile(r"无法[^。！？，、；]{0,10}(进行|提供|完成|协助)[^。！？，、；]{0,14}翻译"),
    # 性处理相关内容违反内容规范，我无法…
    # 必须带「无法/不能」：库里大量正常标题本身就是「【因违反政策立即停止配信！】…」
    re.compile(
        r"(违反|不符|触及)[^。！？]{0,10}(规范|政策|规定|准则|条例)"
        r"[^。！？]{0,20}(无法|不能|不便|不予)"
    ),
    # 我实在无法翻译此类含有色情低俗内容的文本（结尾必须落到「内容/文本/请求」，
    # 否则「无法翻译的爱」这类正常歌名会中招）
    re.compile(r"(无法|不能|拒绝)[^。！？]{0,6}翻译[^。！？]{0,16}(内容|文本|请求|此类)"),
    # 您的请求涉及色情低俗内容，我无法为您提供相关帮助
    re.compile(r"(您的)?请求[^。！？]{0,10}(色情|情色|低俗|敏感|违规)[^。！？]{0,10}(无法|不能|不予)"),
    # 无法识别该内容，请提供有效的影视标题（上游元评论，不是译文）
    re.compile(r"无法识别(该|此)?内容|请提供有效(的)?(影视|视频)?标题"),
    # 作为一个AI语言模型…（必须带「作为/身为」：库里有 431 条「AI重制版…」的正常标题）
    re.compile(r"(作为|身为)[^。！？]{0,6}(AI|ai|人工智能|语言模型|助手)"),
)

# 番号：`SSIS-836` / `HODV-20544`。译文必须原样保留，否则和 movie_actresses、amane 全部对不上
_MOVIE_CODE = re.compile(r"[A-Z]{2,6}-\d{2,5}")


def _normalize_for_code(text: str) -> str:
    """只留字母数字再比：实测模型会把连字符写成 en dash、全角减号或者干脆丢掉。

    这些是字形差异，番号本身没变，不该整条判失败；而 `MIMK-284 → MIMK-2026-06-12`
    这种真的改写了番号的，归一化后 `MIMK284` 依然不在 `MIMK20260612` 里，照样拦得住。
    """
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def detect_refusal(text: str, *, max_length: int = 60) -> bool:
    """译文本身是「抱歉，我无法提供翻译」这类拒答话术 → True。

    带假名的直接排除（艺人名保留是合规的，那不可能是拒答）；
    长度上限兜底：正常标题译文很少这么短，长文本里偶然命中模板的概率也高。
    """
    value = str(text or "").strip()
    if not value or len(value) > max_length:
        return False
    if _KANA.search(value) or _LIST_PREFIX.match(value):
        return False
    return any(pattern.search(value) for pattern in _REFUSAL_PATTERNS)


def missing_codes(source: str, translated: str) -> List[str]:
    """原题里的番号在译文里消失了 → 返回丢失清单。

    实测两种模型都会犯（`hunyuan-mt-7b` 把 `MIMK-284` 粘成 `MIMK-2026-06-12`，
    `Qwen2.5-14B-Abliterated` 直接整条漏掉）。番号错位会让影片与演员、amane 的
    关联全部错行，比译文难看严重得多，所以宁可拒绝入库。
    """
    codes = list(dict.fromkeys(_MOVIE_CODE.findall(str(source or ""))))
    if not codes:
        return []
    haystack = _normalize_for_code(translated)
    return [code for code in codes if _normalize_for_code(code) not in haystack]


def _strip_honorific_suffix(text: str) -> str:
    """摘掉尾部的一个敬称（`いおりさん` → `いおり`）。

    只摘**尾部一个**：片名里 `○○さんの○○` 这种中段的敬称属于内容本身，
    摘掉就变成了另一种标题。
    """
    value = str(text or "")
    for honorific in _HONORIFICS:
        if value.endswith(honorific) and len(value) > len(honorific):
            return value[: -len(honorific)].strip()
    return value


def _is_name_only_form(text: str, names) -> bool:
    """整段文本是不是「某个艺人名（或艺名 + 敬称）」本身。

    判定用**字符串相等**，不是假名分析 ——
    日文艺名两种写法都有：`いおり`（假名）、`香椎花乃`（全汉字）。
    全汉字名的假名段是空的，靠「假名段是否被艺人名覆盖」判断会直接漏掉
    （2026-11-09 生产实测：`香椎花乃` / `奥田咲` / `榊原萌` / `新谷未来`
    每轮都被 `too_similar` 拦下，永远翻不出来）。

    两步判定：原串本身在名字表里 → 命中；摘掉尾部敬称后在名字表里 → 也命中。
    这样不用为 12 万个名字预先物化「名字 + 敬称」的变体集（那要 ~80MB 内存）。
    """
    value = str(text or "").strip()
    if not value:
        return False
    if value in names:
        return True
    core = _strip_honorific_suffix(value)
    return bool(core) and core in names


def _strip_droppable(text: str) -> str:
    """把一段假名**尾部**的「敬称 / の」摘掉，剩下的才是实词部分。

    `ひまりさんの` → `ひまり`；`あめいじんぐ` → `あめいじんぐ`（摘不掉）。

    只摘尾部、不摘中间：`あのね` 不该被摘成 `あね`（那是把词腰斩了，
    不是「省掉了敬称」）。中间出现的 `の` 属于词内部，摘掉就是误判。
    """
    rest = str(text or "")
    changed = True
    while changed and rest:
        changed = False
        for token in _DROPPABLE_KANA_TOKENS:
            if rest.endswith(token) and len(rest) > len(token):
                rest = rest[: -len(token)]
                changed = True
                break
    return rest


def _run_matches(run: str, src_runs: Iterable[str]) -> bool:
    """译文这一段假名，能不能在原文里找到对应的一段。

    判定不是「整段相等」—— 那会把两种情况混为一谈：
      · `ひまり`（原文 `ひまりさんの`）→ 差的是敬称 → 合法（模型保留了专名）
      · `あめいじ`（原文 `あめいじんぐ`）→ 差的是 `んぐ` → 翻残 → 必须拦
    所以要求两者**只差「敬称 / の」** —— 两边都摘一遍敬称/`の` 后相等即可
    （双向：原文 `○○さんの` ↔ 译文 `○○の`、原文 `○○の` ↔ 译文 `○○` 都放行）。

    「保留品牌名 + 翻译后半段」（`ヴィーナス・コード` → `ヴィーナス·代码`）这类
    在**断段**层面就处理掉了：`_kana_runs()` 已按复合词分隔符把原文切成
    `うぃーなす` / `こーど` 两段，译文那段 `うぃーなす` 与原文段直接相等 → 放行。
    """
    for src_run in src_runs:
        if run == src_run:
            return True
        # 「只差敬称 / の」是**双向**的：原文 `○○さんの` ↔ 译文 `○○の`、
        # 原文 `○○の` ↔ 译文 `○○` 都该放行，所以两边都要摘一遍再比。
        if _strip_droppable(src_run) == _strip_droppable(run):
            return True
    return False


def _allowed_kana_runs(allowed_names: Iterable[str]):
    """把 `allowed`（已还原的艺人名）拆成「假名段集合」与「归一化整串」。"""
    runs = set()
    blobs = []
    for name in allowed_names or ():
        name = str(name or "")
        if not name:
            continue
        blobs.append(_normalize_kana(name))
        runs.update(_kana_runs(name))
    return runs, blobs


def _run_covered_by_name(run: str, runs, blobs) -> bool:
    """这一段假名是不是属于某个艺人名（它本身就是，或含在某个名字里）。"""
    return run in runs or any(run in blob for blob in blobs)


def is_artist_name_only(source: str, translated: str, *, allowed: Iterable[str] = ()) -> bool:
    """整条标题**就是艺人名本身**（`香椎花乃` / `いおりさん` / `M.Kちゃん`）。

    这种标题没有「作品名」可译，译文与原文相同是**正确**结果 ——
    既不该判 `kana_in_output`，也不该判 `too_similar`。

    2026-11-09 生产实测踩到两次：
      · `いおりさん`：整条被护栏换成 `ZZ0ZZ`，模型如实回 `ZZ0ZZ`，
        还原后译文 = 原文 → `too_similar` 判失败 → 这条永远翻不出来。
      · `香椎花乃`：**全汉字、零假名** —— 旧实现靠「假名段是否被艺人名覆盖」
        判断，这种名字的假名段是空的 → 直接漏判 → 每轮都被 `too_similar` 拦下。
        更糟的是它随后落到**没有护栏**的兜底翻译源，被音译成 `伊吕波` 之类
        （实测已入库），违反「艺人姓名保留日文假名」的用户口径。
    """
    src = str(source or "").strip()
    dst = str(translated or "").strip()
    if not src or not dst:
        return False
    names = {str(a).strip() for a in (allowed or ()) if str(a or "").strip()}
    if not names:
        return False
    # 主判据：原文与译文**都是**「某个艺人名（或艺名 + 敬称）」本身。
    # 只判原文那一侧是不够的 —— 否则「模型把名字换成了另一个词」也会被放过。
    if _is_name_only_form(src, names) and _is_name_only_form(dst, names):
        return True
    # 兼容口径：标题由多个「名字 + 敬称」拼成时，用假名段覆盖判断。
    dst_runs = _kana_runs(dst)
    if not dst_runs:
        return False
    runs, blobs = _allowed_kana_runs(names)
    return all(_run_covered_by_name(run, runs, blobs) for run in dst_runs)


def is_name_only_title(text: str, *, names: Iterable[str] = (), min_len: int = 2) -> bool:
    """整条标题**就是艺人名**（不需要翻译）。

    给翻译链路入口用：这种标题的「正确译文」就是它自己 ——
    送进翻译链路纯属白烧一次 LLM 调用，更糟的是会在兜底阶段被
    没有护栏的百度/Google 音译成中文（实测 `いおりさん` → `伊吕波`）。

    `min_len` 与护栏的 `min_len` 对齐（默认 2）：1 字的名字护栏本来就不保护
    （`桜` 这种字既可能是人名也可能是普通词），这种标题照常翻译。

    `names` 传**艺人名集合**（护栏的 `names` 属性即可），不传就永远返回 False。
    """
    value = str(text or "").strip()
    if len(value) < max(1, int(min_len)):
        return False
    return _is_name_only_form(value, names or ())


def looks_untranslated(source: str, translated: str, *, allowed: Iterable[str] = ()) -> bool:
    """译文里仍有**原文没有的**假名 → 判定没翻好。

    口径为什么是「原文里找得到对应的一段」（2026-11-09 生产实测改）：
    用户口径是「作品名意译、**艺人姓名保留日文假名**」—— 译文里出现假名
    本身**不是**失败信号，是「模型保留了原文里的专名」的正常结果。
    旧口径「译文里有任何假名就判失败」把两者混为一谈，实测开回填后
    **33% 的条目被误杀**（每条白白消耗一次 10 条的 LLM 请求）：

      · `First Memory 〜ひめごと〜` → `初次记忆 〜ひめごと〜`
        副标题保留原文假名，这是**正确的**译文，被拦下纯属误杀。
      · `私立恵比寿中学 小林歌穂 卒業式…` → `私立惠比寿中学 小林歌穂 退学式…`
        演员名保留日文（库里没这条演员记录，护栏管不到），译文正确，仍被拦。
      · `DOPAMIX-WORLD アメイジング` → `DOPAMIX-WORLD アメイジ音`
        模型把片名**翻残**了（`ング`→`音`），这是**真失败**，必须拦。

    新判据（`_run_matches`）：译文里的每一段假名，都必须能在原文里找到
    一段**只差「敬称 / の」**的对应段。
      · 原文 `ひまりさんの` ↔ 译文 `ひまり` → 差 `さん`+`の` → 放行（保留专名）
      · 原文 `アメイジング` ↔ 译文 `アメイジ` → 差 `ング` → 拦（翻残）

    原文整段照传（`src == dst`）单独判失败 —— 那就是压根没翻。
    唯一例外：整条标题就是艺人名本身（`M.Kちゃん`），保留日文才是对的。

    `allowed` 是已还原回去的艺人名：它们的假名本来就该出现在译文里，
    不算「没翻」。译文里含有这类名字的假名段一律放行。
    """
    dst = str(translated or "")
    if not _KANA.search(dst):
        return False

    src = str(source or "")
    src_runs = set(_kana_runs(src))
    dst_runs = _kana_runs(dst)
    allowed_names = [str(a) for a in (allowed or ()) if a]
    allowed_runs, allowed_blobs = _allowed_kana_runs(allowed_names)

    # 整条标题就是艺人名本身（`香椎花乃` / `いおりさん`）→ 保留日文是**正确**结果，
    # 既不该判「没翻」，也不该判「原文照传」。这条要在下面的片段比对**之前**放行：
    # `香椎花乃さん` 这类「汉字艺名 + 假名敬称」的译文里有一段假名（`さん`），
    # 不放行就会被当成「原文没有的假名」拦掉。
    if is_artist_name_only(src.strip(), dst.strip(), allowed=allowed_names):
        return False

    def _covered_by_name(run: str) -> bool:
        return _run_covered_by_name(run, allowed_runs, allowed_blobs)

    # 原文整段照传 → 压根没翻（这条优先于下面的片段比对）。
    # 例外：整条标题就是艺人名（`M.Kちゃん`），此时保留日文才是正确译文。
    if src.strip() and src.strip() == dst.strip():
        if not all(_covered_by_name(run) for run in dst_runs):
            return True

    for run in dst_runs:
        if _covered_by_name(run):
            # 这一段假名属于已还原的艺人名 → 合规
            continue
        if _run_matches(run, src_runs):
            # 原文里有对应的一段（只差敬称/の）→ 模型保留的是原文专名，合规
            continue
        return True
    return False


def too_similar(source: str, translated: str, *, threshold: float = 0.92) -> bool:
    """译文与原文几乎一样 → 模型直接把原文透传了。"""
    src = (source or "").strip()
    dst = (translated or "").strip()
    if not src or not dst:
        return False
    if src == dst:
        return True
    shorter, longer = (src, dst) if len(src) <= len(dst) else (dst, src)
    if not shorter:
        return False
    # 用集合重合度近似字符相似度，避免引入依赖
    common = len(set(shorter) & set(longer))
    return (common / len(set(shorter))) >= threshold and abs(len(src) - len(dst)) <= 2


def is_low_quality(
    source: str,
    translated: str,
    *,
    leftover_tokens: Optional[List[str]] = None,
    allowed: Iterable[str] = (),
) -> Optional[str]:
    """质量闸门：不合格返回原因字符串，合格返回 None。

    `allowed` 传已还原的艺人名（它们的假名是合规的，见 `looks_untranslated`）。

    调用方应在**写入 translations 表之前**调用本函数——坏译文一旦落库，
    后续会以它为准继续传播，清理成本远高于当场拒绝。
    """
    dst = (translated or "").strip()
    if not dst:
        return "empty"
    if leftover_tokens:
        return f"placeholder_lost:{','.join(leftover_tokens[:3])}"
    # 模型有时不翻译，而是把输入当成清单逐条编号输出（实测 `1. 催眠操控人妻：…`）。
    # 这种译文既错位又带原文残留，必须挡住，否则整批对齐会被它带偏。
    if _LIST_PREFIX.match(dst):
        return "list_prefix"
    if looks_untranslated(source, dst, allowed=allowed):
        return "kana_in_output"
    # 整条标题就是艺人名（`いおりさん`）：译文与原文相同是**正确**结果，
    # 不能拿「原文照传」当失败 —— 否则这类标题永远翻不出来。
    if not is_artist_name_only(source, dst, allowed=allowed) and too_similar(source, dst):
        return "too_similar"
    # 拒答话术是合法中文、无假名、占位符一个不丢，前面几道全放行 → 必须单列一道
    if detect_refusal(dst):
        return "refusal"
    lost = missing_codes(source, dst)
    if lost:
        return f"code_lost:{','.join(lost[:3])}"
    return None