import json
import logging
import hashlib
import re
import time
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

import config
from database import db, display_value_for_log

logger = logging.getLogger(__name__)


class NetflavScraper:
    BASE_URL = "https://netflav.com"
    LIST_CACHE_TTL = 24 * 3600
    DETAIL_CACHE_TTL = 30 * 24 * 3600

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 BusTerminal/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self.reload_config()

    def reload_config(self):
        proxy_str = db.get_config("system_proxy") or config.SYSTEM_PROXY
        if proxy_str and str(proxy_str).strip():
            self.session.proxies.update({"http": proxy_str, "https": proxy_str})
        else:
            self.session.proxies.clear()

    def _fetch_html(self, url: str, timeout: int = 20, retries: int = 3, backoff_seconds: float = 5.0) -> str:
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.get(url, timeout=timeout)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(backoff_seconds * attempt)
                    continue
                resp.raise_for_status()
                return resp.text
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= retries:
                    break
                time.sleep(backoff_seconds * attempt)
        raise last_error

    def _extract_next_state(self, html: str) -> Dict:
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            return {}
        data = json.loads(script.string)
        return data.get("props", {}).get("initialState", {}) or {}

    def _get_cached(self, key: str, ttl_seconds: int):
        cache = db.get_cache_full(key)
        if not cache:
            return None
        age = time.time() - float(cache.get("updated_at") or 0)
        if age >= ttl_seconds:
            return None
        return cache.get("value")

    def _parse_iso_ts(self, value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    def _date_from_iso(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            return value
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except Exception:
            return None

    def _number_text(self, value) -> Optional[str]:
        if value is None:
            return None
        match = re.search(r"\d+(?:\.\d+)?", str(value))
        return match.group(0) if match else None

    def _clean_values(self, values: List[str]) -> List[str]:
        cleaned = []
        seen = set()
        for value in values or []:
            text = re.sub(r"^(jp|zh|en):", "", str(value)).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        return cleaned

    def _actress_page_url(self, name: str) -> str:
        return f"{self.BASE_URL}/all?actress={quote(name or '')}"

    def _build_actress_data(self, list_item: Dict, detail: Optional[Dict] = None) -> Dict:
        detail = detail or {}
        source_id = list_item.get("_id") or detail.get("_id") or detail.get("uid")
        name = detail.get("name") or list_item.get("name") or ""
        aliases = []
        for alias in (detail.get("name_en"), detail.get("name_ja"), detail.get("name_zh"), list_item.get("name_en")):
            if alias and alias != name and alias not in aliases:
                aliases.append(alias)

        return {
            "id": f"netflav:{source_id}",
            "name": name,
            "avatar": detail.get("icon") or list_item.get("icon"),
            "birth_date": detail.get("birthday"),
            "height": self._number_text(detail.get("height")),
            "cup": detail.get("cup"),
            "bust": self._number_text(detail.get("breast")),
            "waist": self._number_text(detail.get("waist")),
            "hip": self._number_text(detail.get("hip")),
            "hobby": detail.get("hobby"),
            "page_url": self._actress_page_url(name),
            "aliases": ",".join(aliases),
            "name_en": detail.get("name_en") or list_item.get("name_en"),
            "source": "netflav",
            "source_id": source_id,
            "source_updated_at": self._parse_iso_ts(detail.get("updatedAt") or detail.get("lastUpdate")),
            "video_count": detail.get("videoCount") or list_item.get("videoCount"),
            "metadata_last_attempt_at": time.time() if detail else None,
            "metadata_attempt_count": 1 if detail else 0,
        }

    def _build_movie_data(self, item: Dict, actress_data: Dict) -> Optional[Dict]:
        code = item.get("code") or item.get("uCode")
        if not code:
            return None

        video_id = item.get("videoId")
        detail_url = f"{self.BASE_URL}/video?id={video_id}" if video_id else None
        actors = self._clean_values(item.get("actors") or [])
        tags = self._clean_values(item.get("tags") or [])
        title = item.get("title_zh") or item.get("title") or item.get("title_en") or code
        original_title = item.get("title_en") or item.get("title") or title

        stars = [{"id": actress_data["id"], "name": actress_data["name"], "source": "netflav"}]
        for actor in actors:
            if actor != actress_data["name"] and not any(s["name"] == actor for s in stars):
                stars.append({"id": f"netflav:name:{actor}", "name": actor, "source": "netflav"})

        detail = {
            "source": "netflav",
            "netflav_id": item.get("_id"),
            "video_id": video_id,
            "code": code,
            "title": title,
            "title_en": item.get("title_en"),
            "tags": tags,
            "raw_tags": item.get("tags") or [],
            "actors": actors,
            "stars": stars,
            "preview_video": item.get("previewVideo"),
            "preview_images_url": item.get("previewImagesUrl"),
            "has_magnets": item.get("hasMagnets"),
            "views": item.get("views"),
        }

        return {
            "id": code,
            "title": title,
            "original_title": original_title,
            "cover": item.get("preview") or item.get("preview_hp"),
            "detail_url": detail_url,
            "date": self._date_from_iso(item.get("sourceDate")),
            "detail": detail,
        }

    def get_actress_list(self, page: int = 1, force_refresh: bool = False) -> List[Dict]:
        return self.get_actress_page(page=page, force_refresh=force_refresh).get("docs") or []

    def get_actress_page(self, page: int = 1, force_refresh: bool = False) -> Dict:
        cache_key = f"netflav_actress_list_p{page}"
        cached = None if force_refresh else self._get_cached(cache_key, self.LIST_CACHE_TTL)
        if cached is not None:
            if isinstance(cached, list):
                return {"docs": cached}
            return cached

        url = f"{self.BASE_URL}/actress" if page <= 1 else f"{self.BASE_URL}/actress?page={page}"
        state = self._extract_next_state(self._fetch_html(url))
        page_data = state.get("actress", {}) or {}
        db.set_cache(cache_key, page_data)
        return page_data

    def search_actress(self, keyword: str, force_refresh: bool = False) -> List[Dict]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        safe_key = hashlib.sha1(keyword.encode("utf-8")).hexdigest()
        cache_key = f"netflav_actress_search_{safe_key}"
        cached = None if force_refresh else self._get_cached(cache_key, self.LIST_CACHE_TTL)
        if cached is not None:
            return cached

        url = f"{self.BASE_URL}/actress?keyword={quote(keyword)}"
        state = self._extract_next_state(self._fetch_html(url))
        docs = state.get("actress", {}).get("docs") or []
        db.set_cache(cache_key, docs)
        return docs

    def _pick_search_match(self, docs: List[Dict], name: str) -> Optional[Dict]:
        if not docs:
            return None
        target = (name or "").strip().lower()
        for item in docs:
            candidates = [
                item.get("name"),
                item.get("name_en"),
                item.get("name_ja"),
                item.get("name_zh"),
            ]
            if any(str(candidate or "").strip().lower() == target for candidate in candidates):
                return item
        return docs[0]

    def get_actress_detail(self, list_item: Dict, force_refresh: bool = False, save_movies: bool = True, target_id: Optional[str] = None) -> Optional[Dict]:
        source_id = list_item.get("_id")
        name = list_item.get("name")
        if not source_id or not name:
            return None

        cache_key = f"netflav_actress_detail_{source_id}"
        cached = None if force_refresh else self._get_cached(cache_key, self.DETAIL_CACHE_TTL)
        if cached is None:
            state = self._extract_next_state(self._fetch_html(self._actress_page_url(name)))
            all_state = state.get("all", {}) or {}
            cached = {
                "actress": all_state.get("actress") or {},
                "movies": all_state.get("docs") or [],
            }
            db.set_cache(cache_key, cached)

        actress_data = self._build_actress_data(list_item, cached.get("actress") or {})
        if target_id:
            actress_data["id"] = target_id
        db.save_actress(actress_data)

        if save_movies:
            for movie_item in cached.get("movies") or []:
                movie_data = self._build_movie_data(movie_item, actress_data)
                if movie_data:
                    db.save_movie(movie_data)

        return actress_data

    def backfill_actress_by_name(self, name: str, target_id: Optional[str] = None, save_movies: bool = False) -> Optional[Dict]:
        docs = self.search_actress(name)
        match = self._pick_search_match(docs, name)
        if not match:
            return None
        return self.get_actress_detail(match, force_refresh=False, save_movies=save_movies, target_id=target_id)

    def refresh_actresses(
        self,
        *,
        max_pages: int = 1,
        detail_limit: int = 20,
        force_refresh: bool = False,
        delay_seconds: float = 1.2,
        save_movies: bool = True,
    ) -> Dict:
        self.reload_config()
        report = {"pages": 0, "list_count": 0, "detail_count": 0, "movie_pages_saved": 0, "errors": []}
        detail_count = 0

        for page in range(1, max_pages + 1):
            docs = self.get_actress_list(page=page, force_refresh=force_refresh)
            report["pages"] += 1
            report["list_count"] += len(docs)
            for item in docs:
                list_data = self._build_actress_data(item)
                db.save_actress(list_data)
                if detail_count >= detail_limit:
                    continue
                try:
                    detail = self.get_actress_detail(item, force_refresh=force_refresh, save_movies=save_movies)
                    if detail:
                        detail_count += 1
                        report["detail_count"] += 1
                        if save_movies:
                            report["movie_pages_saved"] += 1
                    time.sleep(delay_seconds)
                except Exception as exc:
                    error_type = type(exc).__name__
                    logger.warning(
                        "Netflav actress detail failed [%s]: %s",
                        display_value_for_log(item.get("name")),
                        error_type,
                    )
                    report["errors"].append({
                        "name": display_value_for_log(item.get("name")),
                        "error_type": error_type,
                    })
        return report


netflav_scraper = NetflavScraper()
