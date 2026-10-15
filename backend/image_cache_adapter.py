from pathlib import Path
from typing import Any, List, Optional


def sync_image_cache_service_root(image_cache_service: Any, cache_root: Any) -> None:
    root_path = Path(cache_root)
    if image_cache_service.cache_root == root_path:
        return
    image_cache_service.cache_root = root_path
    image_cache_service.proxy_dir = root_path / "proxy"
    image_cache_service.failure_dir = root_path / "proxy_failures"


def remote_image_url_rejection_reason(image_cache_service: Any, cache_root: Any, image_url: str) -> Optional[str]:
    sync_image_cache_service_root(image_cache_service, cache_root)
    return image_cache_service.rejection_reason(image_url)


def normalize_remote_image_cache_url(image_cache_service: Any, cache_root: Any, image_url: Any) -> Optional[str]:
    sync_image_cache_service_root(image_cache_service, cache_root)
    return image_cache_service.normalize_url(image_url)


def get_remote_image_cache_keys(image_cache_service: Any, cache_root: Any, image_url: Any) -> List[str]:
    sync_image_cache_service_root(image_cache_service, cache_root)
    return image_cache_service.get_cache_keys(image_url)


def find_remote_image_cache_file(image_cache_service: Any, cache_root: Any, image_url: Any):
    sync_image_cache_service_root(image_cache_service, cache_root)
    return image_cache_service.find_cached_original(image_url)


def cache_remote_image_url(image_cache_service: Any, cache_root: Any, url: str) -> Optional[str]:
    sync_image_cache_service_root(image_cache_service, cache_root)
    return image_cache_service.cache_remote_url(url)


def collect_remote_image_urls(image_cache_service: Any, cache_root: Any, items: Any, limit: int) -> List[str]:
    sync_image_cache_service_root(image_cache_service, cache_root)
    return image_cache_service.collect_remote_image_urls(items, limit)


def get_proxy_image(image_cache_service: Any, cache_root: Any, image_url: str, width: Any):
    sync_image_cache_service_root(image_cache_service, cache_root)
    return image_cache_service.get_proxy_image(image_url, width)


def get_stats(image_cache_service: Any, cache_root: Any):
    sync_image_cache_service_root(image_cache_service, cache_root)
    return image_cache_service.get_stats()


def invalidate_category(image_cache_service: Any, cache_root: Any, category: str):
    sync_image_cache_service_root(image_cache_service, cache_root)
    return image_cache_service.invalidate_category(category)


def warm_remote_cache(image_cache_service: Any, cache_root: Any, image_urls: List[str], stop_event: Any):
    sync_image_cache_service_root(image_cache_service, cache_root)
    return image_cache_service.warm_remote_cache(image_urls, stop_event)
