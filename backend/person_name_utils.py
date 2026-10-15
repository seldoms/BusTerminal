"""Jellyfin People 名字的可用性判断与跨写法匹配。

背景（生产实测）：
- Jellyfin 的 NFO 元数据里会把「収录时间」「未知演员」这类**字段标签**塞进 People 列表，
  被自动关注当成演员建了订阅。
- 同一个演员在 Jellyfin 里用简体中文写法（铃木真夕）、在 BusTerminal 库里用日文汉字写法
  （鈴木真夕），精确匹配必然失败，结果是漏订阅 + 用兜底 id 再建一条重复订阅。
"""

import re
import unicodedata
from typing import Any, Iterable, Optional

# 出现在名字里就说明这不是人名，而是元数据字段标签。
NON_PERSON_KEYWORDS = (
    "时间", "時間", "とき", "日期", "时长", "時長", "长度", "長さ",
    "标签", "標籤", "标签名", "系列", "厂商", "廠商", "制作商", "製作商",
    "发行商", "發行商", "发行", "發行", "制作", "製作", "导演", "導演",
    "監督", "监督", "编剧", "編劇", "字幕", "番号", "品番",
    "分辨率", "大小", "码率", "比特率", "音轨", "声道",
    "未知", "不明", "未詳", "未详", "无", "無", "その他", "其他",
    "収録", "収录", "收录", "収錄", "出品", "配信", "配信日",
    "発売", "発壳", "发售", "販売", "销售", "配信開始", "公開", "公开",
)

# 明确不是人名的整串值（大小写不敏感，比较前会做 NFKC 归一）。
NON_PERSON_NAMES = {
    "unknown", "n/a", "na", "none", "null", "nil", "test", "nobutain",
    "actor", "actress", "cast", "主演", "演员", "演員", "女优", "女優",
    "av女优", "av女優", "出演者", "出演", "歌手", "-", "--", "---",
    "?", "??", "???", ".", "..", "...", "tbd", "todo",
}

_NAME_ALLOWED_CHAR = re.compile(r"[0-9A-Za-z\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
_NAME_FORBIDDEN_CHAR = re.compile(r"[/\\@|<>\[\]{}$%*+=~^`;]")


def normalize_person_name(text: Any) -> str:
    """归一到可比较形式: NFKC + 去空白 + 去括号内容 + 小写。"""
    raw = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not raw:
        return ""
    raw = re.sub(r"[(（【\[][^)）】\]]*[)）】\]]", "", raw)
    raw = raw.replace("：", ":")
    raw = re.sub(r"\s+", "", raw)
    return raw.casefold()


def is_plausible_person_name(text: Any) -> bool:
    """判断 Jellyfin People 里的 Name 是否像一个演员名。"""
    name = str(text or "").strip()
    if not name:
        return False
    compact = re.sub(r"\s+", "", name)
    if len(compact) < 1 or len(compact) > 64:
        return False
    if _NAME_FORBIDDEN_CHAR.search(compact):
        return False
    # URL / 路径 / 明显的技术字段
    lowered = compact.lower()
    if lowered.startswith(("http://", "https://", "www.", "/", "./")):
        return False
    if "." in compact and re.search(r"\.(com|net|org|cn|jp|tv|cc|me|co)$", lowered):
        return False
    if not _NAME_ALLOWED_CHAR.search(compact):
        return False
    if normalize_person_name(name) in NON_PERSON_NAMES:
        return False
    if any(keyword in compact for keyword in NON_PERSON_KEYWORDS):
        return False
    return True


def names_strictly_equal(left: Any, right: Any) -> bool:
    """归一后完全相等（吸收全角/半角、空格、括号别名）。"""
    a = normalize_person_name(left)
    b = normalize_person_name(right)
    return bool(a) and a == b


def names_loosely_equal(left: Any, right: Any) -> bool:
    """同一人的不同写法是否等价（允许少量字符差异）。

    ⚠️ 只能在**候选唯一**时使用: 「鈴木真夕」和「黒木真夕」只差一个字，
    在多人候选里放宽就会把两个人并成一个。调用方必须先确保只有一个人可选。
    """
    if names_strictly_equal(left, right):
        return True
    a = normalize_person_name(left)
    b = normalize_person_name(right)
    if not a or not b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) != len(b):
        shorter, longer = (a, b) if len(a) < len(b) else (b, a)
        return shorter in longer
    diff = sum(1 for x, y in zip(a, b) if x != y)
    return diff <= max(1, (len(a) + 1) // 2)


def match_person_name(text: Any, candidates: Iterable[Any]) -> Optional[str]:
    """在候选名里做严格匹配（归一后相等），找不到返回 None（不猜）。"""
    for candidate in candidates:
        if names_strictly_equal(text, candidate):
            return str(candidate)
    return None


def match_person_name_loose(text: Any, candidates: Iterable[Any]) -> Optional[str]:
    """放宽匹配。**只在候选唯一时调用**，否则会把不同人并成一个。"""
    candidates = list(candidates)
    if len(candidates) != 1:
        return match_person_name(text, candidates)
    if names_loosely_equal(text, candidates[0]):
        return str(candidates[0])
    return None
