"""封面来源优先级

实测同一番号三站清晰度（JavDB / DMM / JavBus）：

| 来源 | 分辨率 | 体积 |
| --- | --- | --- |
| JavDB `c0.jdbstatic.com/covers/xx/xxxx.jpg` | 800×538 | ~142 KB |
| DMM `pics.dmm.co.jp/.../xxxpl.jpg` | 590×800 | ~19 KB |
| JavBus `pics/cover/600x` | 已失效 | - |

结论：**JavDB 明显更清晰**，作为首选。

## 为什么需要这一层而不是直接改字段
`save_movie` 里是 `cover = EXCLUDED.cover`，**无条件覆盖**。
若每次入库都去 JavDB 查图，查不到时会用空值覆盖掉已有封面 —— 越维护越糟。
所以这里做两件事：

1. **选优质来源**：按优先级挑，挑不到就保留原样，绝不降级。
2. **记住来源**：用 `cover` 列的域名判断是否已是 JavDB；已是则不再重复请求。
"""
import logging
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 来源优先级：数字越小越优先。空的第一项代表"沿用已有"。
# 之所以保留"沿用已有"在最高位：新片首次入库必然还没有封面，不能因为查不到就留空。
COVER_SOURCE_PRIORITY = (
    "",                # 沿用已有（未查到高清时的安全兜底）
    "jdbstatic.com",   # JavDB，实测 800×538 / 142KB
    "dmm.co.jp",       # DMM pl.jpg，实测 590×800 / 19KB
    "javbus",          # JavBus 兜底
)

# JavDB 的文件名是哈希（如 5navDM.jpg），无法从番号反推，只能靠发现流里抓到的真实 URL。
# 结尾不含引号/逗号/括号 —— `\S+` 会把 JSON 的 `"}` 一起吞进来，产出无效 URL。
_JDB_COVER_RE = re.compile(r'https?://[a-z0-9.\-]*jdbstatic\.com/covers/[^\s"\'<>),;]+', re.I)


def cover_tier(cover_url: Any) -> int:
    """判断一个封面 URL 属于哪一档；未知来源返回最低档（比 JavBus 还靠后）。"""
    text = str(cover_url or "").strip()
    if not text:
        return len(COVER_SOURCE_PRIORITY)
    for index, marker in enumerate(COVER_SOURCE_PRIORITY):
        if marker and marker in text.lower():
            return index
    return len(COVER_SOURCE_PRIORITY)


def is_javdb_cover(cover_url: Any) -> bool:
    return "jdbstatic.com" in str(cover_url or "").lower()


def extract_javdb_cover(text: Any) -> str:
    """从任意文本里提取 JavDB 封面 URL。

    发现流的列表项已带 JavDB 封面，入库时可从这里捞到真实 URL ——
    不需要为每部片子单独去 JavDB 搜索页翻找（那会被反爬且很慢）。
    """
    match = _JDB_COVER_RE.search(str(text or ""))
    return match.group(0) if match else ""


def should_replace_cover(existing: Any, candidate: Any) -> bool:
    """候选封面是否比现有的更好。

    规则：**只升不降**。同档位不覆盖（避免反复请求），
    更差的来源一律拒绝 —— 这是防止"查不到就把好图覆盖成坏图"的关键。
    """
    candidate_text = str(candidate or "").strip()
    if not candidate_text:
        return False
    existing_text = str(existing or "").strip()
    if not existing_text:
        return True
    return cover_tier(candidate_text) < cover_tier(existing_text)


class CoverUpgradeService:
    """按优先级提升封面质量。"""

    def __init__(self, *, db: Any, logger_: Optional[logging.Logger] = None):
        self.db = db
        self.logger = logger_ or logger

    def pick_from_source(self, *, existing: Any, source_cover: Any) -> Tuple[str, bool]:
        """从某个来源（如 JavDB 发现流）拿到的封面里，挑最好的写入。

        返回 (最终封面, 是否发生替换)。
        """
        candidate = str(source_cover or "").strip()
        if not candidate:
            return str(existing or ""), False
        if should_replace_cover(existing, candidate):
            self.logger.debug(
                "Cover upgrade: %s -> %s",
                str(existing)[:60], candidate[:60],
            )
            return candidate, True
        return str(existing or ""), False

    def merge_from_any_text(self, *, existing: Any, text: Any) -> Tuple[str, bool]:
        """从任意文本（详情 JSON / 来源列表项）里捞 JavDB 封面并尝试升级。"""
        return self.pick_from_source(existing=existing, source_cover=extract_javdb_cover(text))

    def needs_upgrade(self, cover_url: Any) -> bool:
        """该封面是否还有升级空间（当前不是 JavDB 来源）。"""
        text = str(cover_url or "").strip()
        if not text:
            return True
        return not is_javdb_cover(text)