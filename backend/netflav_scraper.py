import json
import logging
import hashlib
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlparse

import requests
from bs4 import BeautifulSoup

import config
from database import db, display_value_for_log
from domain_router import DomainRouter

logger = logging.getLogger(__name__)

# netflav 官网**没有**备用域名公告（2026-11-04 实测：首页底部只挂「情色網站大全」
# 这类导航站，没有永久域名/防屏蔽地址清单），所以备用域名只能靠外部注入：
#   - 环境变量 NETFLAV_EXTRA_DOMAINS（逗号分隔）
#   - config 表 netflav_domains（逗号分隔，设置页可改）
# 注入的域名同样走 DomainRouter 的熔断与自动下线，能用的会被选优。
DEFAULT_DOMAINS = ["https://netflav.com"]


def _split_domains(raw: object) -> List[str]:
    if isinstance(raw, (list, tuple)):
        items = [str(item or "") for item in raw]
    else:
        items = str(raw or "").split(",")
    return [item.strip().rstrip("/") for item in items if item and item.strip()]


class NetflavScraper:
    LIST_CACHE_TTL = 24 * 3600
    DETAIL_CACHE_TTL = 30 * 24 * 3600

    def __init__(self, domain_provider=None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 BusTerminal/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        # 供测试注入，默认从 config/env 读
        self._domain_provider = domain_provider
        self.domain_router = DomainRouter(
            db=db,
            source_key="netflav",
            default_domains=self._configured_domains(),
            logger=logger,
        )
        self.base_url = self.domain_router.candidates()[0]
        self.reload_config()

    def _configured_domains(self) -> List[str]:
        if self._domain_provider is not None:
            try:
                extra = _split_domains(self._domain_provider())
            except Exception:
                extra = []
        else:
            extra = _split_domains(os.getenv("NETFLAV_EXTRA_DOMAINS", ""))
            stored = _split_domains(db.get_config("netflav_domains"))
            if stored:
                extra = stored + [item for item in extra if item not in stored]
        domains = list(DEFAULT_DOMAINS)
        for item in extra:
            if item not in domains:
                domains.append(item)
        return domains

    def reload_config(self):
        proxy_str = db.get_config("system_proxy") or config.SYSTEM_PROXY
        if proxy_str and str(proxy_str).strip():
            self.session.proxies.update({"http": proxy_str, "https": proxy_str})
        else:
            self.session.proxies.clear()
        # 运行中新增的镜像域名（设置页保存后调 reload_config 即可生效）
        added = self.domain_router.add_domains(
            self._configured_domains(), "configured"
        )
        if added:
            logger.info("Netflav loaded %s extra domain(s)", added)
        candidates = self.domain_router.candidates()
        if candidates:
            self.base_url = candidates[0]

    def allowed_hosts(self) -> List[str]:
        """netflav 侧允许的域名（不含协议）。

        订阅来源校验靠白名单挡 SSRF：只放行 JavBus 的域名。
        但库里也有**直接指向 netflav 的外部订阅**
        （`https://netflav.com/all?actress=…`，2026-11-07 实测 3 条），
        它们不在 JavBus 白名单里 → 每轮都被判 `invalid_subscription_url`，
        `auto_crawl_subscriptions` 的 failed_count 恒为 3。
        把 netflav 自己的域名（含配置注入的镜像）一并放进白名单。
        """
        hosts = set()
        for domain in self._configured_domains() + list(self.domain_router.candidates()):
            parsed = urlparse(str(domain or ""))
            if parsed.hostname:
                hosts.add(parsed.hostname.lower())
        return sorted(hosts)

    def actress_name_from_url(self, raw_url: Any) -> Optional[str]:
        """从 netflav 的演员页 URL 里取出女优名（没有则 None）。

        站点的演员页形态是 `/all?actress=<名字>`，名字是**日文原名**（URL 编码）。
        库里存着直接指向 netflav 的外部订阅（2026-11-07 实测 3 条），
        订阅层靠这个方法把它们识别成 netflav 发现目标 —— 当成 JavBus 页面去抓
        必然 404（而且连白名单都过不了）。
        """
        value = str(raw_url or "").strip()
        if not value:
            return None
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if not host or host not in set(self.allowed_hosts()):
            return None
        path = (parsed.path or "").rstrip("/")
        if path not in ("", "/all"):
            return None
        for name in parse_qs(parsed.query or "").get("actress") or []:
            text = str(name or "").strip()
            if text:
                return text
        return None

    def _fetch_html(self, path_or_url: str, timeout: int = 20, retries: int = 3, backoff_seconds: float = 5.0) -> str:
        """按域名候选轮询抓取；`path_or_url` 可以是绝对 URL 或站内路径。

        绝对 URL 只取 path+query —— 域名一律由 DomainRouter 决定，
        这样调用方不用关心当前用哪个镜像。
        """
        parsed = urlparse(str(path_or_url or ""))
        if parsed.scheme:
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
        else:
            path = str(path_or_url or "").lstrip("/")
            path = f"/{path}" if path else "/"

        last_error: Optional[Exception] = None
        candidates = self.domain_router.candidates()[:3]
        if not candidates:
            raise RuntimeError("no_netflav_domain")

        for base in candidates:
            url = f"{base}{path}"
            for attempt in range(1, retries + 1):
                started = time.time()
                try:
                    resp = self.session.get(url, timeout=timeout)
                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                        time.sleep(backoff_seconds * attempt)
                        continue
                    resp.raise_for_status()
                    self.domain_router.record_success(base, (time.time() - started) * 1000)
                    self.base_url = base
                    return resp.text
                except Exception as exc:
                    last_error = exc
                    if attempt >= retries:
                        break
                    time.sleep(backoff_seconds * attempt)
            self.domain_router.record_failure(base, type(last_error).__name__)
            logger.warning(
                "Netflav domain failed, trying mirror [%s]: %s",
                base,
                type(last_error).__name__,
            )
        raise last_error if last_error else RuntimeError("netflav_fetch_failed")

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
        return f"{self.base_url}/all?actress={quote(name or '')}"

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
        detail_url = f"{self.base_url}/video?id={video_id}" if video_id else None
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

    @staticmethod
    def standard_code(raw: object) -> Optional[str]:
        """把 netflav 的 code 归到库里统一的 `ABC-123` 形态。

        库内 movies.id 带横线；netflav 有时给 `mimk284`，直接拿去做详情页地址会 404。

        **不能借 `normalize_movie_code()`**：那个函数刻意去掉前导零（`JUR-097` → `JUR97`）
        用于跨源比对，拿它拼 URL 会把 `JUR-097` 变成 `JUR-97` 直接 404
        （2026-11-05 实测 netflav 片单里就混着 `JUR-97` 这种写法）。
        所以这里只做大小写/分隔符归一，数字原样保留。
        """
        text = str(raw or "").strip().upper()
        if not text:
            return None
        text = re.sub(r"[\s_]+", "-", text)
        text = re.sub(r"-{2,}", "-", text).strip("-")
        match = re.match(r"^([A-Z]{2,10})-?(\d{2,6})([A-Z]{0,4})$", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}{match.group(3)}"
        return text or None

    def list_movies_for_actress(
        self,
        name: str,
        *,
        limit: int = 30,
        force_refresh: bool = False,
    ) -> List[Dict]:
        """按女优名拉最新片单，供**订阅发现**使用。

        返回结构对齐 `JavBusScraper.get_movies` 的列表项（id/title/date/detail_url），
        所以订阅流程可以无差别消费。`detail_url` 故意留空：调用方会把详情换到
        当前可用的 JavBus 域名上（magnet 只有 JavBus 详情页有）。
        """
        name = (name or "").strip()
        if not name:
            return []

        safe_key = hashlib.sha1(name.encode("utf-8")).hexdigest()
        cache_key = f"netflav_actress_movies_{safe_key}"
        cached = None if force_refresh else self._get_cached(cache_key, self.LIST_CACHE_TTL)
        if cached is None:
            state = self._extract_next_state(
                self._fetch_html(f"/all?actress={quote(name)}")
            )
            docs = (state.get("all") or {}).get("docs") or []
            cached = []
            for item in docs:
                if not isinstance(item, dict):
                    continue
                code = self.standard_code(item.get("code") or item.get("uCode"))
                if not code:
                    continue
                actors = self._clean_values(item.get("actors") or [])
                cached.append({
                    "id": code,
                    "title": item.get("title_zh") or item.get("title") or code,
                    "original_title": item.get("title") or item.get("title_en") or code,
                    "cover": item.get("preview") or item.get("preview_hp"),
                    "date": self._date_from_iso(item.get("sourceDate")),
                    "detail_url": None,
                    "source": "netflav",
                    "actors": actors,
                })
            # 新片在前：订阅只关心「比上次看到的新」
            cached.sort(key=lambda movie: movie.get("date") or "", reverse=True)
            db.set_cache(cache_key, cached)

        return cached[: max(1, int(limit))]


        return self.get_actress_page(page=page, force_refresh=force_refresh).get("docs") or []

    def get_actress_page(self, page: int = 1, force_refresh: bool = False) -> Dict:
        cache_key = f"netflav_actress_list_p{page}"
        cached = None if force_refresh else self._get_cached(cache_key, self.LIST_CACHE_TTL)
        if cached is not None:
            if isinstance(cached, list):
                return {"docs": cached}
            return cached

        url = "/actress" if page <= 1 else f"/actress?page={page}"
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

        url = f"/actress?keyword={quote(keyword)}"
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

    def search_actress_by_candidates(
        self,
        candidates: List[str],
        force_refresh: bool = False,
    ) -> Optional[Dict]:
        """依次用多个候选名搜索女优，返回第一个命中的列表项。

        为什么需要多个候选：库里 actresses.name 存的是**中文译名**
        （`天使萌`），而 netflav 站上索引的是**日文原名**（`天使もえ`）。
        2026-11-07 生产实测：按中文译名搜 20 条命中 0 条，按日文原名搜命中 9 条。

        候选顺序很重要：**原名优先**。`_pick_search_match` 匹配不上时会退回
        `docs[0]`，所以先用最可能精确命中的名字，避免拿到别人的资料。
        """
        for keyword in candidates or []:
            value = str(keyword or "").strip()
            if not value:
                continue
            try:
                docs = self.search_actress(value, force_refresh=force_refresh)
            except Exception:
                continue
            if not docs:
                continue
            match = self._pick_search_match(docs, value)
            if match:
                return match
        return None

    def backfill_actress_by_candidates(
        self,
        candidates: List[str],
        target_id: Optional[str] = None,
        save_movies: bool = False,
    ) -> Optional[Dict]:
        """用候选名（优先日文原名）回填女优资料。"""
        match = self.search_actress_by_candidates(candidates)
        if not match:
            return None
        return self.get_actress_detail(
            match,
            force_refresh=False,
            save_movies=save_movies,
            target_id=target_id,
        )

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
