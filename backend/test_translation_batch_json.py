"""翻译批量策略的锁定测试（2026-11-09 用户口径：组成 json，一次发 10 条）。

背景：原来队列 worker **一次只取 1 条**发请求，翻译器内部虽然有 batch_size，
但从来没被用上 —— 103 万条积压按 1 秒/条算要跑 12 天。
"""

import json

import pytest

import ollama_translator as translator_module
from ollama_translator import OllamaTranslator


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def _capture(texts, content, *, batch_size=10):
    """跑一次 translate_batch，返回 (结果, 发出去的请求体)。"""
    captured = {}

    class _Guard:
        ready = False

    tr = OllamaTranslator(
        "http://x", "m", batch_size=batch_size, guard=_Guard()
    )

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured.setdefault("bodies", []).append(captured["body"])
        return _Resp({"message": {"content": content}})

    original = translator_module.urllib.request.urlopen
    translator_module.urllib.request.urlopen = fake_urlopen
    try:
        result = tr.translate_batch(texts)
    finally:
        translator_module.urllib.request.urlopen = original
    return result, captured["body"]


def _capture_many(texts, responder):
    """喂不同的响应，返回 (结果, 所有请求体)。

    `responder` 可以是字符串，也可以是 ``fn(request_body, index) -> content``。
    """
    bodies = []

    class _Guard:
        ready = False

    tr = OllamaTranslator("http://x", "m", batch_size=10, guard=_Guard())

    def fake_urlopen(request, timeout=None):
        body = json.loads(request.data.decode("utf-8"))
        bodies.append(body)
        if callable(responder):
            content = responder(body, len(bodies) - 1)
        else:
            content = responder
        return _Resp({"message": {"content": content}})

    original = translator_module.urllib.request.urlopen
    translator_module.urllib.request.urlopen = fake_urlopen
    try:
        result = tr.translate_batch(texts)
    finally:
        translator_module.urllib.request.urlopen = original
    return result, bodies


TEXTS = [f"作品{i} フェラチオ 水野アコ" for i in range(10)]


def test_batch_of_ten_sent_as_single_json_array_request():
    """10 条标题必须**一次请求**发出去，且入参是 JSON 数组。"""
    payload = json.dumps(["译" + str(i) for i in range(10)], ensure_ascii=False)
    result, body = _capture(TEXTS, payload)

    # 一次请求里带了全部 10 条
    user_content = body["messages"][1]["content"]
    assert isinstance(json.loads(user_content), list)
    assert len(json.loads(user_content)) == 10
    # 结果 10 条全部对齐
    assert len(result) == 10
    assert result[TEXTS[0]] == "译0"
    assert result[TEXTS[9]] == "译9"


def test_default_batch_size_is_ten():
    """出厂默认批大小 = 10（用户口径：一次发 10 条）。"""
    import config

    assert config.OLLAMA_BATCH_SIZE == 10


def test_json_wrapped_in_code_fence_is_accepted():
    """模型爱加 ```json 围栏，要能剥掉。"""
    payload = "```json\n" + json.dumps(["译" + str(i) for i in range(10)], ensure_ascii=False) + "\n```"
    result, _ = _capture(TEXTS, payload)
    assert len(result) == 10
    assert result[TEXTS[3]] == "译3"


def test_json_object_wrapper_is_unwrapped():
    """模型回 {"translations": [...]} 这种包一层的形式也要能解。"""
    payload = json.dumps({"translations": ["译" + str(i) for i in range(10)]}, ensure_ascii=False)
    result, _ = _capture(TEXTS, payload)
    assert len(result) == 10


def test_plain_lines_still_accepted_as_fallback():
    """模型没按 JSON 回答时退回逐行切分（老协议），不能整批作废。"""
    payload = "\n".join("译" + str(i) for i in range(10))
    result, _ = _capture(TEXTS, payload)
    assert len(result) == 10
    assert result[TEXTS[5]] == "译5"


def test_mismatched_length_discards_whole_batch():
    """条数对不上就整批作废 —— 错位写进库比不写更糟。"""
    payload = json.dumps(["译" + str(i) for i in range(7)], ensure_ascii=False)
    result, _ = _capture(TEXTS, payload)
    assert result == {}


def test_single_title_json_still_works():
    """单条时 JSON 数组也要能用（退化场景）。"""
    result, body = _capture(["単独作品"], json.dumps(["单独译"], ensure_ascii=False))
    assert result == {"単独作品": "单独译"}
    assert json.loads(body["messages"][1]["content"]) == ["単独作品"]


def test_title_containing_newline_does_not_break_alignment():
    """标题里自带换行时，JSON 协议仍能逐条对齐（旧协议会串行）。"""
    texts = ["第一行\n第二行 作品", "普通作品"]
    payload = json.dumps(["译一", "译二"], ensure_ascii=False)
    result, _ = _capture(texts, payload)
    assert result == {"第一行\n第二行 作品": "译一", "普通作品": "译二"}


def test_worker_loop_consumes_batch_not_single():
    """队列 worker 一次取多条 —— 原来一次只取 1 条，批量能力完全没用上。"""
    from unittest.mock import MagicMock

    import scraper as scraper_module

    s = scraper_module.JavBusScraper.__new__(scraper_module.JavBusScraper)
    s._translation_queue_lock = __import__("threading").Lock()
    s._translation_queue_event = __import__("threading").Event()
    s._translation_queue = __import__("collections").deque(["t1", "t2", "t3"])
    s.ollama_translator = MagicMock()
    s.ollama_translator.batch_size = 3

    seen = []

    def fake_batch(texts, **_kw):
        seen.append(list(texts))
        return list(texts)

    s.batch_translate_to_chinese = fake_batch

    worker = scraper_module.JavBusScraper._translation_worker_loop.__get__(s)
    # 只跑一个循环周期：让 batch_translate_to_chinese 抛错来跳出
    def boom(texts, **_kw):
        seen.append(list(texts))
        raise KeyboardInterrupt

    s.batch_translate_to_chinese = boom
    with pytest.raises(KeyboardInterrupt):
        worker()
    assert seen and seen[0] == ["t1", "t2", "t3"], "应一次取走 3 条，而不是一条一条取"


# ---------- 模型数错条数时的兜底（2026-11-09 生产实测） ----------

def test_prompt_states_the_count():
    """prompt 里必须写明条数 —— 模型自己数 12 经常数错（实测少回一条）。"""
    _, body = _capture(TEXTS, json.dumps(["译%d" % i for i in range(10)]))
    system = body["messages"][0]["content"]
    assert "10" in system, system


def test_duplicate_input_is_sent_once():
    """完全相同的标题只发一次 —— 重复项会被模型当冗余，回的条数就对不上。"""
    texts = ["作品A", "作品B", "作品A"]
    payload = json.dumps(["译A", "译B"], ensure_ascii=False)
    result, body = _capture(texts, payload)

    sent = json.loads(body["messages"][1]["content"])
    assert len(sent) == 2, "重复标题应被去重后发出：%r" % (sent,)
    assert result["作品A"] == "译A"
    assert result["作品B"] == "译B"


def test_misaligned_batch_is_retried_in_halves():
    """模型少回一条时不能整批丢 —— 拆成两半各试一次。"""
    texts = ["t%d" % i for i in range(10)]

    def responder(body, index):
        sent = json.loads(body["messages"][1]["content"])
        if index == 0:
            # 第一次整批：模型漏了一条，只回 9 个
            return json.dumps(["译%d" % i for i in range(9)], ensure_ascii=False)
        # 拆开后的两半都按收到的条数老实回
        return json.dumps(["译" + t for t in sent], ensure_ascii=False)

    result, bodies = _capture_many(texts, responder)

    assert len(bodies) >= 2, "对齐失败应触发重试"
    assert len(result) == 10, "重试后应拿回全部 10 条：%r" % (result,)
    assert result["t0"] == "译t0"
    assert result["t9"] == "译t9"


def test_single_item_misalignment_is_dropped():
    """拆到单条还对不齐（模型什么都没给）→ 丢弃，不无限重试。"""
    result, bodies = _capture_many(["t1"], "")
    assert result == {}
    assert len(bodies) == 1, "单条不该再拆"


def test_preamble_before_json_array_is_accepted():
    """模型前面加一句「好的，以下是…」也能解出来。"""
    payload = "好的，以下是翻译结果：\n" + json.dumps(["译%d" % i for i in range(10)], ensure_ascii=False)
    result, _ = _capture(TEXTS, payload)
    assert len(result) == 10
    assert result[TEXTS[0]] == "译0"
