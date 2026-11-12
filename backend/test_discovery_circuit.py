"""发现页数据源的熔断行为。

背景：jable.tv / javdb.com 被 Cloudflare 按出口 IP 拉黑（同容器同一刻 javbus 仍 200），
而 feed 预热每 30 分钟全量重打，把它变成每天几百条告警。熔断的作用是让被封的源
安静下来，而不是无休止重试。
"""

import time
from unittest.mock import MagicMock

import pytest

from discovery_service import (
    SOURCE_COOLDOWN_SECONDS,
    SOURCE_FAILURE_THRESHOLD,
    DiscoveryService,
)


def _build_service():
    db = MagicMock()
    db.get_translations_batch.return_value = {}
    db.get_cache_full.return_value = {
        "value": [{"id": "cached-1", "title": "缓存片"}],
        "updated_at": time.time(),
    }
    scraper = MagicMock()
    service = DiscoveryService(
        db=db,
        scraper=scraper,
        jable_scraper=MagicMock(),
        discovery_scraper=MagicMock(),
        config=MagicMock(),
        logger=MagicMock(),
        stop_event=MagicMock(),
        cache_ttl_seconds=1800,
        safe_number=float,
        safe_exception_label=lambda exc: type(exc).__name__,
        display_value_for_log=str,
        normalize_magnet_url=lambda value: value,
        evaluate_download_permission=lambda *args, **kwargs: {"allowed": False},
        queue_download_if_absent=lambda *args, **kwargs: {},
        schedule_image_cache_warmup=lambda *args: None,
    )
    return service, db


def test_threshold_failures_trip_circuit():
    service, _ = _build_service()
    for _ in range(SOURCE_FAILURE_THRESHOLD - 1):
        service.record_source_failure("jable")
    assert service.is_source_cooling_down("jable") is False

    service.record_source_failure("jable")
    assert service.is_source_cooling_down("jable") is True


def test_success_resets_failure_counter():
    service, _ = _build_service()
    service.record_source_failure("jable")
    service.record_source_failure("jable")
    service.record_source_success("jable")
    service.record_source_failure("jable")
    assert service.is_source_cooling_down("jable") is False


def test_circuit_is_per_source():
    service, _ = _build_service()
    for _ in range(SOURCE_FAILURE_THRESHOLD):
        service.record_source_failure("jable")
    assert service.is_source_cooling_down("jable") is True
    assert service.is_source_cooling_down("javbus") is False


def test_cooldown_expires(monkeypatch):
    service, _ = _build_service()
    for _ in range(SOURCE_FAILURE_THRESHOLD):
        service.record_source_failure("javdb")
    assert service.is_source_cooling_down("javdb") is True

    future = time.time() + SOURCE_COOLDOWN_SECONDS + 1
    monkeypatch.setattr("discovery_service.time.time", lambda: future)
    assert service.is_source_cooling_down("javdb") is False


def test_fetch_movies_serves_cache_without_hitting_site_when_cooling_down():
    service, db = _build_service()
    for _ in range(SOURCE_FAILURE_THRESHOLD):
        service.record_source_failure("jable")

    movies = service.fetch_movies("jable", 1, "daily", force_refresh=True)

    assert movies == [{"id": "cached-1", "title": "缓存片"}]
    service.jable_scraper.get_hot_movies.assert_not_called()
    db.get_cache_full.assert_called_once()


def test_fetch_movies_returns_empty_when_cooling_down_and_no_cache():
    service, db = _build_service()
    db.get_cache_full.return_value = None
    for _ in range(SOURCE_FAILURE_THRESHOLD):
        service.record_source_failure("jable")

    assert service.fetch_movies("jable", 1, "daily") == []
    service.jable_scraper.get_hot_movies.assert_not_called()


def test_refresh_cache_skips_network_when_cooling_down():
    service, db = _build_service()
    db.get_cache_full.return_value = None
    for _ in range(SOURCE_FAILURE_THRESHOLD):
        service.record_source_failure("javbus")

    assert service.refresh_cache("javbus", 1, "daily") == []
    service.discovery_scraper.get_javbus_hot.assert_not_called()
    db.record_cache_event.assert_called_with(
        service.get_cache_key("javbus", 1, "daily"), "refresh_skipped_circuit_open"
    )


def test_refresh_cache_records_failure_and_trips_after_threshold():
    service, _ = _build_service()
    service.discovery_scraper.get_javbus_hot.side_effect = ConnectionError("blocked")

    for _ in range(SOURCE_FAILURE_THRESHOLD):
        with pytest.raises(ConnectionError):
            service.refresh_cache("javbus", 1, "daily")

    assert service.is_source_cooling_down("javbus") is True


def test_refresh_cache_success_clears_state():
    service, db = _build_service()
    service.discovery_scraper.get_javbus_hot.return_value = [{"id": "m1"}]
    service.record_source_failure("javbus")

    movies = service.refresh_cache("javbus", 1, "daily")

    # normalize_movie_list 会把番号/ID 规整成大写，这里只关心"取到了"和"熔断已复位"。
    assert len(movies) == 1
    assert service.is_source_cooling_down("javbus") is False
    db.set_cache.assert_called_once()


def test_circuit_state_snapshot_exposes_cooldown_flag():
    service, _ = _build_service()
    for _ in range(SOURCE_FAILURE_THRESHOLD):
        service.record_source_failure("jable")
    state = service.get_source_circuit_state()
    assert state["jable"]["cooling_down"] is True
