import logging

from domain_router import DomainRouter


class FakeDb:
    def __init__(self):
        self.values = {}

    def get_config(self, key):
        return self.values.get(key)

    def set_config(self, key, value):
        self.values[key] = value


def test_domain_router_persists_new_domains_and_opens_circuit():
    db = FakeDb()
    router = DomainRouter(
        db=db,
        source_key="javbus",
        default_domains=["https://primary.example", "https://backup.example"],
        logger=logging.getLogger(__name__),
        failure_threshold=2,
        cooldown_seconds=60,
    )

    assert router.add_domains(["https://new.example/path"], "official_page") == 0
    assert router.add_domains(["https://new.example"], "official_page") == 1
    router.record_failure("https://primary.example", "Timeout")
    router.record_failure("https://primary.example", "Timeout")

    snapshot = db.values["source_domains_v1:javbus"]
    primary = next(item for item in snapshot["domains"] if item["domain"] == "https://primary.example")
    assert primary["consecutive_failures"] == 2
    assert primary["circuit_open_until"] > 0


def test_domain_router_restores_persisted_health_state():
    db = FakeDb()
    db.values["source_domains_v1:javbus"] = {
        "version": 1,
        "domains": [
            {
                "domain": "https://backup.example",
                "priority": 1,
                "score": 90,
                "latency_ms": 20,
                "consecutive_failures": 0,
                "circuit_open_until": 0,
            }
        ],
    }
    router = DomainRouter(
        db=db,
        source_key="javbus",
        default_domains=["https://primary.example", "https://backup.example"],
        logger=logging.getLogger(__name__),
    )

    assert router.candidates()[0] == "https://backup.example"


def test_domain_router_revives_healthy_domain_disabled_by_failure_count():
    """累计失败判死曾误杀健康域名，加载时要给复活机会。

    2026-11-07 实测：busdmm.casa / seedmm.casa / dmmsee.casa 详情页全部
    OK_GID，只是历史上一段 SSL/超时波动累积到 20 次被永久排除，
    于是候选池只剩 3 个（其中还有 1 个是停放页）。
    """
    db = FakeDb()
    db.values["source_domains_v1:javbus"] = {
        "version": 1,
        "domains": [
            {
                "domain": "https://flaky.example",
                "priority": 0,
                "score": 0,
                "consecutive_failures": 25,
                "circuit_open_until": 0,
                "enabled": False,
                "last_failure_reason": "dead_domain:SSLError",
            }
        ],
    }
    router = DomainRouter(
        db=db,
        source_key="javbus",
        default_domains=["https://flaky.example"],
        logger=logging.getLogger(__name__),
        dead_threshold=20,
    )

    assert "https://flaky.example" in router.candidates()
    state = router.snapshot()["domains"][0]
    assert state["consecutive_failures"] == 0
    assert state["score"] >= 20.0


def test_domain_router_keeps_dead_page_domain_excluded_after_reload():
    """停放页是内容级判死，必须永久排除，重启后也不能复活。"""
    db = FakeDb()
    router = DomainRouter(
        db=db,
        source_key="javbus",
        default_domains=["https://parked.example", "https://alive.example"],
        logger=logging.getLogger(__name__),
    )
    router.record_dead_domain("https://parked.example", "parked_page")

    # 重新加载（模拟重启）：停放页仍需被排除
    reloaded = DomainRouter(
        db=db,
        source_key="javbus",
        default_domains=["https://parked.example", "https://alive.example"],
        logger=logging.getLogger(__name__),
    )
    candidates = reloaded.candidates()
    assert "https://parked.example" not in candidates
    assert "https://alive.example" in candidates


def test_domain_router_success_revives_temporarily_disabled_domain():
    """暂时下线的域名抓到内容后应恢复可用。"""
    db = FakeDb()
    router = DomainRouter(
        db=db,
        source_key="javbus",
        default_domains=["https://flaky.example"],
        logger=logging.getLogger(__name__),
        failure_threshold=1,
        dead_threshold=2,
    )
    router.record_failure("https://flaky.example", "Timeout")
    router.record_failure("https://flaky.example", "Timeout")
    assert router.snapshot()["domains"][0]["enabled"] is False

    router.record_success("https://flaky.example", 120.0)
    state = router.snapshot()["domains"][0]
    assert state["enabled"] is True
    assert state["consecutive_failures"] == 0


def test_domain_router_success_does_not_revive_dead_page_domain():
    """停放页即使返回 200 也不能复活（它返回的是停放页，不是真内容）。"""
    db = FakeDb()
    router = DomainRouter(
        db=db,
        source_key="javbus",
        default_domains=["https://parked.example"],
        logger=logging.getLogger(__name__),
    )
    router.record_dead_domain("https://parked.example", "parked_page")
    router.record_success("https://parked.example", 50.0)

    state = router.snapshot()["domains"][0]
    assert state["enabled"] is False
    assert state["dead_page"] is True


def test_domain_router_infers_dead_page_from_persisted_reason():
    """存量状态没有 dead_page 字段，要能从 last_failure_reason 反推。

    v2.01 的 record_dead_domain 写的 reason 形如 dead_domain:parked_page，
    v2.02 才引入 dead_page 字段。升级后必须认得存量标记，
    否则停放页会在重启时被当成「累计失败误杀」拉回候选池。
    """
    db = FakeDb()
    db.values["source_domains_v1:javbus"] = {
        "version": 1,
        "domains": [
            {
                "domain": "https://www.busdmm.cyou",
                "priority": 0,
                "score": 70,
                "consecutive_failures": 2,
                "circuit_open_until": 0,
                "enabled": True,
                "last_failure_reason": "dead_domain:parked_page",
            },
            {
                "domain": "https://www.busdmm.casa",
                "priority": 1,
                "score": 0,
                "consecutive_failures": 25,
                "circuit_open_until": 0,
                "enabled": False,
                "last_failure_reason": "dead_domain:SSLError",
            },
        ],
    }
    router = DomainRouter(
        db=db,
        source_key="javbus",
        default_domains=["https://www.busdmm.casa"],
        logger=logging.getLogger(__name__),
        dead_threshold=20,
    )

    candidates = router.candidates()
    # 停放页：即使它在内置列表外、且存量 enabled=true，也必须被排除
    assert "https://www.busdmm.cyou" not in candidates
    # 内置且健康的域名：必须回到候选池（否则只有全熔断时才轮得到它）
    assert "https://www.busdmm.casa" in candidates


def test_domain_router_builtin_domain_returns_to_candidate_pool_on_reload():
    """内置实测可用域名，重启后要真的回到候选池而不是 enabled=false。"""
    db = FakeDb()
    db.values["source_domains_v1:javbus"] = {
        "version": 1,
        "domains": [
            {
                "domain": "https://builtin.example",
                "priority": 0,
                "score": 0,
                "consecutive_failures": 25,
                "circuit_open_until": 0,
                "enabled": False,
                "last_failure_reason": "dead_domain:SSLError",
            },
            {
                "domain": "https://other.example",
                "priority": 1,
                "score": 90,
                "consecutive_failures": 0,
                "circuit_open_until": 0,
                "enabled": True,
            },
        ],
    }
    router = DomainRouter(
        db=db,
        source_key="javbus",
        default_domains=["https://builtin.example"],
        logger=logging.getLogger(__name__),
        dead_threshold=20,
    )

    states = {s["domain"]: s for s in router.snapshot()["domains"]}
    assert states["https://builtin.example"]["enabled"] is True
    assert states["https://builtin.example"]["consecutive_failures"] == 0
    assert "https://builtin.example" in router.candidates()


def test_domain_router_failure_does_not_overwrite_dead_page_reason():
    """已判死的域名再记普通失败时，不能覆盖失败原因。

    last_failure_reason 里的 parked_page 是存量状态反推 dead_page 的
    唯一依据（dead_page 字段 v2.02 才加），被覆盖后停放页就认不出来了。
    """
    db = FakeDb()
    router = DomainRouter(
        db=db,
        source_key="javbus",
        default_domains=["https://parked.example"],
        logger=logging.getLogger(__name__),
    )
    router.record_dead_domain("https://parked.example", "parked_page")
    router.record_failure("https://parked.example", "RuntimeError")

    state = router.snapshot()["domains"][0]
    assert state["dead_page"] is True
    assert state["enabled"] is False
    assert "parked_page" in state["last_failure_reason"]
    # 已判死的域名不再累计普通失败计数
    assert state["consecutive_failures"] == 20


def test_domain_router_dead_page_domain_stays_excluded_from_candidates():
    """判死后即使又被拉回状态，也要稳定排除在候选池之外。"""
    db = FakeDb()
    router = DomainRouter(
        db=db,
        source_key="javbus",
        default_domains=["https://parked.example", "https://alive.example"],
        logger=logging.getLogger(__name__),
    )
    router.record_dead_domain("https://parked.example", "parked_page")

    candidates = router.candidates()
    assert "https://parked.example" not in candidates
    assert "https://alive.example" in candidates
