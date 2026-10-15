"""翻译任务多路分发：把积压标题摊给多个后端，谁快谁先出。

背景（2026-10-11）：本地 Hunyuan-MT 7B 单条约 0.55 秒，103 万条要 6.6 天。
同时手上有一个 OpenAI 兼容网关（实测 152 个模型），如果挑出几个又快又稳的，
并行分流能把这个周期压缩下来。

**设计要点 —— 为什么这样分工**：
    · 网关模型是第三方代理，实测大量 TimeoutError / 429，**质量与可用性都不可控**；
    · 但只要它们产出的译文**过不了质量闸门就不会入库**（见 translation_guard），
      派单给它们的风险是可控的——最坏情况是白跑一次，闸门拒收。
    · 所以策略是「本地为主 + 网关补位」：本地先吃，按剩余量再往网关分，
      网关失败/超时直接跳过并落回本地，不让它拖慢整体。

所有后端共用同一套占位保护与质量闸门，保证无论谁翻译，产出的口径一致。
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, Iterable, List, Optional, Sequence

from ollama_translator import OllamaTranslator
from translation_guard import TitleGuard, is_low_quality

logger = logging.getLogger(__name__)


class OpenAICompatTranslator:
    """OpenAI 兼容 /v1/chat/completions 的翻译后端。

    刻意做成「和 OllamaTranslator 同接口」，这样分发器可以一视同仁地调度。
    逐条发送：这些聚合网关对多行批量同样不友好（实测会当成一整条理解）。
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        token: str,
        *,
        timeout: int = 60,
        temperature: float = 0.2,
        guard: Optional[TitleGuard] = None,
        strict_quality_gate: bool = True,
    ):
        self.base_url = str(base_url or "").rstrip("/")
        self.model = model
        self.token = str(token or "").strip()
        self.timeout = timeout
        self.temperature = temperature
        self.guard = guard
        self.strict_quality_gate = strict_quality_gate

    @property
    def name(self) -> str:
        return f"{self.model}"

    def translate_batch(self, texts: Sequence[str]) -> Dict[str, str]:
        if not texts:
            return {}
        text = str(texts[0])
        guard = self.guard if (self.guard and self.guard.ready) else None
        payload_text, mapping = guard.protect(text) if guard else (text, {})

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content":
                    "Translate the given Japanese title into Simplified Chinese. "
                    "Output only the translation. Keep every ZZ-number-ZZ placeholder "
                    "exactly as-is. Do not number, explain, or echo the input."},
                {"role": "user", "content": payload_text},
            ],
            "temperature": self.temperature,
            "max_tokens": 256,
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(type(exc).__name__) from exc

        choice = (body.get("choices") or [{}])[0]
        translated = ((choice.get("message") or {}).get("content") or "").strip()
        if not translated:
            return {}
        restored = guard.restore(translated, mapping) if (guard and mapping) else translated
        leftovers = guard.leftover_tokens(restored, mapping) if (guard and mapping) else []
        reason = is_low_quality(text, restored, leftover_tokens=leftovers,
                               allowed=mapping.values())
        if reason:
            logger.info("[%s] 译文未过闸门(%s): %s", self.model, reason, text[:50])
            return {}
        return {text: restored}


class TranslationDispatcher:
    """把一批标题分发到多个后端，按实测速度加权、失败自动回退。

    `speed_score` 由 `scripts/probe_models.py` 的测速结果给出（越小越快）；
    没有测速数据时按出现顺序均权。
    """

    def __init__(
        self,
        backends: Sequence[object],
        *,
        guard: Optional[TitleGuard] = None,
        prefer: str = "local",
    ):
        self.backends = [b for b in backends if b is not None]
        self.guard = guard
        self.prefer = prefer
        self._lock = threading.Lock()
        self._counters: Dict[str, Dict[str, int]] = {}

    @property
    def backend_names(self) -> List[str]:
        return [getattr(b, "name", type(b).__name__) for b in self.backends]

    def stats(self) -> Dict[str, Dict[str, int]]:
        with self._lock:
            return {k: dict(v) for k, v in self._counters.items()}

    def _bump(self, backend: str, field: str) -> None:
        with self._lock:
            entry = self._counters.setdefault(backend, {"ok": 0, "fail": 0, "reject": 0})
            entry[field] = entry.get(field, 0) + 1

    def _order(self) -> List[object]:
        """本地后端优先，其余随机打散以免单一后端被压满。"""
        head = [b for b in self.backends if getattr(b, "name", "") == self.prefer
                or "ollama" in type(b).__name__.lower()]
        tail = [b for b in self.backends if b not in head]
        random.shuffle(tail)
        return head + tail

    def run_once(self, texts: Iterable[str]) -> Dict[str, str]:
        """处理一批标题，返回 {原文: 译文}。单个后端失败会继续试下一个。"""
        pending = [t for t in texts if t]
        done: Dict[str, str] = {}
        order = self._order()

        for text in pending:
            for backend in order:
                name = getattr(backend, "name", type(backend).__name__)
                try:
                    result = backend.translate_batch([text]) or {}
                except Exception as exc:  # noqa: BLE001
                    self._bump(name, "fail")
                    logger.debug("[%s] 失败: %s", name, exc)
                    continue
                if not result:
                    # 可能是后端挂了，也可能是被质量闸门拒收；两者都不该重试到底
                    self._bump(name, "reject")
                    continue
                done[text] = result[text]
                self._bump(name, "ok")
                break
            time.sleep(0.05)
        return done

    def report(self) -> Dict[str, object]:
        stats = self.stats()
        return {
            "backends": self.backend_names,
            "stats": stats,
            "total_ok": sum(s.get("ok", 0) for s in stats.values()),
            "total_reject": sum(s.get("reject", 0) for s in stats.values()),
            "total_fail": sum(s.get("fail", 0) for s in stats.values()),
        }


def build_dispatcher(
    *,
    ollama_url: str = "",
    ollama_model: str = "",
    gateway_url: str = "",
    gateway_token: str = "",
    gateway_models: Optional[List[str]] = None,
    guard: Optional[TitleGuard] = None,
) -> TranslationDispatcher:
    """按配置拼一个分发器；没有可用后端时返回空分发器（调用方需自行判断）。"""
    backends: List[object] = []
    if ollama_url and ollama_model:
        backends.append(OllamaTranslator(
            ollama_url, ollama_model, timeout=300, batch_size=1,
            guard=guard, strict_quality_gate=True,
        ))
    for model in gateway_models or []:
        if not gateway_url:
            break
        backends.append(OpenAICompatTranslator(
            gateway_url, model, gateway_token, timeout=60, guard=guard,
        ))
    return TranslationDispatcher(backends, guard=guard)