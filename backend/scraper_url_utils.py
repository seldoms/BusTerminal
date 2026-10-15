from typing import Any, Callable, Dict, Iterable, Optional, Set
from urllib.parse import unquote, urlparse

from movie_download_service import resolve_movie_detail_url
from subscription_url_utils import (
    normalize_internal_subscription_url,
    normalize_legacy_internal_subscription_alias,
)


def get_scraper_allowed_hosts(base_url: Any, all_domains: Iterable[Any]) -> Set[str]:
    hosts = set()
    for source in [base_url, *list(all_domains or [])]:
        parsed = urlparse(str(source or ""))
        if parsed.hostname:
            hosts.add(parsed.hostname.lower())
    return hosts


def normalize_scraper_url(raw_url: Any, *, base_url: Any, all_domains: Iterable[Any]) -> Optional[str]:
    value = str(raw_url or "").strip()
    if not value or len(value) > 2048 or "\x00" in value or "\\" in value:
        return None

    allowed_hosts = get_scraper_allowed_hosts(base_url, all_domains)
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        if parsed.hostname.lower() not in allowed_hosts:
            return None
        path_values = (parsed.path, unquote(parsed.path))
        if any(part == ".." for path in path_values for part in path.split("/")):
            return None
        return value.rstrip("/") if parsed.path not in ("", "/") else value.rstrip("/") or value

    if value.startswith("//"):
        return None
    if not value.startswith("/"):
        value = f"/{value}"

    relative = urlparse(value)
    path_values = (relative.path, unquote(relative.path))
    if any(part == ".." for path in path_values for part in path.split("/")):
        return None

    current_base_url = str(base_url or "").strip()
    if not current_base_url:
        domains = list(all_domains or [])
        current_base_url = str(domains[0] if domains else "").strip()
    if not current_base_url.startswith(("http://", "https://")):
        return None
    return f"{current_base_url.rstrip('/')}{value}"


def has_meaningful_path(raw_url: Any) -> bool:
    """URL 是否指向具体页面（路径里至少有一个非空段）。

    站点首页（`https://www.javbus.com`）路径为空，不能作为订阅目标 —— 它定位不到
    任何具体演员，多个演员的 page_url 都是首页时会互相覆盖订阅行。
    """
    value = str(raw_url or "").strip()
    if not value:
        return False
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return any(segment for segment in parsed.path.split("/") if segment)
    return any(segment for segment in value.split("?")[0].split("/") if segment)


def normalize_scraper_page_url(
    raw_url: Any,
    *,
    base_url: Any,
    all_domains: Iterable[Any],
) -> Optional[str]:
    """同 normalize_scraper_url，但额外要求 URL 指向具体页面而非站点首页。"""
    url = normalize_scraper_url(raw_url, base_url=base_url, all_domains=all_domains)
    if not url or not has_meaningful_path(url):
        return None
    return url


def normalize_subscription_source_url(
    raw_url: Any,
    *,
    normalize_external_url: Callable[[Any], Optional[str]],
) -> Optional[str]:
    value = str(raw_url or "").strip()
    internal_url = normalize_internal_subscription_url(value)
    if internal_url:
        return internal_url
    legacy_internal_url = normalize_legacy_internal_subscription_alias(value)
    if legacy_internal_url:
        return legacy_internal_url
    return normalize_external_url(value)


def resolve_movie_detail_url_for_download(
    raw_url: Any,
    *,
    normalize_url: Callable[[Any], Optional[str]],
    normalize_video_id: Callable[[Any], Optional[str]],
    movie_id: Any = None,
    title: Any = None,
) -> Optional[Dict[str, Optional[str]]]:
    return resolve_movie_detail_url(
        raw_url,
        normalize_url=normalize_url,
        normalize_video_id=normalize_video_id,
        movie_id=movie_id,
        title=title,
    )
