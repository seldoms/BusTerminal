import json
import os
import time

import health_deps
import config


def _write_heartbeat(hb_dir, service, **overrides):
    os.makedirs(hb_dir, exist_ok=True)
    payload = {
        "service": service,
        "ts": time.time(),
        "iso": "2026-10-11 04:41:06",
        "ok": True,
        "elapsed_s": 51.3,
        "detail": {},
        "artifacts": [],
    }
    payload.update(overrides)
    with open(os.path.join(hb_dir, f"{service}.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return payload


def _all_fresh(hb_dir, now):
    """三个服务都刚跑完、产物也都是刚产出的。"""
    _write_heartbeat(hb_dir, "bus_backup", ts=now, artifacts=[
        {"kind": "pg_dump", "name": "bt-20261011.dump", "bytes": 255852544, "ts": now},
    ])
    _write_heartbeat(hb_dir, "bus_backup_files", ts=now, artifacts=[
        {"kind": "amane_sqlite", "name": "amane-sqlite-20261011.tar.gz", "bytes": 4530390, "ts": now},
        {"kind": "jellyfin_core", "name": "jf-core-20261011.tar.gz", "bytes": 75082985, "ts": now},
        {"kind": "jellyfin_full", "name": "jf-full-20261011.tar.gz", "bytes": 5652197376, "ts": now},
    ])
    _write_heartbeat(hb_dir, "pt_ignore_guard", ts=now,
                     detail={"ignored": 88, "unignored": 2, "errors": 0})


def test_all_fresh_heartbeats_are_ok(tmp_path):
    now = time.time()
    hb_dir = str(tmp_path / "heartbeat")
    _all_fresh(hb_dir, now)

    report = health_deps.evaluate(now=now, directory=hb_dir)

    assert report["status"] == "ok"
    assert report["alerts"] == []
    assert {item["service"] for item in report["services"]} == {
        "bus_backup", "bus_backup_files", "pt_ignore_guard"}
    assert all(item["status"] == "ok" for item in report["services"])


def test_guard_stale_heartbeat_is_a_critical_alert(tmp_path):
    now = time.time()
    hb_dir = str(tmp_path / "heartbeat")
    _all_fresh(hb_dir, now)
    # 守卫 60s 一轮，1 小时没心跳 = 进程没了
    _write_heartbeat(hb_dir, "pt_ignore_guard", ts=now - 3600)

    report = health_deps.evaluate(now=now, directory=hb_dir)

    guard = next(item for item in report["services"] if item["service"] == "pt_ignore_guard")
    assert guard["status"] == "stale"
    assert "heartbeat_stale" in guard["issues"]
    alert = next(a for a in report["alerts"] if a["service"] == "pt_ignore_guard")
    assert alert["key"] == "pt_ignore_guard/heartbeat_stale"
    assert alert["severity"] == "critical"


def test_fresh_heartbeat_with_ok_false_is_failing_not_stale(tmp_path):
    """区分「活着但这一轮失败了」和「整个进程死了」。"""
    now = time.time()
    hb_dir = str(tmp_path / "heartbeat")
    _all_fresh(hb_dir, now)
    _write_heartbeat(hb_dir, "bus_backup_files", ts=now, ok=False,
                     detail={"failed": ["jellyfin_core"]})

    report = health_deps.evaluate(now=now, directory=hb_dir)

    service = next(item for item in report["services"] if item["service"] == "bus_backup_files")
    assert service["status"] == "failing"
    assert service["issues"] == ["heartbeat_failing"]
    assert service["heartbeat"]["age_seconds"] < 1
    assert report["alerts"][0]["severity"] == "warning"


def test_backup_artifact_stale_while_heartbeat_is_fresh(tmp_path):
    """进程活着但产物过期 —— 正是「静默失败」要抓的情况。"""
    now = time.time()
    hb_dir = str(tmp_path / "heartbeat")
    _all_fresh(hb_dir, now)
    _write_heartbeat(hb_dir, "bus_backup", ts=now, artifacts=[
        {"kind": "pg_dump", "name": "bt-20261009.dump", "bytes": 255852544,
         "ts": now - 48 * 3600},
    ])

    report = health_deps.evaluate(now=now, directory=hb_dir)

    service = next(item for item in report["services"] if item["service"] == "bus_backup")
    assert "artifact_stale:pg_dump" in service["issues"]
    assert service["artifacts"][0]["status"] == "stale"
    keys = {alert["key"] for alert in report["alerts"]}
    assert "bus_backup/artifact_stale:pg_dump" in keys


def test_weekly_artifact_tolerates_more_than_a_day(tmp_path):
    now = time.time()
    hb_dir = str(tmp_path / "heartbeat")
    _all_fresh(hb_dir, now)
    two_days_ago = now - 2 * 24 * 3600
    _write_heartbeat(hb_dir, "bus_backup_files", ts=now, artifacts=[
        {"kind": "amane_sqlite", "name": "amane-sqlite-20261009.tar.gz", "bytes": 1, "ts": now},
        {"kind": "jellyfin_core", "name": "jf-core-20261009.tar.gz", "bytes": 1, "ts": now},
        {"kind": "jellyfin_full", "name": "jf-full-20261009.tar.gz", "bytes": 1,
         "ts": two_days_ago},
    ])

    report = health_deps.evaluate(now=now, directory=hb_dir)

    assert report["status"] == "ok"


def test_missing_heartbeat_directory_is_unknown_without_alerts(tmp_path):
    report = health_deps.evaluate(now=time.time(), directory=str(tmp_path / "nope"))

    assert report["status"] == "unknown"
    assert report["issues"] == ["heartbeat_dir_missing"]
    assert report["alerts"] == []


def test_unreadable_heartbeat_is_treated_as_stale(tmp_path):
    hb_dir = tmp_path / "heartbeat"
    hb_dir.mkdir()
    (hb_dir / "bus_backup.json").write_text("{ not json", encoding="utf-8")

    report = health_deps.evaluate(now=time.time(), directory=str(hb_dir))

    service = next(item for item in report["services"] if item["service"] == "bus_backup")
    assert service["issues"] == ["heartbeat_unreadable"]
    assert service["status"] == "stale"


def _alert(key, service="bus_backup"):
    return {"key": key, "service": service, "issue": "heartbeat_stale",
            "severity": "critical", "title": "t", "message": "m"}


def test_alert_dispatch_is_deduped_by_cooldown(tmp_path):
    state_path = str(tmp_path / "heartbeat" / "alerts.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    sent_calls = []

    first = health_deps.dispatch_health_alerts(
        [_alert("bus_backup/heartbeat_stale")],
        cooldown_seconds=3600,
        state_path=state_path,
        now=1000.0,
        sender=lambda title, message: sent_calls.append(title) or True,
    )
    assert len(first) == 1
    assert len(sent_calls) == 1

    # 冷却期内重复命中：不再打扰
    second = health_deps.dispatch_health_alerts(
        [_alert("bus_backup/heartbeat_stale")],
        cooldown_seconds=3600,
        state_path=state_path,
        now=1200.0,
        sender=lambda title, message: sent_calls.append(title) or True,
    )
    assert second == []
    assert len(sent_calls) == 1
    state = json.load(open(state_path, encoding="utf-8"))
    assert state["bus_backup/heartbeat_stale"]["suppressed"] == 1

    # 过了冷却期才再发一条
    third = health_deps.dispatch_health_alerts(
        [_alert("bus_backup/heartbeat_stale")],
        cooldown_seconds=3600,
        state_path=state_path,
        now=5000.0,
        sender=lambda title, message: sent_calls.append(title) or True,
    )
    assert len(third) == 1
    assert len(sent_calls) == 2


def test_alert_state_is_cleared_when_recovered(tmp_path):
    state_path = str(tmp_path / "heartbeat" / "alerts.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)

    health_deps.dispatch_health_alerts(
        [_alert("bus_backup/heartbeat_stale")],
        cooldown_seconds=3600, state_path=state_path, now=1000.0,
        sender=lambda title, message: True,
    )
    health_deps.dispatch_health_alerts(
        [], cooldown_seconds=3600, state_path=state_path, now=1100.0,
        sender=lambda title, message: True,
    )

    assert json.load(open(state_path, encoding="utf-8")) == {}


def test_undelivered_alert_does_not_count_as_sent(tmp_path):
    state_path = str(tmp_path / "heartbeat" / "alerts.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)

    sent = health_deps.dispatch_health_alerts(
        [_alert("bus_backup/heartbeat_stale")],
        cooldown_seconds=3600, state_path=state_path, now=1000.0,
        sender=lambda title, message: False,  # 通道未启用
    )

    assert sent == []


def test_health_deps_endpoint_is_public_and_reports_services(main_module, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import auth_service

    # 容器探针与外部健康检查要用，必须免鉴权
    assert any("/api/health/deps".startswith(prefix)
               for prefix in auth_service.AUTH_EXEMPT_PREFIXES)

    now = time.time()
    hb_dir = tmp_path / "heartbeat"
    _all_fresh(hb_dir, now)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))

    client = TestClient(main_module.app, raise_server_exceptions=False)
    response = client.get("/api/health/deps")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["status"] == "ok"
    assert body["heartbeat_dir"] == str(hb_dir)
    assert len(body["services"]) == 3
    assert body["alerts"] == []
    assert body["thresholds"]["guard"] == config.HEALTH_GUARD_MAX_AGE_SECONDS


def test_health_deps_endpoint_reports_stale_backup(main_module, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    now = time.time()
    hb_dir = tmp_path / "heartbeat"
    _all_fresh(str(hb_dir), now)
    _write_heartbeat(str(hb_dir), "bus_backup", ts=now, artifacts=[
        {"kind": "pg_dump", "name": "bt-20261009.dump", "bytes": 1,
         "ts": now - 48 * 3600},
    ])
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))

    client = TestClient(main_module.app, raise_server_exceptions=False)
    body = client.get("/api/health/deps").json()

    assert body["status"] == "degraded"
    assert "bus_backup/artifact_stale:pg_dump" in {a["key"] for a in body["alerts"]}
