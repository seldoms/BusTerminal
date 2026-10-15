import logging
from threading import Event
from types import SimpleNamespace

from discovery_service import DiscoveryService


def test_feed_preload_translates_cached_titles(monkeypatch):
    calls = []
    scraper = SimpleNamespace(
        base_url="https://www.javbus.com",
        get_movies=lambda *args, **kwargs: [
            {"id": "TEST-1", "original_title": "日本語", "title": "日本語", "cover": ""}
        ],
        batch_translate_to_chinese=lambda titles, **kwargs: ["中文标题"],
        get_movies_cache_key=lambda path, page, limit: f"movies_list_v2_{page}_{limit}",
    )
    service = object.__new__(DiscoveryService)
    service.scraper = scraper
    service.db = SimpleNamespace(set_cache=lambda key, value: calls.append((key, value)))
    service.stop_event = Event()
    service.logger = logging.getLogger(__name__)
    service.display_value_for_log = str
    service.safe_exception_label = lambda error: type(error).__name__
    service.schedule_image_cache_warmup = lambda *args: None
    monkeypatch.setattr("discovery_service.time.sleep", lambda _: None)

    service.preload_movie_feed_cache(max_pages=1)

    assert len(calls) == 3
    assert calls[0][0] == "movies_list_v2_1_30"
    assert all(value[0]["title"] == "中文标题" for _, value in calls)
