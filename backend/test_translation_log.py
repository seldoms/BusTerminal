"""翻译调用明细：发送什么 / 返回什么 / 写入什么都要能查（2026-11-15）。

不写主日志的原因见 translation_log 模块头：容器日志 50MB×5，高频翻译会把
历史轮转冲掉，而且完整请求体与原始响应本来也不该塞进主日志。
"""
import json
import threading
from unittest.mock import MagicMock

import pytest

import translation_log
import ollama_translator as translator_module
import scraper as scraper_module
from ollama_translator import OllamaTranslator
from scraper import JavBusScraper


@pytest.fixture(autouse=True)
def _clean_buffer():
    translation_log.clear()
    yield
    translation_log.clear()


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


# ---------- 缓冲本身 ----------

def test_recent_is_newest_first_and_capped():
    for i in range(5):
        translation_log.record("request", source="ollama", request=f"t{i}")

    assert [item["request"] for item in translation_log.recent(3)] == ["t4", "t3", "t2"]
    assert len(translation_log.recent(999)) == 5
    assert translation_log.recent(0)[0]["request"] == "t4"


def test_long_text_is_clipped_not_dropped():
    entry = translation_log.record("response", response="x" * 9000)
    assert len(entry["response"]) < translation_log.MAX_TEXT_LENGTH + 100
    assert "已截断，共 9000 字" in entry["response"]


# ---------- Ollama：完整请求体 + 模型原始返回 ----------

def test_ollama_request_and_raw_response_are_logged(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _Resp({
            "message": {"content": json.dumps(["中文标题"], ensure_ascii=False)},
            "done_reason": "stop",
            "eval_count": 42,
        })

    monkeypatch.setattr(translator_module.urllib.request, "urlopen", fake_urlopen)
    translator = OllamaTranslator("http://ollama.test", "test-model", guard=None)

    assert translator.translate_batch(["日文タイトル"]) == {"日文タイトル": "中文标题"}

    entries = translation_log.recent()
    assert [item["stage"] for item in entries] == ["response", "request"]
    request_entry = entries[1]
    assert "日文タイトル" in request_entry["request"]      # 发送了什么
    assert "test-model" in request_entry["request"]
    assert "/api/chat" in request_entry["note"]
    assert captured["body"]["think"] is False             # 日志没有干扰真实请求

    response_entry = entries[0]
    assert "中文标题" in response_entry["response"]        # 原始返回（不是解析后的结果）
    assert response_entry["duration_ms"] is not None
    assert "eval_count=42" in response_entry["note"]


def test_ollama_failure_is_logged(monkeypatch):
    def boom(request, timeout=None):
        raise TimeoutError("upstream down")

    monkeypatch.setattr(translator_module.urllib.request, "urlopen", boom)
    translator = OllamaTranslator("http://ollama.test", "test-model", guard=None)

    with pytest.raises(TimeoutError):
        translator.translate_batch(["日文タイトル"])

    entries = translation_log.recent()
    assert [item["stage"] for item in entries] == ["error", "request"]
    assert "TimeoutError" in entries[0]["note"]


# ---------- 写库：原文 → 译文，以及被拦下的条目 ----------

def _make_scraper(translator):
    s = JavBusScraper.__new__(JavBusScraper)
    s._translation_lock = threading.Lock()
    s.ollama_translator = translator
    s.translation_guard = None
    s.has_baidu = False
    s.translator = None
    s.proxies = None
    s._ollama_disabled_until = 0.0
    s._ollama_fail_log_at = 0.0
    s._google_endpoint_fail_log_at = 0.0
    s._google_disabled_until = 0.0
    s._google_cooldown_log_at = 0.0
    s.session = MagicMock()
    return s


def _fake_db(monkeypatch):
    fake = MagicMock()
    fake.get_translation.return_value = None
    fake.get_translations_batch.return_value = {}
    monkeypatch.setattr(scraper_module, "db", fake)
    return fake


def test_write_logs_exact_saved_pair(monkeypatch):
    class _Translator:
        batch_size = 10

        def translate_batch(self, texts):
            return {texts[0]: "中文标题"}

    fake_db = _fake_db(monkeypatch)
    s = _make_scraper(_Translator())

    assert s.batch_translate_to_chinese(["日文タイトル"]) == ["中文标题"]

    writes = [item for item in translation_log.recent() if item["stage"] == "write"]
    assert len(writes) == 1
    assert writes[0]["written"] == [{"original": "日文タイトル", "translated": "中文标题"}]
    assert "movies.title" in writes[0]["note"]
    fake_db.save_translation.assert_called_once_with("日文タイトル", "中文标题")
    fake_db.update_movie_titles_by_translation.assert_called_once_with("日文タイトル", "中文标题")


def test_skipped_translation_is_logged_without_write(monkeypatch):
    class _Translator:
        batch_size = 10

        def translate_batch(self, texts):
            return {texts[0]: texts[0]}   # 原文照抄 → 不该入库

    fake_db = _fake_db(monkeypatch)
    s = _make_scraper(_Translator())
    # 只验 ollama 的拦下记录：把兜底源关掉，避免测试真的去打外部翻译
    s._translate_via_google_endpoint = MagicMock(return_value={})
    s._google_disabled_until = float("inf")
    s.batch_translate_to_chinese(["日文タイトル"])

    entries = translation_log.recent()
    assert any(item["stage"] == "skip" and item["source"] == "ollama" for item in entries)
    assert not any(item["stage"] == "write" for item in entries)
    fake_db.save_translation.assert_not_called()
