import logging
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)
logging.getLogger("scrapling").setLevel(logging.CRITICAL)


def safe_url_for_log(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return "<url>"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def exception_type(exc: Exception) -> str:
    return type(exc).__name__


def fetch_html_with_scrapling(
    url: str,
    *,
    timeout: int = 15,
    proxies: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[bytes], Optional[int], Optional[str]]:
    """Fetch a page through Scrapling's browser-impersonating HTTP fetcher."""
    try:
        from scrapling.fetchers import Fetcher
    except Exception as exc:
        logger.debug("Scrapling unavailable: %s", exception_type(exc))
        return None, None, "scrapling_unavailable"

    try:
        kwargs = {
            "timeout": timeout,
            "retries": 2,
            "impersonate": "chrome",
            "stealthy_headers": True,
        }
        if proxies:
            proxy = proxies.get("https") or proxies.get("http")
            if proxy:
                kwargs["proxy"] = proxy

        previous_disable_level = logging.root.manager.disable
        try:
            logging.disable(logging.ERROR)
            page = Fetcher.get(url, **kwargs)
        finally:
            logging.disable(previous_disable_level)
        return page.body, getattr(page, "status", None), getattr(page, "url", url)
    except Exception as exc:
        logger.warning("Scrapling fetch fail [%s]: %s", safe_url_for_log(url), exception_type(exc))
        return None, None, exception_type(exc)
