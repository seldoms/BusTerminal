"""本地 Ollama 大模型翻译（内网 qwen3.8、hy-mt2 等推理/翻译模型）。

要点（实测）：
- 必须走原生 /api/chat：OpenAI 兼容接口忽略 think 参数，推理模型的思考链关不掉，会慢 2~27 倍。
- think=False：翻译属于有明确格式要求的任务，不需要思考链。
- Ollama 默认 OLLAMA_NUM_PARALLEL=1，同一时刻只能跑一个请求，调用方必须串行（翻译锁已经保证）。
- 质量：模型守不住「艺人名保留日文假名」这条口径（实测 qwen3.8:27b 把 `水野アコ` 译成 `水野绫子`、
  hy-mt2 把 `水野優香` 译成 `水野优香`）。因此翻译前后加占位保护与质量闸门，见 translation_guard。
"""

import json
import logging
import urllib.request

from translation_guard import TitleGuard, is_low_quality

logger = logging.getLogger(__name__)

# 批量协议：输入输出都是 JSON 字符串数组。
#
# 之前用的是「换行拼接 → 按行切分」，两个毛病：
# 1. 行数对不上就整批作废 —— 模型偶尔多回一行空行/说明，整批 10 条全丢；
# 2. 译文里自带换行就彻底错位（每条对一行，标题本身含换行就串行）。
# 换成 JSON 数组后，长度与顺序由结构本身保证，解析失败还能退回逐行兜底。
#
# ⚠️ 但「结构保证」只是协议层面的保证 —— 模型自己数错数照样会发生：
# 实测 12 条里偶尔只回 11 条（`done_reason=stop`，不是被截断，就是它漏了一条）。
# 对策有三层：① prompt 里显式写明条数；② 入参去重（重复项会被模型当冗余）；
# ③ 对齐失败时**二分重试**，不整批丢。
#
# 条数写进 system prompt 而不是 user message：入参必须保持**纯 JSON 数组**
# （老协议靠它对齐），加一行说明会改变协议形状。
SYSTEM_PROMPT_TEMPLATE = (
    "你是影视标题翻译助手。用户会给出一个 JSON 字符串数组（共 {n} 条），"
    "请把数组里的每个日文/英文标题翻译成简体中文，"
    "返回一个**恰好 {n} 个元素**的 JSON 字符串数组，顺序与输入一一对应。"
    "保留番号、日期、数字等非文字内容，不要编号、不要解释、不要输出原文、"
    "不要加代码块标记，只输出 JSON 数组本身。"
)

# 对齐失败时二分重试的最大层数。12 条拆两次 → 最差 3 条一批；
# 单元素数组必然对齐，所以再往下拆没有意义（纯属多发请求）。
MAX_BISECT_DEPTH = 2


def build_system_prompt(count: int) -> str:
    """按条数渲染 system prompt。

    模型自己数 12 经常数错（实测 12 条里只回 11 条），把条数写进 prompt
    能显著降低错数率 —— 这比事后补救便宜得多。
    """
    return SYSTEM_PROMPT_TEMPLATE.format(n=int(count))


class OllamaTranslator:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: int = 300,
        batch_size: int = 12,
        num_predict: int = 4096,
        temperature: float = 0.2,
        guard: TitleGuard = None,
        strict_quality_gate: bool = False,
    ):
        self.base_url = str(base_url or "").rstrip("/")
        self.model = model
        self.timeout = timeout
        self.batch_size = batch_size
        self.num_predict = num_predict
        self.temperature = temperature
        self.guard = guard
        # 默认 False：不合格译文仍然返回给调用方（保持旧行为，便于灰度对比）。
        # 生产回填应置 True，让不合格条目直接不入库。
        self.strict_quality_gate = bool(strict_quality_gate)

    def translate_batch(self, texts, *, _depth=0):
        """一次请求翻译一小批，返回 {原文: 译文}。

        带占位保护时：先换掉标题里的演员假名串 → 翻译 → 原样还原并过质量闸门。
        `strict_quality_gate=True` 时不合格条目直接从结果里剔除（调用方视作未翻译，
        下一轮再排队，不会用坏译文污染 translations 表）。

        **对齐失败会二分重试**（`_depth` 是当前层数）：实测模型 12 条里偶尔只回
        11 条（`done_reason=stop`，不是被截断，是它自己漏了一条）。整批作废等于
        白等一次 20 秒的请求还丢掉 12 条译文 → 拆成两半各试一次，最差拆到 3 条。
        """
        if not texts:
            return {}

        guard = self.guard if (self.guard and self.guard.ready) else None
        # 完全相同的标题只发一次：模型会把重复项当冗余，回的条数就对不上。
        # 结果按原文索引，重复项自动共用同一份译文（占位符各按自己的映射还原）。
        unique_texts = list(dict.fromkeys(texts))
        mappings = {}
        payloads = []
        for text in unique_texts:
            if guard is not None:
                protected, mapping = guard.protect(text)
                payloads.append(protected)
                mappings[text] = mapping
            else:
                payloads.append(text)
                mappings[text] = {}

        # 显式告诉模型条数：它自己数 12 经常数错（少回一条），给它个数能少错很多。
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": build_system_prompt(len(payloads))},
                # JSON 数组入参：一条标题一个元素，不会因为标题里含换行而错位
                {"role": "user", "content": json.dumps(payloads, ensure_ascii=False)},
            ],
            "stream": False,
            "think": False,
            "options": {
                "temperature": self.temperature,
                # 输出上限按**入参实际长度**给：译文长度≈原文，给太少会被
                # num_predict 截断 → JSON 残缺 → 整批作废（实测 1024 对 12 条
                # 长标题偏紧）。每条再留 2 倍余量（JSON 包装 + 译长浮动）。
                "num_predict": min(self.num_predict, 256 + 2 * sum(len(p) for p in payloads)),
            },
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.load(response)

        content = (body.get("message") or {}).get("content") or ""
        lines = self._parse_translations(content, len(unique_texts))
        if lines is None:
            # 对不上 → 二分重试：拆两半各试一次，比整批丢掉划算得多。
            if len(unique_texts) > 1 and _depth < MAX_BISECT_DEPTH:
                mid = len(unique_texts) // 2
                logger.info(
                    "本地大模型译文未对齐（%d 条），拆成 %d + %d 重试",
                    len(unique_texts),
                    mid,
                    len(unique_texts) - mid,
                )
                merged = {}
                merged.update(self.translate_batch(unique_texts[:mid], _depth=_depth + 1))
                merged.update(self.translate_batch(unique_texts[mid:], _depth=_depth + 1))
                if merged:
                    return merged
            logger.warning(
                "本地大模型译文无法对齐: 原文 %s 条 / 原文响应 %r",
                len(unique_texts),
                content[:120],
            )
            return {}

        out = {}
        rejected = 0
        for original, line in zip(unique_texts, lines):
            value = self._finish(original, line, guard, mappings)
            if value is None:
                rejected += 1
                continue
            out[original] = value
        if rejected:
            logger.warning(
                "质量闸门拦下 %s/%s 条（不放行时仍会返回，但会标记 low_quality）",
                rejected, len(unique_texts),
            )
        return out

    @staticmethod
    def _parse_translations(content: str, expected: int):
        """把模型响应解析成与原文等长的译文列表；解析不出来返回 None。

        主路径是 JSON 数组；模型没按 JSON 回答时退回逐行切分（老协议），
        两条路都拿不到对齐结果才算失败 —— 这时整批作废，避免错位写进库。
        """
        text = str(content or "").strip()
        if not text:
            return None

        # 剥掉模型可能加的 ```json 围栏
        if text.startswith("```"):
            text = text.split("```")[1] if text.count("```") >= 2 else text
            text = text.strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()

        # 主路径：JSON 数组
        parsed = OllamaTranslator._try_json(text)
        if isinstance(parsed, list) and len(parsed) == expected:
            if all(isinstance(item, str) for item in parsed):
                return [item.strip() for item in parsed]
            return None
        # 模型回了 JSON 但条数对不上：可能多包了一层 {"translations": [...]}
        if isinstance(parsed, dict):
            for value in parsed.values():
                if isinstance(value, list) and len(value) == expected:
                    if all(isinstance(item, str) for item in value):
                        return [item.strip() for item in value]
                    return None

        # 兜底：逐行切分（老协议）。单条时合并成一行，避免模型多回空行就作废。
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if expected == 1:
            merged = " ".join(lines)
            return [merged] if merged else None
        if len(lines) == expected:
            return lines
        return None

    @staticmethod
    def _try_json(text: str):
        """把响应当 JSON 解析；模型前面加了一句话（"好的，以下是…"）也能解出来。"""
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            pass
        # 掐出第一个 '[' 到最后一个 ']' 之间的内容再试一次
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except (TypeError, ValueError):
                return None
        return None

    def _finish(self, original: str, translated: str, guard, mappings):
        """还原占位符 + 过质量闸门。返回译文；不合格时按 strict_quality_gate 决定返回 None 还是原样。"""
        if not translated:
            return None
        mapping = (mappings or {}).get(original) or {}
        restored = guard.restore(translated, mapping) if (guard and mapping) else translated
        leftovers = guard.leftover_tokens(restored, mapping) if (guard and mapping) else []
        # 还原回去的艺人名本来就该带假名（口径要求保留），不能算作「没翻」
        reason = is_low_quality(
            original, restored, leftover_tokens=leftovers, allowed=mapping.values()
        )
        if reason:
            logger.info("译文未过质量闸门(%s): %s", reason, original[:60])
            return None if self.strict_quality_gate else restored
        return restored
