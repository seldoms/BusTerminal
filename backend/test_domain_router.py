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
