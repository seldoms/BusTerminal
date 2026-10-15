import logging
import re
import requests
import time
from bs4 import BeautifulSoup
from typing import Dict, List

from database import db, display_value_for_log
from scraper import HEADERS
from domain_router import DomainRouter
from scrapling_adapter import fetch_html_with_scrapling
from movie_normalization import normalize_movie_list

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def exception_type(exc: Exception) -> str:
    return type(exc).__name__


class DiscoveryScraper:
    """Multi-source movie discovery scraper."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.proxies = None
        self.domain_routers = {
            "javdb": DomainRouter(
                db=db,
                source_key="javdb",
                default_domains=["https://javdb.com"],
                logger=logger,
            ),
            "airav": DomainRouter(
                db=db,
                source_key="airav",
                default_domains=["https://airav.io", "https://airav.cc", "https://www.airav.cc"],
                logger=logger,
            ),
        }

    def reload_config(self):
        import config
        proxy_str = db.get_config('system_proxy') or config.SYSTEM_PROXY
        if proxy_str and proxy_str.strip():
            self.proxies = {"http": proxy_str, "https": proxy_str}
            self.session.proxies.update(self.proxies)
        else:
            self.proxies = {}
            self.session.proxies.clear()

    def get_javbus_hot(self, page: int = 1, force_refresh: bool = False) -> List[Dict]:
        from scraper import scraper as javbus_scraper
        return javbus_scraper.get_movies(
            url=javbus_scraper.base_url,
            page=page,
            limit=30,
            force_refresh=force_refresh,
            perform_translate=True,
            translate_mode="cache_only",
        )

    def get_javdb_hot(self, rank_type: str = "daily", page: int = 1, force_refresh: bool = False) -> List[Dict]:
        period = "daily"
        if "week" in rank_type.lower():
            period = "weekly"
        elif "month" in rank_type.lower():
            period = "monthly"

        cache_key = f"discovery_javdb_{rank_type}_p{page}"
        cached = db.get_cache(cache_key)
        if cached and not force_refresh:
            return normalize_movie_list(cached)

        router = self.domain_routers["javdb"]
        for base_url in router.candidates():
            url = f"{base_url}/rankings/movies?p={period}&t=censored&page={page}"
            try:
                started_at = time.time()
                content = None
                status = None
                final_url = url

                # 1) 优先普通 requests 快速抓取（8s），多数情况足够，避免 Scrapling 浏览器模拟开销
                try:
                    resp = self.session.get(url, timeout=8)
                    if resp.status_code == 200 and resp.content:
                        text = resp.content
                        # 仅当内容确实含影片列表结构时才采用，避免误用反爬挑战页
                        if b"movie-list" in text or b"video-title" in text or re.search(rb"[A-Z]{2,10}-\d{2,5}", text):
                            content = text
                            status = 200
                            final_url = resp.url
                except Exception as e:
                    logger.debug("Discovery: JavDB quick requests failed: %s", exception_type(e))

                # 2) 被反爬或失败 → 回退 Scrapling 浏览器模拟
                if not content or status != 200:
                    content, status, final_url = fetch_html_with_scrapling(url, timeout=20, proxies=self.proxies)

                # 3) Scrapling 仍失败 → 再用普通 requests 长超时兜底
                if not content or status != 200:
                    resp = self.session.get(url, timeout=15)
                    resp.raise_for_status()
                    content = resp.content
                    final_url = resp.url

                soup = BeautifulSoup(content, 'html.parser')
                movies = []
                for box in soup.select('.movie-list .item, div.item'):
                    title_tag = box.find('div', class_='video-title')
                    title = title_tag.get_text(" ", strip=True) if title_tag else box.get_text(" ", strip=True)
                    match = re.search(r'([A-Z]{2,10}-\d{2,5})', title.upper())
                    movie_id = match.group(1) if match else ""

                    img = box.find('img')
                    cover = img.get('data-src') or img.get('src') if img else ""

                    link = box.find('a')
                    href = link.get('href') if link else ""
                    detail_url = base_url + href if href and href.startswith('/') else href

                    if movie_id:
                        movies.append({
                            'id': movie_id,
                            'title': title,
                            'cover': cover,
                            'detail_url': detail_url or final_url,
                            'source': 'javdb'
                        })

                movies = normalize_movie_list(movies)
                if not movies:
                    raise RuntimeError("empty_result")
                db.set_cache(cache_key, movies)
                router.record_success(base_url, (time.time() - started_at) * 1000)
                return movies
            except Exception as e:
                router.record_failure(base_url, exception_type(e))
                logger.warning("Discovery: JavDB fail [%s]: %s", display_value_for_log(base_url), exception_type(e))
        return []

    def get_airav_hot(self) -> List[Dict]:
        router = self.domain_routers["airav"]
        for base_url in router.candidates():
            try:
                started_at = time.time()
                content, status, _ = fetch_html_with_scrapling(base_url, timeout=8, proxies=self.proxies)
                if not content and status is None:
                    continue
                if not content or status != 200:
                    resp = self.session.get(base_url, timeout=8)
                    resp.raise_for_status()
                    content = resp.content

                soup = BeautifulSoup(content, 'html.parser')
                movies = []
                for item in soup.select('div.oneVideo'):
                    img = item.select_one('img.index_video_cover') or item.find('img')
                    title = ""
                    h5 = item.select_one('.oneVideo-body h5')
                    if h5:
                        title = h5.get_text(" ", strip=True).replace('\xa0', ' ').strip()
                    if not title and img:
                        title = (img.get('alt') or "").replace('\xa0', ' ').strip()
                    if not title:
                        title = item.get_text(" ", strip=True)
                    cover = img.get('data-src') or img.get('src') if img else ""

                    link = item.find('a')
                    href = link.get('href') if link else ""
                    detail_url = base_url + href if href and href.startswith('/') else href

                    match = re.search(r'([A-Z0-9]+-\d+)', title.upper()) if title else None
                    movie_id = match.group(1) if match else ""

                    if movie_id or title:
                        movies.append({
                            'id': movie_id or "unknown",
                            'title': title,
                            'cover': cover,
                            'detail_url': detail_url,
                            'source': 'airav'
                        })
                movies = normalize_movie_list(movies)
                if movies:
                    router.record_success(base_url, (time.time() - started_at) * 1000)
                    return movies
                raise RuntimeError("empty_result")
            except Exception as e:
                router.record_failure(base_url, exception_type(e))
                logger.warning(
                    "Discovery: AirAV fail [%s]: %s",
                    display_value_for_log(base_url),
                    type(e).__name__,
                )
        return []


discovery_scraper = DiscoveryScraper()
