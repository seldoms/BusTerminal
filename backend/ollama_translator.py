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

# 逐行翻译：行数与顺序必须对齐，调用方按 '\n' 切分对齐原文
SYSTEM_PROMPT = (
    "你是影视标题翻译助手。把用户给出的日文/英文标题逐行翻译成简体中文。"
    "输出必须与输入行数完全一致：每行只放对应行的译文，保留番号、日期、数字等非文字内容，"
    "不要编号、不要解释、不要空行、不要输出原文。"
)


class OllamaTranslator:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: int = 300,
        batch_size: int = 12,
        num_predict: int = 1024,
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

    def translate_batch(self, texts):
        """一次请求翻译一小批，返回 {原文: 译文}。

        带占位保护时：先换掉标题里的演员假名串 → 翻译 → 原样还原并过质量闸门。
        `strict_quality_gate=True` 时不合格条目直接从结果里剔除（调用方视作未翻译，
        下一轮再排队，不会用坏译文污染 translations 表）。
        """
        if not texts:
            return {}

        guard = self.guard if (self.guard and self.guard.ready) else None
        mappings = {}
        payloads = []
        for text in texts:
            if guard is not None:
                protected, mapping = guard.protect(text)
                payloads.append(protected)
                mappings[text] = mapping
            else:
                payloads.append(text)
                mappings[text] = {}

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(payloads)},
            ],
            "stream": False,
            "think": False,
            "options": {
                "temperature": self.temperature,
                # 按条数收紧输出上限：译文长度约等于原文，给太多只会让模型啰嗦、白等
                "num_predict": min(self.num_predict, 128 + 64 * len(texts)),
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
        lines = [line.strip() for line in content.split("\n") if line.strip()]
        if len(texts) == 1:
            # 一个片子一个请求：模型偶尔会多回一个空行/断行，这里合并即可，不必整条作废
            merged = " ".join(lines)
            result = self._finish(texts[0], merged, guard, mappings) if merged else {}
            return {texts[0]: result} if result else {}
        if len(lines) != len(texts):
            logger.warning(
                "本地大模型译文行数不匹配: 原文 %s 行 / 译文 %s 行",
                len(texts),
                len(lines),
            )
            return {}

        out = {}
        rejected = 0
        for original, line in zip(texts, lines):
            value = self._finish(original, line, guard, mappings)
            if value is None:
                rejected += 1
                continue
            out[original] = value
        if rejected:
            logger.warning(
                "质量闸门拦下 %s/%s 条（不放行时仍会返回，但会标记 low_quality）",
                rejected, len(texts),
            )
        return out

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
