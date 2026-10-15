"""系统配置保存路径的清洗与校验。

回归背景：生产库里 jellyfin_api_key 实际存成了 `...b64;;;;;`（末尾 5 个分号），
导致 Jellyfin 所有请求 401，而界面只提示「保存成功」，完全看不出来。
"""
import pytest

from system_config_service import sanitize_pasted_value, sanitize_pasted_config_payload


# ------------------------------------------------------------------ 单值清洗


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 生产实测脏值形态：32 位十六进制 key 结尾误粘 5 个分号。
        # ⚠️ 这里必须是假值 —— 测试文件会随仓库公开发布，
        # 原先是生产 Jellyfin API Key 的真实值，已替换。
        ("0123456789abcdef0123456789abcdef;;;;;", "0123456789abcdef0123456789abcdef"),
        ("  abc123  ", "abc123"),
        ('"abc123"', "abc123"),
        ("'abc123'", "abc123"),
        ("`abc123`", "abc123"),
        ("abc123;", "abc123"),
        # 不能误伤：URL 结尾斜杠、中文分号都是内容的一部分
        ("http://192.168.1.100:8096/", "http://192.168.1.100:8096/"),
        ("正常中文值；", "正常中文值；"),
        # 边界
        ("", ""),
        ("a", "a"),
        (123, 123),
        (None, None),
    ],
)
def test_sanitize_pasted_value(raw, expected):
    assert sanitize_pasted_value(raw) == expected


def test_sanitize_keeps_inner_content_untouched():
    """只去包裹字符，中间内容一个字都不能动。"""
    assert sanitize_pasted_value("a;b;c") == "a;b;c"
    assert sanitize_pasted_value(";;;") == ""


# ------------------------------------------------------------------ 批量清洗


def test_sanitize_payload_only_touches_pasted_keys():
    cleaned = sanitize_pasted_config_payload({
        "jellyfin_api_key": "key123;;;",
        "jellyfin_host": " http://h:8096/ ",
        "disk_threshold_gb": 1190.0,
        "auto_follow_enabled": True,
        "automation_mode": "suggest",
    })
    assert cleaned["jellyfin_api_key"] == "key123"
    assert cleaned["jellyfin_host"] == "http://h:8096/"
    # 非粘贴型字段必须原样保留
    assert cleaned["disk_threshold_gb"] == 1190.0
    assert cleaned["auto_follow_enabled"] is True
    assert cleaned["automation_mode"] == "suggest"


def test_sanitize_payload_ignores_unknown_keys():
    """白名单外的键不参与处理，避免误伤。"""
    payload = {"some_future_key": ";;;keep;;;"}
    assert sanitize_pasted_config_payload(payload)["some_future_key"] == ";;;keep;;;"


def test_sanitize_payload_passes_through_non_dict():
    assert sanitize_pasted_config_payload(None) is None
    assert sanitize_pasted_config_payload("x") == "x"

def test_main_module_exposes_the_sanitizer_it_calls():
    """回归：main.py 里调用了 sanitize_pasted_config_payload，却只导入了
    sanitize_pasted_value —— 名字不匹配，**保存系统配置时会抛 NameError**。
    这类错误接口测试测不到（没有真的 POST /api/config/system），只能靠静态断言兜住。
    """
    import ast
    import inspect

    import main as main_module

    src = inspect.getsource(main_module)
    tree = ast.parse(src)

    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name.startswith("sanitize_"):
                    imported.add(alias.name)

    used_but_not_imported = {n for n in called if n.startswith("sanitize_")} - imported
    assert not used_but_not_imported, (
        f"main.py 调用了 {used_but_not_imported} 但没有导入 —— 调用时会 NameError"
    )
