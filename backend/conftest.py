import asyncio
import inspect
import os
import sys
from pathlib import Path

import pytest

# ⚠️ 不要删这段。测试会话固定跑在「不需要登录」模式下。
#
# auth_service.auth_required() 读的是 os.getenv("REQUIRE_AUTH", "true")，默认值
# 是「需要登录」。以前这个开关是从开发者本机的 backend/.env 里漏进来的（那份文件
# 未纳入版本库，里面写着 REQUIRE_AUTH=false），于是**全新 clone 的仓库跑 pytest
# 会有十几条接口用例直接 401** —— 在本机完全看不出来，只有干净检出才暴露。
# 这里显式钉住，去掉对本机未跟踪文件的隐式依赖。
# test_auth.py 需要鉴权时会自己临时改成 "true"，结束时还原回这里的值。
os.environ["REQUIRE_AUTH"] = "false"

import test_lifecycle_scenario as lifecycle_scenario

# 会话级临时目录。必须在 pytest_configure 阶段（收集测试用例之前）创建：
# 部分测试模块（如 test_resilience_verification.py）在模块级 import main，
# 而 main 会实例化 database.db 单例。若等到 fixture 阶段才设置 DATA_DIR /
# DATABASE_URL，单例早已指向真实数据库，DB 隔离断言会失败。
_SESSION_TEMP_DIR = None


def pytest_configure(config):
    global _SESSION_TEMP_DIR
    if _SESSION_TEMP_DIR is None:
        _SESSION_TEMP_DIR = lifecycle_scenario._prepare_temp_environment()


def pytest_unconfigure(config):
    global _SESSION_TEMP_DIR
    if _SESSION_TEMP_DIR is not None:
        _SESSION_TEMP_DIR.cleanup()
        _SESSION_TEMP_DIR = None


@pytest.fixture(scope="session")
def temp_dir():
    yield _SESSION_TEMP_DIR


@pytest.fixture(autouse=True)
def reset_runtime_circuit_breakers(main_module):
    """复位会话级熔断器，避免用例之间互相污染。

    Jellyfin 服务的熔断器是进程内状态，跳闸后有 60s 冷却；前一个用例一旦
    把它打下来，后续用例就会拿到 503，测到的就不是用例自己的行为了。
    """
    main_module.get_jellyfin_service().record_success()
    yield


@pytest.fixture(scope="session")
def main_module(temp_dir):
    backend_dir = Path(__file__).resolve().parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    import database

    expected_db_path = (Path(temp_dir.name) / "movies.db").resolve()
    actual_db_path = Path(database.db.db_path).resolve()
    assert actual_db_path == expected_db_path, (
        f"test database isolation failed: expected {expected_db_path}, got {actual_db_path}"
    )

    database.db.set_config("enable_domain_rotation", "false")
    database.db.set_config("fixed_domain", "https://www.javbus.com")

    import main as app_main

    lifecycle_scenario._patch_external_services(app_main)
    return app_main


def pytest_pyfunc_call(pyfuncitem):
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None

    fixture_names = pyfuncitem._fixtureinfo.argnames
    kwargs = {name: pyfuncitem.funcargs[name] for name in fixture_names}
    asyncio.run(pyfuncitem.obj(**kwargs))
    return True
