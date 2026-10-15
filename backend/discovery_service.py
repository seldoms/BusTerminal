import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from movie_normalization import normalize_movie_list
from cover_upgrade_service import is_javdb_cover, should_replace_cover


DISCOVERY_SOURCES = {"jable", "javbus", "javdb", "airav"}
DISCOVERY_RANK_TYPES = {"daily", "weekly", "monthly"}
INFORMATION_FLOW_CATEGORY_TARGETS = (
    ("home", "https://www.javbus.com"),
    ("subtitles", "/genre/sub"),
    ("hd", "/genre/hd"),
)
MAX_PRELOAD_PAGES = 30


class DiscoveryService:
    def __init__(
        self,
        *,
        db: Any,
        scraper: Any,
        jable_scraper: Any,
        discovery_scraper: Any,
        config: Any,
        logger: Any,
        stop_event: Any,
        cache_ttl_seconds: int,
        safe_number: Callable[..., float],
        safe_exception_label: Callable[[Exception], str],
        display_value_for_log: Callable[[Any], str],
        normalize_magnet_url: Callable[[Any], Optional[str]],
        evaluate_download_permission: Callable[..., Dict[str, Any]],
        queue_download_if_absent: Callable[[str, str, str, Optional[str]], Dict[str, Any]],
        schedule_image_cache_warmup: Callable[[Any, str], None],
    ):
        self.db = db
        self.scraper = scraper
        self.jable_scraper = jable_scraper
        self.discovery_scraper = discovery_scraper
        self.config = config
        self.logger = logger
        self.stop_event = stop_event
        self.cache_ttl_seconds = cache_ttl_seconds
        self.safe_number = safe_number
        self.safe_exception_label = safe_exception_label
        self.display_value_for_log = display_value_for_log
        self.normalize_magnet_url = normalize_magnet_url
        self.evaluate_download_permission = evaluate_download_permission
        self.queue_download_if_absent = queue_download_if_absent
        self.schedule_image_cache_warmup = schedule_image_cache_warmup
        self.refreshing_keys = set()
        self.refresh_lock = threading.Lock()
        self.refresh_scheduler: Optional[Callable[[str, int, str, str], bool]] = None

    def get_sources(self) -> List[Dict[str, str]]:
        return [
            {"id": "jable", "name": "Jable.tv", "description": "Jable discovery"},
            {"id": "javbus", "name": "JavBus", "description": "JavBus discovery"},
            {"id": "javdb", "name": "JavDB", "description": "JavDB discovery"},
            {"id": "airav", "name": "AirAV", "description": "AirAV discovery"},
        ]

    def get_jable_hot_movies(
        self,
        page: int,
        limit: int,
        sort_by: str,
        force_refresh: bool,
        translate: bool,
    ) -> List[Dict]:
        movies = normalize_movie_list(self.jable_scraper.get_hot_movies(
            page,
            limit,
            sort_by,
            force_refresh,
            translate,
        ))
        self.schedule_image_cache_warmup(movies, "jable_hot")
        return movies

    def get_cache_key(self, source: str, page: int, rank_type: str) -> str:
        safe_source = re.sub(r"[^a-zA-Z0-9_-]", "_", source or "unknown")
        safe_rank = re.sub(r"[^a-zA-Z0-9_-]", "_", rank_type or "default")
        return f"discovery_movies_{safe_source}_{safe_rank}_p{page}"

    def apply_cached_movie_title_translations(self, movies: List[Dict]) -> List[Dict]:
        if not movies:
            return movies

        originals = [
            str(movie.get("original_title") or movie.get("title") or "").strip()
            for movie in movies
            if isinstance(movie, dict)
        ]
        cached_translations = self.db.get_translations_batch([text for text in originals if text])
        if not cached_translations:
            return movies

        translated_movies = []
        for movie in movies:
            if not isinstance(movie, dict):
                translated_movies.append(movie)
                continue

            original_title = str(movie.get("original_title") or movie.get("title") or "").strip()
            translated_title = cached_translations.get(original_title)
            if translated_title and translated_title != movie.get("title"):
                next_movie = dict(movie)
                if not next_movie.get("original_title"):
                    next_movie["original_title"] = original_title
                next_movie["title"] = translated_title
                translated_movies.append(next_movie)
            else:
                translated_movies.append(movie)

        return translated_movies

    def normalize_source(self, raw_source: Any) -> Optional[str]:
        source = str(raw_source or "").strip().lower()
        return source if source in DISCOVERY_SOURCES else None

    def normalize_rank_type(self, raw_rank_type: Any) -> Optional[str]:
        rank_type = str(raw_rank_type or "").strip().lower()
        return rank_type if rank_type in DISCOVERY_RANK_TYPES else None

    def fetch_movies(self, source: str, page: int, rank_type: str, force_refresh: bool = False) -> List[Dict]:
        self.discovery_scraper.reload_config()
        if source == "jable":
            # 预加载必须写入页面实际读取的翻译缓存，否则用户打开 Jable 热门时仍会现场抓取。
            movies = self.jable_scraper.get_hot_movies(
                page=page,
                sort_by=rank_type,
                force_refresh=force_refresh,
                translate=True,
            )
        elif source == "javbus":
            movies = self.discovery_scraper.get_javbus_hot(page=page, force_refresh=force_refresh)
        elif source == "javdb":
            movies = self.discovery_scraper.get_javdb_hot(rank_type=rank_type, page=page, force_refresh=force_refresh)
        elif source == "airav":
            movies = self.discovery_scraper.get_airav_hot()
        else:
            raise ValueError("unsupported discovery source")
        movies = normalize_movie_list(movies)
        for movie in movies:
            movie_id = movie.get("id")
            if not movie_id:
                continue
            try:
                lifecycle = self.db.get_movie_lifecycle(movie_id=movie_id, limit=1)
                if lifecycle and lifecycle[0].get("discovery_status") == "discovered":
                    continue
                self.db.record_lifecycle_event(
                    movie_id,
                    "content_discovered",
                    title=movie.get("title"),
                    discovery_status="discovered",
                    source=f"discovery:{source}",
                    reason="normalized discovery result",
                    execution_result="success",
                )
            except Exception as exc:
                self.logger.warning(
                    "Discovery lifecycle audit skipped: %s",
                    self.safe_exception_label(exc),
                )
        return self.upgrade_covers(movies, source)

    def upgrade_covers(self, movies: List[Dict], source: str) -> List[Dict]:
        """按来源优先级提升封面清晰度。

        实测 JavDB 800×538（142KB）明显优于 DMM 590×800（19KB）。
        这里做两件事，**只升不降**：
        - 当前就是 JavDB 来源 → 直接用它自带的高清封面；
        - 其他来源 → 若库里此前已存过 JavDB 封面，用高清那张替换。

        存量 145 万条不做回填（用户决定），只影响此后流经这里的条目。
        """
        if not movies:
            return movies

        upgraded = 0
        for movie in movies:
            try:
                movie_id = str(movie.get("id") or "").strip()
                if not movie_id:
                    continue
                incoming = str(movie.get("cover") or "").strip()

                if source == "javdb" and is_javdb_cover(incoming):
                    # 本条就是 JavDB 的高清封面，直接采用
                    if incoming:
                        movie["cover"] = incoming
                        upgraded += 1
                    continue

                # 其他来源：看库里是否已有 JavDB 封面可替换
                existing = self.db.get_movie_cover(movie_id) if hasattr(self.db, "get_movie_cover") else ""
                if is_javdb_cover(existing) and should_replace_cover(incoming, existing):
                    movie["cover"] = existing
                    upgraded += 1
            except Exception as exc:
                self.logger.debug(
                    "Discovery cover upgrade skipped: %s", self.safe_exception_label(exc)
                )

        if upgraded:
            self.logger.info("Discovery: upgraded %s covers (source=%s)", upgraded, source)
        return movies

    def refresh_cache(
        self,
        source: str,
        page: int,
        rank_type: str,
        cache_key: Optional[str] = None,
        force_refresh: bool = True,
    ) -> List[Dict]:
        cache_key = cache_key or self.get_cache_key(source, page, rank_type)
        try:
            movies = self.fetch_movies(source, page, rank_type, force_refresh=force_refresh)
        except Exception:
            self.db.record_cache_event(cache_key, "refresh_failure")
            raise
        if movies:
            self.db.set_cache(cache_key, movies)
            self.db.record_cache_event(cache_key, "refresh_success")
        else:
            self.db.record_cache_event(cache_key, "refresh_failure")
        return movies

    def get_cache_age_seconds(self, cache_full: Optional[Dict[str, Any]]) -> float:
        if not cache_full:
            return 0.0
        updated_at = self.safe_number(cache_full.get("updated_at"), 0)
        return max(time.time() - updated_at, 0)

    def schedule_refresh(self, source: str, page: int, rank_type: str, cache_key: str) -> bool:
        if self.refresh_scheduler:
            return bool(self.refresh_scheduler(source, page, rank_type, cache_key))

        with self.refresh_lock:
            if cache_key in self.refreshing_keys:
                self.db.record_cache_event(cache_key, "refresh_deduplicated")
                return False
            self.refreshing_keys.add(cache_key)

        def refresh_once():
            try:
                self.refresh_cache(source, page, rank_type, cache_key)
            except Exception as e:
                self.logger.warning(
                    "Discovery background refresh failed [%s p%s]: %s",
                    self.display_value_for_log(source),
                    page,
                    self.safe_exception_label(e),
                )
            finally:
                with self.refresh_lock:
                    self.refreshing_keys.discard(cache_key)

        threading.Thread(target=refresh_once, name=f"discovery_refresh_{source}_{page}", daemon=True).start()
        return True

    def get_refreshing_count(self) -> int:
        with self.refresh_lock:
            return len(self.refreshing_keys)

    def get_raw_movies(self, source: str, page: int, rank_type: str) -> Dict[str, Any]:
        movies = self.fetch_movies(source, page, rank_type, force_refresh=False)
        if movies:
            self.schedule_image_cache_warmup(movies, f"discovery_raw_{source}")
        return {"data": movies, "source": source, "page": page}

    def get_cached_movies(self, source: str, page: int, rank_type: str, force_refresh: bool = False) -> Dict[str, Any]:
        cache_key = self.get_cache_key(source, page, rank_type)
        cache_full = self.db.get_cache_full(cache_key, track=not force_refresh)

        if cache_full and cache_full.get("value") and not force_refresh:
            cached_movies = normalize_movie_list(
                self.apply_cached_movie_title_translations(cache_full["value"])
            )
            self.schedule_image_cache_warmup(cached_movies, f"discovery_cached_{source}")
            age_seconds = self.get_cache_age_seconds(cache_full)
            stale = age_seconds >= self.cache_ttl_seconds
            if stale:
                self.db.record_cache_event(cache_key, "stale")
                self.schedule_refresh(source, page, rank_type, cache_key)
            return {
                "data": cached_movies,
                "source": source,
                "page": page,
                "cached": True,
                "stale": stale,
                "cache_status": "stale" if stale else "fresh",
                "cache_age_seconds": int(max(age_seconds, 0)),
            }

        movies = self.refresh_cache(source, page, rank_type, cache_key, force_refresh)
        if not movies:
            if cache_full and cache_full.get("value"):
                self.db.record_cache_event(cache_key, "stale")
                cached_movies = normalize_movie_list(
                    self.apply_cached_movie_title_translations(cache_full["value"])
                )
                self.schedule_image_cache_warmup(cached_movies, f"discovery_stale_{source}")
                age_seconds = self.get_cache_age_seconds(cache_full)
                return {
                    "data": cached_movies,
                    "source": source,
                    "page": page,
                    "cached": True,
                    "stale": True,
                    "cache_status": "stale",
                    "cache_age_seconds": int(age_seconds),
                    "warning": "remote refresh failed, returned local cache",
                }
            return {
                "data": [],
                "source": source,
                "page": page,
                "empty": True,
                "cached": False,
                "stale": False,
                "cache_status": "miss",
            }

        translated_movies = normalize_movie_list(self.apply_cached_movie_title_translations(movies))
        self.schedule_image_cache_warmup(translated_movies, f"discovery_refresh_{source}")
        return {
            "data": translated_movies,
            "source": source,
            "page": page,
            "cached": False,
            "stale": False,
            "cache_status": "miss",
            "cache_age_seconds": 0,
        }

    def preload_discovery_cache(self, max_pages: int = 1, force_refresh: bool = False) -> None:
        sources = ("javbus", "jable", "javdb", "airav")
        rank_types = {
            "jable": ("daily", "weekly", "monthly"),
            "javdb": ("daily", "weekly", "monthly"),
        }
        pages = range(1, max(1, min(int(max_pages), MAX_PRELOAD_PAGES)) + 1)

        for source in sources:
            for rank_type in rank_types.get(source, ("daily",)):
                for page in pages:
                    if self.stop_event.is_set():
                        return
                    try:
                        cache_key = self.get_cache_key(source, page, rank_type)
                        cache_full = self.db.get_cache_full(cache_key)
                        if force_refresh:
                            movies = self.refresh_cache(source, page, rank_type, cache_key, force_refresh=True)
                            self.schedule_image_cache_warmup(movies, f"discovery_preload_{source}")
                        elif cache_full and cache_full.get("value"):
                            age_seconds = self.get_cache_age_seconds(cache_full)
                            if age_seconds < self.cache_ttl_seconds:
                                self.schedule_image_cache_warmup(cache_full.get("value"), f"discovery_preload_cached_{source}")
                                continue
                            movies = self.refresh_cache(source, page, rank_type, cache_key, force_refresh=True)
                            self.schedule_image_cache_warmup(movies, f"discovery_preload_{source}")
                        else:
                            movies = self.refresh_cache(source, page, rank_type, cache_key, force_refresh=False)
                            self.schedule_image_cache_warmup(movies, f"discovery_preload_{source}")
                    except Exception as e:
                        self.logger.warning(
                            "Discovery preload failed [%s %s p%s]: %s",
                            self.display_value_for_log(source),
                            self.display_value_for_log(rank_type),
                            page,
                            self.safe_exception_label(e),
                        )
                    time.sleep(1)

    def preload_movie_feed_cache(self, max_pages: int = 3, force_refresh: bool = False) -> None:
        pages = range(1, max(1, min(int(max_pages), MAX_PRELOAD_PAGES)) + 1)

        for name, url in INFORMATION_FLOW_CATEGORY_TARGETS:
            for page in pages:
                if self.stop_event.is_set():
                    return
                try:
                    movies = self.scraper.get_movies(
                        url,
                        page=page,
                        limit=30,
                        force_refresh=force_refresh,
                        perform_translate=True,
                        translate_mode="cache_only",
                    )
                    if movies:
                        untranslated = [
                            movie for movie in movies
                            if movie.get("original_title")
                            and movie.get("title") == movie.get("original_title")
                        ]
                        if untranslated:
                            titles = [movie["original_title"] for movie in untranslated]
                            translations = self.scraper.batch_translate_to_chinese(
                                titles,
                                wait_for_lock=False,
                            )
                            changed = False
                            for movie, translated in zip(untranslated, translations):
                                if translated and translated != movie["original_title"]:
                                    movie["title"] = translated
                                    changed = True
                            if changed:
                                cache_key = self.scraper.get_movies_cache_key(url, page, 30)
                                self.db.set_cache(cache_key, movies)
                    self.logger.info(
                        "Information flow preload [%s p%s] cached %s movies",
                        self.display_value_for_log(name),
                        page,
                        len(movies or []),
                    )
                    self.schedule_image_cache_warmup(movies, f"feed_preload_{name}")
                except Exception as e:
                    self.logger.warning(
                        "Information flow preload failed [%s p%s]: %s",
                        self.display_value_for_log(name),
                        page,
                        self.safe_exception_label(e),
                    )
                time.sleep(1)

    def preload_information_flow_cache(self, max_pages: int = 3, force_refresh: bool = False) -> None:
        pages = max(1, min(int(max_pages), MAX_PRELOAD_PAGES))
        self.preload_movie_feed_cache(max_pages=pages, force_refresh=force_refresh)
        self.preload_discovery_cache(max_pages=pages, force_refresh=force_refresh)

    def jable_hot_auto_task_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                now = datetime.now()
                is_10pm = now.hour == 22 and now.minute < 30

                if not is_10pm:
                    self.stop_event.wait(60)
                    continue

                enabled = self.db.get_config("jable_hot_auto_download")
                if enabled is None:
                    enabled = self.config.JABLE_HOT_AUTO_DOWNLOAD

                if not enabled:
                    self.logger.debug("Jable hot auto download is disabled; skipping this check.")
                else:
                    self._run_jable_hot_discovery_cycle(now)

            except Exception as e:
                self.logger.error("Jable hot discovery loop failed: %s", self.safe_exception_label(e))

            self.stop_event.wait(60)

    def _run_jable_hot_discovery_cycle(self, now: datetime) -> None:
        self.logger.info("[%s] Starting Jable hot discovery cycle", now.strftime("%Y-%m-%d %H:%M:%S"))

        for sort_by in ["video_viewed_today", "video_viewed_week", "video_viewed_month"]:
            if self.stop_event.is_set():
                break

            try:
                movies = self.jable_scraper.get_hot_movies(page=1, limit=30, sort_by=sort_by, translate=False)
                if not movies:
                    continue

                self.logger.info("Jable %s: fetched %s videos", sort_by, len(movies))

                for movie in movies:
                    self.db.save_jable_hot_item(movie["id"], sort_by, movie.get("title", "Unknown"))
            except Exception as e:
                self.logger.error("Failed to fetch Jable %s hot list: %s", sort_by, self.safe_exception_label(e))

        pending = self.db.get_pending_jable_hot_items(limit=20)
        if pending:
            self.logger.info("Processing %s pending Jable hot items", len(pending))
            current_best = self.scraper.find_best_domain()
            self.scraper.base_url = current_best

            for item in pending:
                if self.stop_event.is_set():
                    break
                self._process_pending_jable_hot_item(item)

        self.logger.info("Jable hot discovery cycle finished.")

    def _process_pending_jable_hot_item(self, item: Dict[str, Any]) -> None:
        movie_id = item["video_id"]
        sort_by = item["sort_by"]

        if self.db.is_movie_downloaded(movie_id):
            self.db.update_jable_hot_status(movie_id, sort_by, "downloaded")
            return

        if self.db.get_local_file(movie_id):
            self.db.update_jable_hot_status(movie_id, sort_by, "downloaded")
            self.db.record_download(movie_id)
            return

        self.logger.info("Searching magnet links for Jable hot item %s", movie_id)
        try:
            detail_url = f"{self.scraper.base_url}/{movie_id}"
            detail = self.scraper.get_movie_detail(detail_url, perform_translate=False)

            if not detail or not detail.get("magnets"):
                self.db.save_pending_download(movie_id, item["title"], source="jable", note="movie_not_found")
                self.db.update_jable_hot_status(movie_id, sort_by, "failed_not_found")
                return

            best_magnet = self.scraper.select_best_magnet(detail["magnets"], preference="1080P")
            if not best_magnet:
                self.db.save_pending_download(movie_id, item["title"], source="jable", note="no_suitable_magnet")
                self.db.update_jable_hot_status(movie_id, sort_by, "failed_no_magnet")
                return

            selected_magnet = self.normalize_magnet_url(best_magnet.get("magnet"))
            if selected_magnet and self.db.get_active_queue_entry(movie_id=movie_id, magnet=selected_magnet):
                self.db.update_jable_hot_status(movie_id, sort_by, "submitted")
                self.logger.info("Jable hot item %s already has active queue entry", movie_id)
                return

            permission = self.evaluate_download_permission(
                movie_id,
                selected_magnet,
                f"{item['title']} ({best_magnet['resolution']})",
                f"jable_hot_{sort_by}",
                automated=True,
            )
            if not permission["allowed"]:
                if permission["reasons"] in (
                    ["auto_download_disabled"],
                    ["automation_mode_not_auto_approve"],
                ):
                    self.db.upsert_automation_candidate(
                        "subscription_download",
                        movie_id,
                        item["title"],
                        movie_id,
                        item["title"],
                        "jable_hot",
                        "review_download",
                        "jable_hot_match_requires_approval",
                        0.7,
                        {
                            "magnet": selected_magnet,
                            "source_url": f"jable_hot_{sort_by}",
                            "matched_rules": ["jable_hot", f"rank:{sort_by}"],
                        },
                    )
                    self.db.update_jable_hot_status(movie_id, sort_by, "candidate")
                    return
                self.logger.info("Jable hot item %s blocked by download policy: %s", movie_id, permission["reasons"])
                self.db.update_jable_hot_status(movie_id, sort_by, "blocked")
                return

            self.queue_download_if_absent(
                selected_magnet,
                movie_id,
                f"{item['title']} ({best_magnet['resolution']})",
                f"jable_hot_{sort_by}",
            )
            self.db.update_jable_hot_status(movie_id, sort_by, "submitted")
            self.logger.info("Jable hot item %s submitted to download queue", movie_id)
        except Exception as e:
            self.logger.error("Failed to search magnets for %s: %s", movie_id, self.safe_exception_label(e))
            self.db.update_jable_hot_status(movie_id, sort_by, "failed_error")
