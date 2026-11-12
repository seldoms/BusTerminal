"""翻译失败降级与日志节流（2026-11-09）。

背景：ollama 被停机期间，「本地大模型翻译失败」这条 WARNING 每条标题打一次，
一小时刷出 5000+ 行，把 json-file 的 10MB×3 上限写满轮转掉，
44 小时的日志被整个丢弃 —— 故障现场根本查不到（连「谁被停的」都只能靠猜）。

改动：
- ollama 失败即进入 10 分钟冷却，冷却期内直接走其它翻译源，不再每条撞超时；
- 失败类 WARNING 按 60 秒最小间隔节流。
"""
import threading
import time
from unittest.mock import MagicMock, patch

import scraper as scraper_module
from scraper import JavBusScraper


def _make_scraper(ollama=None):
    s = JavBusScraper.__new__(JavBusScraper)
    s._translation_lock = threading.Lock()
    s.ollama_translator = ollama
    s.has_baidu = False
    s.translator = None
    s.proxies = None
    s._ollama_disabled_until = 0.0
    s._ollama_fail_log_at = 0.0
    s._google_endpoint_fail_log_at = 0.0
    s._google_disabled_until = 0.0
    s._google_cooldown_log_at = 0.0
    return s


def _fake_db():
    db = MagicMock()
    db.get_translation.return_value = None
    db.get_translations_batch.return_value = {}
    return db


class _BoomTranslator:
    """总是抛异常的假 ollama 翻译器。"""

    batch_size = 1

    def __init__(self):
        self.calls = 0

    def translate_batch(self, texts):
        self.calls += 1
        raise RuntimeError("connection refused")


def test_ollama_failure_enters_cooldown():
    """ollama 抛异常后要进入冷却，而不是下一条标题继续撞。"""
    ollama = _BoomTranslator()
    s = _make_scraper(ollama)
    db = _fake_db()

    with patch.object(scraper_module, "db", db), \
         patch.object(s, "_translate_via_google_endpoint", return_value={}), \
         patch.object(scraper_module, "GoogleTranslator", MagicMock()):
        s.batch_translate_to_chinese(["酔った勢いで友達と"])

    assert s._ollama_disabled_until > time.time()
    assert s._ollama_disabled_until - time.time() <= scraper_module.OLLAMA_FAILURE_COOLDOWN_SECONDS + 5


def test_cooldown_skips_ollama_entirely():
    """冷却期内不再调用 ollama（省掉每条标题的超时等待）。"""
    ollama = _BoomTranslator()
    s = _make_scraper(ollama)
    s._ollama_disabled_until = time.time() + 300
    db = _fake_db()

    with patch.object(scraper_module, "db", db), \
         patch.object(s, "_translate_via_google_endpoint", return_value={}), \
         patch.object(scraper_module, "GoogleTranslator", MagicMock()):
        s.batch_translate_to_chinese(["酔った勢いで友達と"])

    assert ollama.calls == 0


def test_cooldown_expires_and_ollama_is_retried():
    """冷却过后要重新尝试 ollama（不能一次失败就永久放弃）。"""
    ollama = _BoomTranslator()
    s = _make_scraper(ollama)
    s._ollama_disabled_until = time.time() - 1
    db = _fake_db()

    with patch.object(scraper_module, "db", db), \
         patch.object(s, "_translate_via_google_endpoint", return_value={}), \
         patch.object(scraper_module, "GoogleTranslator", MagicMock()):
        s.batch_translate_to_chinese(["酔った勢いで友達と"])

    assert ollama.calls == 1


def test_repeated_ollama_failures_log_only_once_per_interval():
    """连续失败不该每条都打一条 WARNING（日志洪水的来源）。"""
    ollama = _BoomTranslator()
    s = _make_scraper(ollama)
    db = _fake_db()

    with patch.object(scraper_module, "db", db), \
         patch.object(s, "_translate_via_google_endpoint", return_value={}), \
         patch.object(scraper_module, "GoogleTranslator", MagicMock()), \
         patch.object(scraper_module.logger, "warning") as warn:
        # 冷却每次都会重置，所以这里手动把冷却清掉，模拟「持续故障」场景
        for _ in range(5):
            s._ollama_disabled_until = 0.0
            s.batch_translate_to_chinese(["酔った勢いで友達と"])

    ollama_warnings = [
        call for call in warn.call_args_list
        if "本地大模型翻译失败" in str(call)
    ]
    assert len(ollama_warnings) == 1, f"应只打一条，实际 {len(ollama_warnings)} 条"


def test_google_endpoint_failure_log_is_throttled():
    """Google 网页端点失败同样要节流。"""
    s = _make_scraper(ollama=None)
    db = _fake_db()

    with patch.object(scraper_module, "db", db), \
         patch.object(s, "_translate_via_google_endpoint", side_effect=RuntimeError("http error")), \
         patch.object(scraper_module, "GoogleTranslator", MagicMock()), \
         patch.object(scraper_module.logger, "warning") as warn:
        for _ in range(4):
            s.batch_translate_to_chinese(["酔った勢いで友達と"])

    google_warnings = [
        call for call in warn.call_args_list
        if "Google 网页端点批量翻译失败" in str(call)
    ]
    assert len(google_warnings) == 1, f"应只打一条，实际 {len(google_warnings)} 条"
