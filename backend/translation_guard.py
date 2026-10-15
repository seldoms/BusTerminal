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
from typing import Dict, Iterable, List, Optional, Tuple

# 连续段：假名（含浊音/半浊音的小写区间）+ 汉字 + 全角字母数字，长度 >= 2 才可能是人名
_NAME_LIKE = re.compile(r"[぀-ヿㇰ-ㇿ㐀-䶿一-鿿々〆ヵ-ヶァ-ヶｦ-ﾟ]+")
# 判定「译文里还有假名」用；刻意不含长音符 ー(U+30FC)，它会出现在正常的简体译文里吗？会，
# 例如「重—」类表述少见，但「一百零一」类不含。为稳妥，质量闸门只看**汉字假名**（平/片假名）。
_KANA = re.compile(r"[぀-ゟ゠-ヿ]")
_PLACEHOLDER = re.compile(r"ZZ(\d{1,4})ZZ")


class TitleGuard:
    """按演员表保护标题里的人工名，翻译后原样还原。

    只做「假名串 + 演员表求交」而不是逐个名字全文扫描：标题里的假名连续段就是人名所在，
    逐标题一次正则扫描 + 集合查表即可，10 万级名字表也不会拖慢批量翻译。
    """

    def __init__(self, names: Iterable[str] = (), *, min_len: int = 2):
        self.min_len = max(1, int(min_len))
        self.names = {
            n for n in (str(x or "").strip() for x in names) if len(n) >= self.min_len
        }

    def update_names(self, names: Iterable[str]) -> None:
        self.names.update(
            n for n in (str(x or "").strip() for x in names) if len(n) >= self.min_len
        )

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

    def protect(self, text: str) -> Tuple[str, Dict[str, str]]:
        """返回 (替换后的文本, {占位符: 原串})。未命中任何演员名时原样返回。"""
        original = str(text or "")
        if not original or not self.names:
            return original, {}

        # 长的先替换，避免短名是长名子串时把长名拆坏
        hits = sorted(
            {seg for seg in self._segments(original) if seg in self.names},
            key=len,
            reverse=True,
        )
        if not hits:
            return original, {}

        mapping: Dict[str, str] = {}
        working = original
        for index, name in enumerate(hits):
            token = f"ZZ{index}ZZ"
            # 用 count=1 且从左往右逐个替换；同名多次出现时第一个换成占位、其余保持原样即可
            if token in working:
                continue
            working = working.replace(name, token, 1)
            mapping[token] = name
        return working, mapping

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


def looks_untranslated(source: str, translated: str, *, allowed: Iterable[str] = ()) -> bool:
    """译文里仍有假名 → 判定没翻好。

    `allowed` 是**已经还原回去的艺人名**：按用户口径「艺人名保留日文假名」，
    这些假名本来就该出现在译文里，不能算作「没翻」。所以先把它们从译文里抠掉再判断，
    否则所有合法的译文都会被误杀。
    """
    text = str(translated or "")
    for name in allowed or ():
        if name:
            text = text.replace(str(name), "")
    return bool(_KANA.search(text))


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
    if too_similar(source, dst):
        return "too_similar"
    return None