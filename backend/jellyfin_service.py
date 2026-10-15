import asyncio
import json
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
import requests

from number_parsing import extract_number
from person_name_utils import (
    is_plausible_person_name,
    match_person_name,
    match_person_name_loose,
)


JELLYFIN_BEHAVIOR_BUCKETS = {
    "watched_not_favorite",
    "watched_and_favorite",
    "favorite_unwatched",
    "strong_preference",
}


class JellyfinService:
    def __init__(
        self,
        *,
        db: Any,
        jellyfin_client: Any,
        scraper: Any,
        logger: Any,
        deletion_tasks: Any,
        deletion_tasks_lock: Any,
        as_bool: Callable[[Any, bool], bool],
        parse_int_value: Callable[..., int],
        safe_number: Callable[..., float],
        safe_exception_label: Callable[[Exception], str],
        display_value_for_log: Callable[[Any], str],
        normalize_scraper_url: Callable[[Any], Optional[str]],
        normalize_external_subscription_url: Callable[[Any], Optional[str]],
        normalize_subscription_display_name: Callable[[Optional[str], str], str],
        follow_and_subscribe_actress: Callable[..., Dict[str, Any]],
        jellyfin_person_subscription_url_from_actress_id: Callable[[Optional[str]], Optional[str]],
        resolve_actress_subscription_url: Callable[[Optional[str], Optional[str], Optional[str]], Optional[str]],
        subscription_visible_for_actress: Callable[..., bool],
        get_actress_subscription_url_candidates: Callable[..., List[str]],
        get_subscription_delete_url_candidates: Callable[[Any], List[str]],
        subscription_url_exists: Callable[[Optional[str]], bool],
        ensure_followed_actress_subscriptions: Callable[[], Dict[str, int]],
        normalize_internal_subscription_url: Callable[[Any], Optional[str]],
        normalize_item_id: Callable[[Any], Optional[str]],
        run_in_threadpool: Callable[..., Any],
        actor_subscription_handler: Optional[Callable[..., Any]] = None,
    ):
        self.db = db
        self.jellyfin_client = jellyfin_client
        self.scraper = scraper
        self.logger = logger
        self.deletion_tasks = deletion_tasks
        self.deletion_tasks_lock = deletion_tasks_lock
        self.as_bool = as_bool
        self.parse_int_value = parse_int_value
        self.safe_number = safe_number
        self.safe_exception_label = safe_exception_label
        self.display_value_for_log = display_value_for_log
        self.normalize_scraper_url = normalize_scraper_url
        self.normalize_external_subscription_url = normalize_external_subscription_url
        self.normalize_subscription_display_name = normalize_subscription_display_name
        self.follow_and_subscribe_actress = follow_and_subscribe_actress
        self.jellyfin_person_subscription_url_from_actress_id = jellyfin_person_subscription_url_from_actress_id
        self.resolve_actress_subscription_url = resolve_actress_subscription_url
        self.subscription_visible_for_actress = subscription_visible_for_actress
        self.get_actress_subscription_url_candidates = get_actress_subscription_url_candidates
        self.get_subscription_delete_url_candidates = get_subscription_delete_url_candidates
        self.subscription_url_exists = subscription_url_exists
        self.ensure_followed_actress_subscriptions = ensure_followed_actress_subscriptions
        self.normalize_internal_subscription_url = normalize_internal_subscription_url
        self.normalize_item_id = normalize_item_id
        self.run_in_threadpool = run_in_threadpool
        self.actor_subscription_handler = actor_subscription_handler
        self._availability_lock = threading.Lock()
        self._availability_cache: Dict[str, Any] = {
            "key": None,
            "checked_at": 0.0,
            "server_info": None,
        }
        self._offline_until: float = 0.0

    def is_circuit_open(self) -> bool:
        with self._availability_lock:
            return time.time() < self._offline_until

    def record_failure(self, cooldown_seconds: float = 60.0) -> None:
        with self._availability_lock:
            self._offline_until = time.time() + cooldown_seconds
            self._availability_cache = {"key": None, "checked_at": 0.0, "server_info": None}

    def record_success(self) -> None:
        with self._availability_lock:
            self._offline_until = 0.0

    def clear_availability_cache(self) -> None:
        with self._availability_lock:
            self._availability_cache = {"key": None, "checked_at": 0.0, "server_info": None}

    def probe_server_info(self, force: bool = False) -> Optional[Dict[str, Any]]:
        if self.is_circuit_open():
            return None

        self.jellyfin_client.reload_config()
        if not self.jellyfin_client.host or not self.jellyfin_client.api_key:
            return None

        cache_key = (self.jellyfin_client.host, self.jellyfin_client.api_key)
        with self._availability_lock:
            now = time.monotonic()
            cached = self._availability_cache
            if (
                not force
                and cached.get("key") == cache_key
                and now - float(cached.get("checked_at") or 0) < 5
            ):
                return cached.get("server_info")

        try:
            server_info = self.jellyfin_client.get_server_info()
            if not server_info:
                self.record_failure()
                return None
            self.record_success()
            with self._availability_lock:
                self._availability_cache = {
                    "key": cache_key,
                    "checked_at": now,
                    "server_info": server_info,
                }
            return server_info
        except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError) as e:
            self.logger.warning("Jellyfin probe connection failed: %s", e)
            self.record_failure()
            return None

    async def require_available(self) -> Dict[str, Any]:
        if self.is_circuit_open():
            raise ConnectionError("jellyfin_unavailable")
        server_info = await self.run_in_threadpool(self.probe_server_info)
        if not server_info:
            raise ConnectionError("jellyfin_unavailable")
        return server_info

    def disconnected_stats(self, availability: str) -> Dict[str, Any]:
        return {
            "connected": False,
            "degraded": True,
            "availability": availability,
            "host_configured": bool(self.jellyfin_client.host),
            "server_info": None,
            "libraries": [],
            "total_items": 0,
            "favorites_count": 0,
            "watched_count": 0,
            "total_followed_actresses": 0,
            "total_subscribed_actresses": 0,
            "actresses_with_metadata": 0,
            "last_sync_time": None,
            "cleanup": {
                "enabled": self.as_bool(self.db.get_config("cleanup_enabled"), False),
                "last_run": None,
            },
        }

    async def call_actor_subscription(self, **kwargs: Any) -> Dict[str, Any]:
        if not self.actor_subscription_handler:
            return await self.apply_actor_subscription(**kwargs)

        result = self.actor_subscription_handler(**kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    def fetch_behavior_items(self, library_id: Optional[str], scan_limit: int) -> Dict[str, Any]:
        items: List[Dict[str, Any]] = []
        total_available = 0
        page_size = min(200, max(1, scan_limit))
        offset = 0

        self.jellyfin_client.reload_config()
        while len(items) < scan_limit:
            remaining = scan_limit - len(items)
            result = self.jellyfin_client.get_library_items(
                library_id,
                None,
                None,
                "DateCreated",
                "Descending",
                None,
                min(page_size, remaining),
                offset,
            )
            page_items = result.get("items", [])
            total_available = result.get("total", total_available)
            if not page_items:
                break
            items.extend(page_items)
            offset += len(page_items)
            if offset >= total_available:
                break

        return {
            "items": items,
            "total_available": total_available,
            "scanned": len(items),
            "is_truncated": total_available > len(items),
        }

    def matches_behavior_bucket(self, movie: Dict[str, Any], bucket: str, strong_preference_min_plays: int) -> bool:
        watched = self.as_bool(movie.get("is_watched"), False)
        favorite = self.as_bool(movie.get("is_favorite"), False)
        play_count = self.parse_int_value(movie.get("play_count"), 0, minimum=0)

        if bucket == "watched_not_favorite":
            return watched and not favorite
        if bucket == "watched_and_favorite":
            return watched and favorite
        if bucket == "favorite_unwatched":
            return favorite and not watched
        if bucket == "strong_preference":
            return favorite and play_count >= strong_preference_min_plays
        return False

    def behavior_movie_view(self, movie: Dict[str, Any], bucket: str, strong_preference_min_plays: int) -> Dict[str, Any]:
        reason_map = {
            "watched_not_favorite": "watched_without_favorite",
            "watched_and_favorite": "watched_and_favorite",
            "favorite_unwatched": "favorite_before_watch",
            "strong_preference": f"favorite_with_play_count_gte_{strong_preference_min_plays}",
        }
        return {
            "jellyfin_id": movie.get("jellyfin_id"),
            "movie_id": movie.get("id"),
            "title": movie.get("title"),
            "cover": movie.get("cover"),
            "year": movie.get("year"),
            "rating": movie.get("rating"),
            "added_at": movie.get("added_at"),
            "release_date": movie.get("release_date"),
            "is_watched": self.as_bool(movie.get("is_watched"), False),
            "is_favorite": self.as_bool(movie.get("is_favorite"), False),
            "play_count": self.parse_int_value(movie.get("play_count"), 0, minimum=0),
            "last_played": movie.get("last_played"),
            "bucket": bucket,
            "reason": reason_map.get(bucket, bucket),
            "cleanup_priority": bucket == "watched_not_favorite",
        }

    def decode_automation_candidate(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(candidate)
        if data.get("payload") and isinstance(data["payload"], str):
            try:
                data["payload"] = json.loads(data["payload"])
            except Exception:
                pass
        if data.get("candidate_type") == "favorite_actor" and data.get("subject_id"):
            try:
                actress = self.db.get_actress(data["subject_id"])
            except Exception:
                actress = None
            if actress:
                data["subject_profile"] = {
                    "id": actress.get("id"),
                    "name": actress.get("name"),
                    "avatar": actress.get("avatar"),
                    "page_url": actress.get("page_url"),
                    "cup": actress.get("cup"),
                    "birth_date": actress.get("birth_date"),
                }
        return data

    def extract_movie_id_from_name(self, name: str) -> Optional[str]:
        """从 Jellyfin 条目名解析番号; 未命中返回 None, 不把标题原文冒充番号."""
        text = str(name or "").strip()
        if not text:
            return None
        return extract_number(text)

    def iter_actor_people(self, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        people = item.get("People") or []
        actors = []
        seen_names = set()
        for person in people:
            if not isinstance(person, dict):
                continue
            person_type = str(person.get("Type") or "").lower()
            if person_type and person_type not in {"actor", "person"}:
                continue
            name = str(person.get("Name") or "").strip()
            if not name or name in seen_names:
                continue
            # Jellyfin 的 NFO 里会把「収录时间」「未知演员」这类字段标签塞进 People，
            # 不过滤就会被自动关注当成演员建订阅。
            if not is_plausible_person_name(name):
                continue
            seen_names.add(name)
            actors.append(person)
        return actors

    def get_actor_people_for_item(self, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        actors = self.iter_actor_people(item)
        if actors:
            return actors

        item_id = str(item.get("Id") or item.get("id") or "").strip()
        if not item_id:
            return []
        detail = self.jellyfin_client.get_item_detail(item_id, fields="ProviderIds,People")
        if not isinstance(detail, dict):
            return []
        merged = {**item, **detail}
        return self.iter_actor_people(merged)

    def resolve_actress_for_person(
        self,
        name: Optional[str],
        movie_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """按名字找演员；精确匹配不到时在该片自己的演员名单里按宽松写法再找一次。

        生产实测: Jellyfin 用简体写法（铃木真夕），库里存日文汉字写法（鈴木真夕），
        精确匹配必然失败 —— 结果是漏订阅 + 用兜底 id 再建一条重复订阅。
        """
        name = str(name or "").strip()
        if not name:
            return None
        actress = self.db.get_actress_by_name(name)
        if actress or not movie_id:
            return actress

        try:
            rows = self.db.get_movie_actresses(movie_id) or []
        except Exception as exc:
            self.logger.warning("Failed to read movie actresses for %s: %s", movie_id, self.safe_exception_label(exc))
            return None
        cast_names = [row.get("actress_name") for row in rows if row.get("actress_name")]
        # 候选唯一时才放宽: 多人候选里放宽会把「鈴木真夕」和「黒木真夕」并成一个人。
        matched_name = match_person_name_loose(name, cast_names)
        if not matched_name:
            return None
        for row in rows:
            if row.get("actress_name") != matched_name:
                continue
            actress_id = str(row.get("actress_id") or "").strip()
            if not actress_id:
                continue
            actress = self.db.get_actress(actress_id)
            if actress:
                return actress
            return {"id": actress_id, "name": matched_name}
        return None

    def sync_people_to_subscriptions(self, item: Dict[str, Any], movie_id: Optional[str]) -> Dict[str, Any]:
        result = {"matched": 0, "created": 0, "subscribed": 0, "subscription_created": 0, "missing_page_url": 0, "unmatched": []}
        for person in self.get_actor_people_for_item(item):
            name = str(person.get("Name") or "").strip()
            actress = self.resolve_actress_for_person(name, movie_id)
            if not actress:
                synced = self.sync_person_to_subscription(person)
                if synced.get("matched"):
                    result["created"] += 1
                    if synced.get("subscribed"):
                        result["subscribed"] += 1
                    if synced.get("subscription_created"):
                        result["subscription_created"] += 1
                else:
                    result["unmatched"].append(name)
                continue

            jellyfin_person_id = str(person.get("Id") or person.get("id") or "").strip()
            source_url = f"jellyfin:person:{jellyfin_person_id}" if jellyfin_person_id else None
            applied = self.follow_and_subscribe_actress(
                actress,
                source="jellyfin_favorite_people_sync",
                movie_id=movie_id,
                source_url=source_url,
            )
            result["matched"] += 1
            if applied.get("subscribed"):
                result["subscribed"] += 1
            else:
                result["missing_page_url"] += 1
            if applied.get("subscription_created"):
                result["subscription_created"] += 1
        return result

    def sync_person_to_subscription(self, person: Dict[str, Any], movie_id: Optional[str] = None) -> Dict[str, Any]:
        name = str(person.get("Name") or person.get("name") or "").strip()
        jellyfin_id = str(person.get("Id") or person.get("id") or "").strip()
        if not name and not jellyfin_id:
            return {"matched": False, "subscribed": False, "reason": "missing_person_identity"}
        if name and not is_plausible_person_name(name):
            return {"matched": False, "subscribed": False, "reason": "implausible_person_name"}

        fallback_id = f"jf_person:{jellyfin_id}" if jellyfin_id else f"jf_actor:{name}"
        source_url = f"jellyfin:person:{jellyfin_id}" if jellyfin_id else None
        actress = self.db.get_actress(fallback_id) or self.resolve_actress_for_person(name, movie_id)
        if not actress:
            self.db.save_actress({
                "id": fallback_id,
                "name": name or jellyfin_id,
                "is_followed": False,
                "source": "jellyfin",
            })
            actress = self.db.get_actress(fallback_id)

        if not actress:
            return {"matched": False, "subscribed": False, "reason": "actress_save_failed"}

        result = self.follow_and_subscribe_actress(
            actress,
            source="jellyfin_favorite_person_sync",
            source_url=source_url,
        )
        return {
            **result,
            "matched": True,
            "jellyfin_person_id": jellyfin_id,
        }

    def create_person_candidate(
        self,
        person: Dict[str, Any],
        *,
        movie_id: Optional[str] = None,
        source_title: Optional[str] = None,
        source: str,
    ) -> Optional[Dict[str, Any]]:
        name = str(person.get("Name") or person.get("name") or "").strip()
        jellyfin_id = str(person.get("Id") or person.get("id") or "").strip()
        if name and not is_plausible_person_name(name):
            return None
        actress = self.resolve_actress_for_person(name, movie_id)
        subject_id = str((actress or {}).get("id") or "").strip()
        if not subject_id:
            subject_id = f"jf_person:{jellyfin_id}" if jellyfin_id else (f"jf_actor:{name}" if name else "")
        if not subject_id:
            return None
        source_url = f"jellyfin:person:{jellyfin_id}" if jellyfin_id else None
        return self.db.upsert_automation_candidate(
            "favorite_actor",
            subject_id,
            (actress or {}).get("name") or name or subject_id,
            movie_id,
            source_title or name or subject_id,
            source,
            "review_follow_and_subscribe",
            "Jellyfin favorite contains this actor",
            0.8,
            {
                "jellyfin_person_id": jellyfin_id,
                "source_url": source_url,
                "movie_id": movie_id,
            },
        )

    def create_people_candidates(
        self,
        item: Dict[str, Any],
        movie_id: Optional[str],
        source: str,
    ) -> Dict[str, Any]:
        result = {"candidates": 0, "unmatched": []}
        for person in self.get_actor_people_for_item(item):
            candidate = self.create_person_candidate(
                person,
                movie_id=movie_id,
                source_title=item.get("Name") or movie_id,
                source=source,
            )
            if candidate:
                result["candidates"] += 1
            else:
                result["unmatched"].append(str(person.get("Name") or ""))
        return result

    def get_automation_policy(self) -> Dict[str, Any]:
        legacy_enabled = self.as_bool(self.db.get_config("jellyfin_auto_approve_candidates"), False)
        configured_mode = str(self.db.get_config("automation_mode") or "").strip().lower()
        mode = configured_mode if configured_mode in {"off", "suggest", "auto_approve_low_risk"} else (
            "auto_approve_low_risk" if legacy_enabled else "suggest"
        )
        legacy_action_default = legacy_enabled and not configured_mode
        return {
            "automation_mode": mode,
            "auto_follow_enabled": self.as_bool(
                self.db.get_config("auto_follow_enabled"), legacy_action_default
            ),
            "auto_subscribe_enabled": self.as_bool(
                self.db.get_config("auto_subscribe_enabled"), legacy_action_default
            ),
        }

    def sync_favorite_people_to_subscriptions(self) -> Dict[str, Any]:
        report = {"synced": 0, "subscribed": 0, "created": 0, "candidates": 0, "skipped": 0, "error": None}
        self.jellyfin_client.reload_config()
        if not self.jellyfin_client.host or not self.jellyfin_client.api_key:
            report["skipped"] = 1
            report["error"] = "jellyfin_not_configured"
            return report

        try:
            favorite_people = self.jellyfin_client.get_favorite_people()
            if self.get_automation_policy()["automation_mode"] == "off":
                report["skipped"] = len(favorite_people)
                return report
            for person in favorite_people:
                candidate = self.create_person_candidate(
                    person,
                    source="jellyfin_favorite_person_sync",
                )
                if candidate:
                    report["synced"] += 1
                    report["candidates"] += 1
                else:
                    report["skipped"] += 1
        except Exception as exc:
            error_label = self.safe_exception_label(exc)
            self.logger.warning("Failed to sync Jellyfin favorite people into subscriptions: %s", error_label)
            report["error"] = error_label
        return report

    def sync_favorite_movie_people_to_subscriptions(self) -> Dict[str, Any]:
        report = {
            "movies": 0,
            "matched": 0,
            "subscribed": 0,
            "created": 0,
            "candidates": 0,
            "unmatched": 0,
            "skipped": 0,
            "error": None,
        }
        self.jellyfin_client.reload_config()
        if not self.jellyfin_client.host or not self.jellyfin_client.api_key:
            report["skipped"] = 1
            report["error"] = "jellyfin_not_configured"
            return report

        try:
            favorite_movies = self.jellyfin_client.get_favorite_movies()
            report["movies"] = len(favorite_movies)
            policy = self.get_automation_policy()
            if policy["automation_mode"] == "off":
                report["skipped"] = len(favorite_movies)
                return report
            for item in favorite_movies:
                movie_id = self.extract_movie_id_from_name(item.get("Name", ""))
                if policy["automation_mode"] == "auto_approve_low_risk" and (
                    policy["auto_follow_enabled"] or policy["auto_subscribe_enabled"]
                ):
                    people_result = self.sync_people_to_subscriptions(item, movie_id)
                    report["matched"] += people_result["matched"]
                    report["created"] += people_result["created"]
                    report["subscribed"] += people_result["subscribed"]
                    report["unmatched"] += len(people_result["unmatched"])
                else:
                    people_result = self.create_people_candidates(
                        item,
                        movie_id,
                        "jellyfin_favorite_movie_people_sync",
                    )
                    report["matched"] += people_result["candidates"]
                    report["candidates"] += people_result["candidates"]
                    report["unmatched"] += len(people_result["unmatched"])
        except Exception as exc:
            error_label = self.safe_exception_label(exc)
            self.logger.warning("Failed to sync Jellyfin favorite movie people into subscriptions: %s", error_label)
            report["error"] = error_label
        return report

    def apply_approved_automation_candidate(
        self,
        candidate: Dict[str, Any],
        *,
        follow: bool = True,
        subscribe: bool = True,
    ) -> Dict[str, Any]:
        candidate = self.decode_automation_candidate(candidate)
        if candidate.get("candidate_type") != "favorite_actor":
            return {"applied": False, "message": "Unsupported candidate type"}

        payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
        subject_name = candidate.get("subject_name") or payload.get("actor_name") or payload.get("name")
        # 历史候选里存着「収录时间」「未知演员」这类字段标签，自动批准时会把它们
        # 重新变成订阅 —— 删掉订阅也没用，下一轮同步又会建回来。
        if subject_name and not is_plausible_person_name(subject_name):
            return {"applied": False, "message": "implausible_person_name", "subject_name": subject_name}
        actress_id = candidate.get("subject_id") or payload.get("actor_id") or payload.get("actress_id")
        if not actress_id and subject_name:
            actress_id = f"jf_actor:{subject_name}"
        if not actress_id:
            return {"applied": False, "message": "missing_actress_identity"}
        if not follow and not subscribe:
            return {"applied": False, "message": "automation_actions_disabled"}

        actress = self.db.get_actress(actress_id)
        page_url = self.normalize_external_subscription_url((actress or {}).get("page_url") or payload.get("actor_page_url"))
        source_url = payload.get("source_url") or payload.get("url")
        if not source_url:
            source_url = self.jellyfin_person_subscription_url_from_actress_id(actress_id)
        subscription_url = self.resolve_actress_subscription_url(actress_id, page_url, source_url)
        subject_name = subject_name or actress_id
        subscription_name = self.normalize_subscription_display_name(subject_name, actress_id)

        if not actress:
            saved = self.db.save_actress({
                "id": actress_id,
                "name": subscription_name,
                "page_url": page_url,
                "is_followed": False,
                "source": "jellyfin",
            })
            if not saved:
                return {"applied": False, "message": "actress_save_failed", "actress_id": actress_id}

        if subscribe and not subscription_url:
            return {"applied": False, "message": "missing_subscription_url", "actress_id": actress_id}

        subscribed = False
        subscription_visible = False
        subscription_urls = self.get_actress_subscription_url_candidates(
            actress_id,
            page_url,
            source_url,
            subscription_url,
        )
        if subscribe:
            subscribed = self.db.add_subscription(
                url=subscription_url,
                name=subscription_name,
                category_type="star",
                start_date=(datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"),
            )
            subscription_visible = self.subscription_visible_for_actress(
                actress_id,
                page_url,
                source_url,
                subscription_url=subscription_url,
            )
            if not subscribed or not subscription_visible:
                return {
                    "applied": False,
                    "message": "subscription_save_failed" if not subscribed else "subscription_not_visible",
                    "actress_id": actress_id,
                    "page_url": page_url,
                    "subscription_url": subscription_url,
                    "subscription_urls": subscription_urls,
                    "subscription_visible": subscription_visible,
                }

        followed = self.as_bool((self.db.get_actress(actress_id) or {}).get("is_followed"), False)
        if follow:
            followed = self.db.toggle_follow_actress(actress_id, force_state=True)

        self.db.record_automation_event(
            candidate.get("id"),
            "candidate_applied",
            actor="system",
            reason="Approved favorite_actor candidate applied",
            payload={
                "actress_id": actress_id,
                "page_url": page_url,
                "source_url": source_url,
                "subscription_url": subscription_url,
                "followed": followed,
                "subscribed": subscribed,
                "follow_enabled": follow,
                "subscribe_enabled": subscribe,
            },
        )
        return {
            "applied": True,
            "actress_id": actress_id,
            "followed": followed,
            "subscribed": subscribed,
            "page_url": page_url,
            "source_url": source_url,
            "subscription_url": subscription_url,
            "subscription_urls": subscription_urls,
            "subscription_visible": subscription_visible,
        }

    def is_auto_approve_candidates_enabled(self) -> bool:
        policy = self.get_automation_policy()
        return policy["automation_mode"] == "auto_approve_low_risk" and (
            policy["auto_follow_enabled"] or policy["auto_subscribe_enabled"]
        )

    def approve_pending_favorite_actor_candidates(
        self,
        actor: str = "system",
        reason: str = "Bulk approve favorite actor candidates",
        limit: int = 500,
        follow: bool = True,
        subscribe: bool = True,
    ) -> Dict[str, Any]:
        rows = self.db.get_automation_candidates("candidate", "favorite_actor", limit, 0)
        report = {
            "scanned": len(rows),
            "approved": 0,
            "followed": 0,
            "subscribed": 0,
            "subscription_urls": [],
            "subscription_url_groups": [],
            "failed": 0,
            "discarded": 0,
            "errors": [],
        }
        for candidate in rows:
            try:
                result = self.apply_approved_automation_candidate(
                    candidate,
                    follow=follow,
                    subscribe=subscribe,
                )
                if not result.get("applied"):
                    if result.get("message") == "implausible_person_name":
                        # 名字不像人的历史候选（Jellyfin 字段标签）直接作废，
                        # 否则每轮同步都会重新把它变成订阅。
                        try:
                            self.db.update_automation_candidate_status(
                                candidate["id"], "rejected", actor, reason
                            )
                            report["discarded"] += 1
                        except Exception as exc:
                            self.logger.warning(
                                "Failed to discard candidate %s: %s",
                                candidate.get("id"),
                                self.safe_exception_label(exc),
                            )
                        continue
                    report["failed"] += 1
                    report["errors"].append({
                        "candidate_id": candidate.get("id"),
                        "error": result.get("message") or "candidate_apply_failed",
                    })
                    continue

                self.db.update_automation_candidate_status(candidate["id"], "approved", actor, reason)
                report["approved"] += 1
                if result.get("followed"):
                    report["followed"] += 1
                if result.get("subscribed"):
                    report["subscribed"] += 1
                if result.get("subscription_url"):
                    report["subscription_urls"].append(result["subscription_url"])
                if result.get("subscription_urls"):
                    report["subscription_url_groups"].append(result["subscription_urls"])
            except Exception as exc:
                error_label = self.safe_exception_label(exc)
                report["failed"] += 1
                report["errors"].append({
                    "candidate_id": candidate.get("id"),
                    "subject_id": candidate.get("subject_id"),
                    "error": error_label,
                })
                self.logger.error("Failed to approve automation candidate %s: %s", candidate.get("id"), error_label)

        return report

    def sync_status_report(
        self,
        auto_approve_candidates: Optional[bool] = None,
        approval_actor: str = "system",
        approval_reason: str = "Jellyfin sync auto approval",
    ) -> Dict[str, Any]:
        self.jellyfin_client.reload_config()
        if not self.jellyfin_client.host or not self.jellyfin_client.api_key:
            raise ValueError("Jellyfin is not configured")

        report = {
            "candidates": 0,
            "followed": 0,
            "auto_followed": 0,
            "auto_subscribed": 0,
            "auto_created": 0,
            "favorite_people": 0,
            "favorite_people_subscribed": 0,
            "favorite_people_created": 0,
            "unmatched_people": 0,
            "watched": 0,
            "auto_approved": 0,
            "approval": None,
            "errors": [],
        }
        automation_policy = self.get_automation_policy()
        automation_enabled = automation_policy["automation_mode"] != "off"
        auto_apply_enabled = automation_policy["automation_mode"] == "auto_approve_low_risk" and (
            automation_policy["auto_follow_enabled"] or automation_policy["auto_subscribe_enabled"]
        )

        try:
            fav_items = self.jellyfin_client.get_favorite_movies()
            self.logger.info("Sync: found %s Jellyfin favorite movies", len(fav_items))

            for item in fav_items:
                name = item.get("Name", "")
                movie_id = self.extract_movie_id_from_name(name)

                if auto_apply_enabled:
                    people_result = self.sync_people_to_subscriptions(item, movie_id)
                    report["favorite_people_subscribed"] += people_result["subscribed"]
                    report["favorite_people_created"] += people_result["created"]
                    report["unmatched_people"] += len(people_result["unmatched"])
                elif automation_enabled:
                    people_result = self.create_people_candidates(
                        item,
                        movie_id,
                        "jellyfin_favorite_movie_people_sync",
                    )
                    report["candidates"] += people_result["candidates"]
                    report["unmatched_people"] += len(people_result["unmatched"])

                if not movie_id:
                    continue

                try:
                    detail_url = self.normalize_scraper_url(f"/{movie_id}")
                    if not detail_url:
                        raise ValueError("invalid_movie_detail_url")
                    detail = self.scraper.get_movie_detail(detail_url)
                    if auto_apply_enabled and detail and detail.get("stars"):
                        for star in detail["stars"]:
                            if not star.get("id") and not star.get("name"):
                                continue
                            applied = self.sync_person_to_subscription(
                                {
                                    "Id": star.get("id"),
                                    "Name": star.get("name"),
                                },
                                movie_id=movie_id,
                            )
                            if applied.get("matched"):
                                report["favorite_people_created"] += int(applied.get("subscription_created", False))
                                report["favorite_people_subscribed"] += int(applied.get("subscribed", False))
                            else:
                                report["unmatched_people"] += 1
                    elif automation_enabled and detail and detail.get("stars"):
                        for star in detail["stars"]:
                            if star.get("id"):
                                self.db.upsert_automation_candidate(
                                    "favorite_actor",
                                    star["id"],
                                    star.get("name") or star["id"],
                                    movie_id,
                                    detail.get("title") or name,
                                    "jellyfin_favorite_sync",
                                    "review_follow_and_subscribe",
                                    "Jellyfin favorite sync contains this actor",
                                    0.75,
                                    {"movie_id": movie_id, "star": star},
                                )
                                report["candidates"] += 1
                except Exception as exc:
                    self.logger.warning(
                        "Jellyfin favorite detail sync skipped for %s: %s",
                        self.display_value_for_log(movie_id),
                        self.safe_exception_label(exc),
                    )
        except Exception as exc:
            error_label = self.safe_exception_label(exc)
            self.logger.error("Failed to sync Jellyfin favorites: %s", error_label)
            report["errors"].append(f"Favorites: {error_label}")

        try:
            favorite_people = self.jellyfin_client.get_favorite_people()
            self.logger.info("Sync: found %s Jellyfin favorite people", len(favorite_people))

            if automation_enabled:
                for person in favorite_people:
                    candidate = self.create_person_candidate(
                        person,
                        source="jellyfin_favorite_person_sync",
                    )
                    if candidate:
                        report["favorite_people"] += 1
                        report["candidates"] += 1
        except Exception as exc:
            error_label = self.safe_exception_label(exc)
            self.logger.error("Failed to sync Jellyfin favorite people: %s", error_label)
            report["errors"].append(f"Favorite people: {error_label}")

        try:
            watched_items = self.jellyfin_client.get_watched_movies()
            self.logger.info("Sync: found %s Jellyfin watched movies", len(watched_items))

            for item in watched_items:
                name = item.get("Name", "")
                movie_id = self.extract_movie_id_from_name(name)
                if movie_id:
                    self.db.mark_movie_watched(movie_id)
                    report["watched"] += 1
        except Exception as exc:
            error_label = self.safe_exception_label(exc)
            self.logger.error("Failed to sync Jellyfin watched movies: %s", error_label)
            report["errors"].append(f"Watched: {error_label}")

        if auto_approve_candidates is None:
            auto_approve_candidates = automation_policy["automation_mode"] == "auto_approve_low_risk"

        follow_enabled = automation_policy["auto_follow_enabled"]
        subscribe_enabled = automation_policy["auto_subscribe_enabled"]
        if auto_approve_candidates and (follow_enabled or subscribe_enabled):
            approval = self.approve_pending_favorite_actor_candidates(
                actor=approval_actor,
                reason=approval_reason,
                follow=follow_enabled,
                subscribe=subscribe_enabled,
            )
            report["approval"] = approval
            report["auto_approved"] = approval["approved"]
            report["auto_followed"] = approval["followed"]
            report["auto_subscribed"] = approval["subscribed"]

        self.db.set_config("jellyfin_last_sync_time", time.time())
        return report

    def is_favorite_event(self, payload: Any) -> bool:
        event_type = str(getattr(payload, "NotificationType", None) or "").lower()
        user_data = getattr(payload, "UserData", None) or {}
        return (
            event_type in {"itemfavorite", "itemfavoriteadded", "itemfavoriteremoved"}
            or "IsFavorite" in user_data
        )

    def favorite_state(self, payload: Any) -> bool:
        user_data = getattr(payload, "UserData", None) or {}
        if "IsFavorite" in user_data:
            return self.as_bool(user_data.get("IsFavorite"), False)
        event_type = str(getattr(payload, "NotificationType", None) or "").lower()
        return event_type != "itemfavoriteremoved"

    def is_watched_event(self, payload: Any) -> bool:
        event_type = str(getattr(payload, "NotificationType", None) or "").lower()
        return (
            getattr(payload, "PlayedToCompletion", None) is True
            or event_type in {"playbackstop", "playbackstopped"}
        )

    async def apply_actor_subscription(
        self,
        *,
        name: str,
        actress_id: Optional[str] = None,
        page_url: Optional[str] = None,
        source_url: Optional[str] = None,
        is_subscribed: bool = True,
        sync_jellyfin_person: bool = False,
    ) -> Dict[str, Any]:
        name = str(name or "").strip()
        actress_id = str(actress_id or "").strip() or None
        page_url = str(page_url or "").strip() or None
        source_url = str(source_url or "").strip() or None
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        actress = None
        if actress_id:
            actress = await self.run_in_threadpool(self.db.get_actress, actress_id)
        if not actress and name:
            actress = await self.run_in_threadpool(self.db.get_actress_by_name, name)

        if not actress and is_subscribed and name:
            normalized_source_url = self.normalize_internal_subscription_url(source_url)
            if not actress_id and str(normalized_source_url or "").startswith("jellyfin:person:"):
                person_id = str(normalized_source_url).split(":", 2)[2].strip()
                if person_id:
                    actress_id = f"jf_person:{person_id}"
            fallback_id = actress_id or f"jf_actor:{name}"
            safe_profile_url = self.normalize_external_subscription_url(page_url) or self.normalize_external_subscription_url(source_url)
            created = await self.run_in_threadpool(
                self.db.save_actress,
                {
                    "id": fallback_id,
                    "name": name,
                    "page_url": safe_profile_url,
                    "is_followed": False,
                    "source": "jellyfin",
                },
            )
            if created:
                actress = await self.run_in_threadpool(self.db.get_actress, fallback_id)
                actress_id = fallback_id

        if not actress and not name:
            return {
                "success": False,
                "message": "missing_actress_identity",
                "is_followed": False,
                "subscribed": False,
            }

        actor_name_for_jellyfin = self.normalize_subscription_display_name(
            actress["name"] if actress else name,
            actress_id or name,
        )
        actress_id = (actress or {}).get("id") or actress_id
        resolved_page_url = self.normalize_external_subscription_url(page_url) or self.normalize_external_subscription_url((actress or {}).get("page_url"))
        target_sub_url = self.resolve_actress_subscription_url(actress_id, resolved_page_url, source_url)
        target_sub_urls = self.get_actress_subscription_url_candidates(
            actress_id,
            resolved_page_url,
            source_url,
            target_sub_url,
        )

        subscribed = False
        subscription_visible = False
        subscription_preexisting = False
        if is_subscribed:
            if not target_sub_url:
                return {
                    "success": False,
                    "message": "missing_subscription_url",
                    "is_followed": False,
                    "subscribed": False,
                    "actress_id": actress_id,
                }
            subscription_preexisting = await self.run_in_threadpool(self.subscription_url_exists, target_sub_url)
            subscribed = await self.run_in_threadpool(
                self.db.add_subscription,
                url=target_sub_url,
                name=self.normalize_subscription_display_name(actor_name_for_jellyfin, actress_id or name),
                category_type="star",
                start_date=start_date,
            )
            if not subscribed:
                return {
                    "success": False,
                    "message": "subscription_save_failed",
                    "is_followed": False,
                    "subscribed": False,
                    "actress_id": actress_id,
                    "subscription_url": target_sub_url,
                    "subscription_urls": target_sub_urls,
                }
            subscription_visible = await self.run_in_threadpool(
                self.subscription_visible_for_actress,
                actress_id,
                resolved_page_url,
                source_url,
                target_sub_url,
            )
            if not subscription_visible:
                return {
                    "success": False,
                    "message": "subscription_not_visible",
                    "is_followed": False,
                    "subscribed": subscribed,
                    "actress_id": actress_id,
                    "page_url": resolved_page_url,
                    "source_url": source_url,
                    "subscription_url": target_sub_url,
                    "subscription_urls": target_sub_urls,
                    "subscription_visible": False,
                }
        elif target_sub_url:
            for item in target_sub_urls:
                for target in self.get_subscription_delete_url_candidates(item):
                    await self.run_in_threadpool(self.db.delete_subscription, target)

        followed = False
        if actress_id:
            followed = await self.run_in_threadpool(
                self.db.toggle_follow_actress,
                actress_id,
                force_state=is_subscribed,
            )
            if is_subscribed and resolved_page_url and not (actress or {}).get("page_url"):
                await self.run_in_threadpool(
                    self.db.save_actress,
                    {
                        "id": actress_id,
                        "name": actor_name_for_jellyfin,
                        "page_url": resolved_page_url,
                        "is_followed": True,
                        "source": (actress or {}).get("source") or "jellyfin",
                    },
                )
        elif name:
            conn = await self.run_in_threadpool(self.db.get_connection)
            try:
                cursor = conn.cursor()
                val = 1 if is_subscribed else 0
                if self.db.is_postgres:
                    cursor.execute("UPDATE actresses SET is_followed = %s WHERE name = %s", (is_subscribed, name))
                else:
                    cursor.execute("UPDATE actresses SET is_followed = ? WHERE name = ?", (val, name))
                conn.commit()
                followed = is_subscribed
            finally:
                await self.run_in_threadpool(self.db._release_connection, conn)

        if sync_jellyfin_person and actor_name_for_jellyfin:
            j_actor = await self.run_in_threadpool(self.jellyfin_client.get_item_by_name, actor_name_for_jellyfin, ["Person"])
            if j_actor:
                await self.run_in_threadpool(self.jellyfin_client.toggle_favorite_item, j_actor["Id"], is_subscribed)

        return {
            "success": True,
            "is_followed": followed,
            "is_subscribed": followed,
            "subscribed": subscribed if is_subscribed else False,
            "subscription_created": bool(is_subscribed and subscribed and not subscription_preexisting),
            "actress_id": actress_id,
            "page_url": resolved_page_url,
            "source_url": source_url,
            "subscription_url": target_sub_url,
            "subscription_urls": target_sub_urls,
            "subscription_visible": subscription_visible,
        }

    async def handle_webhook(self, payload: Any) -> Dict[str, Any]:
        movie_id = self.extract_movie_id_from_name(getattr(payload, "Name", None) or "")
        item_type = str(getattr(payload, "ItemType", None) or "").lower()
        automation_policy = self.get_automation_policy()

        if self.is_favorite_event(payload) and item_type in {"person", "actor"}:
            item_id = self.normalize_item_id(getattr(payload, "ItemId", None))
            if not self.favorite_state(payload):
                return {"success": True, "action": "favorite_removed", "candidates": []}
            if automation_policy["automation_mode"] == "off":
                return {"success": True, "action": "automation_disabled", "candidates": []}
            if automation_policy["automation_mode"] == "auto_approve_low_risk" and (
                automation_policy["auto_follow_enabled"] or automation_policy["auto_subscribe_enabled"]
            ):
                application = await self.run_in_threadpool(
                    self.sync_person_to_subscription,
                    {"Id": item_id, "Name": getattr(payload, "Name", None) or ""},
                )
                candidates = []
            else:
                candidate = await self.run_in_threadpool(
                    self.create_person_candidate,
                    {"Id": item_id, "Name": getattr(payload, "Name", None) or ""},
                    source="jellyfin_favorite_person_webhook",
                )
                if not candidate:
                    return {"success": False, "message": "missing_person_identity"}
                candidates = [candidate]
                application = None
            return {
                "success": True,
                "action": "favorite_actor_synced" if not candidates else "created_automation_candidates",
                "candidates": candidates,
                "application": application,
            }

        if not movie_id:
            return {"success": False, "message": "Missing movie id"}

        if self.is_watched_event(payload):
            await self.run_in_threadpool(self.db.mark_movie_watched, movie_id)
            return {"success": True, "action": "mark_watched", "movie_id": movie_id}

        if self.is_favorite_event(payload):
            if not self.favorite_state(payload):
                await self.run_in_threadpool(
                    self.db.record_lifecycle_event,
                    movie_id,
                    "jellyfin_favorite_status_updated",
                    title=getattr(payload, "Name", None),
                    jellyfin_status="not_favorite",
                    source="jellyfin_webhook",
                    payload={"candidates": 0},
                )
                return {
                    "success": True,
                    "action": "favorite_removed",
                    "movie_id": movie_id,
                    "candidates": [],
                }

            if automation_policy["automation_mode"] == "off":
                return {
                    "success": True,
                    "action": "automation_disabled",
                    "movie_id": movie_id,
                    "candidates": [],
                }

            detail = await self.run_in_threadpool(self.scraper.get_movie_detail, f"/{movie_id}")
            applications = []
            candidates = []
            for star in (detail or {}).get("stars", []):
                if not star.get("id") and not star.get("name"):
                    continue
                if automation_policy["automation_mode"] == "auto_approve_low_risk" and (
                    automation_policy["auto_follow_enabled"] or automation_policy["auto_subscribe_enabled"]
                ):
                    application = await self.run_in_threadpool(
                        self.sync_person_to_subscription,
                        {"Id": star.get("id"), "Name": star.get("name")},
                    )
                    applications.append(application)
                else:
                    candidate = await self.run_in_threadpool(
                        self.db.upsert_automation_candidate,
                        "favorite_actor",
                        star.get("id") or f"jf_actor:{star.get('name')}",
                        star.get("name") or star.get("id"),
                        movie_id,
                        (detail or {}).get("title") or getattr(payload, "Name", None) or movie_id,
                        "jellyfin_favorite",
                        "review_follow_and_subscribe",
                        "Jellyfin favorite contains this actor",
                        0.85,
                        {"movie_id": movie_id, "star": star},
                    )
                    candidates.append(candidate)

            await self.run_in_threadpool(
                self.db.record_lifecycle_event,
                movie_id,
                "jellyfin_favorite_status_updated",
                title=(detail or {}).get("title") or getattr(payload, "Name", None),
                jellyfin_status="favorite",
                source="jellyfin_webhook",
                payload={"applications": len(applications)},
            )
            return {
                "success": True,
                "action": "favorite_movie_synced" if applications else "created_automation_candidates",
                "movie_id": movie_id,
                "candidates": candidates,
                "applications": applications,
            }

        return {"success": True, "action": "ignored", "movie_id": movie_id}

    async def favorite_actor_sync(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = payload or {}
        name = str(raw.get("name", "")).strip()
        actress_id = str(raw.get("actress_id", "")).strip() or str(raw.get("actor_id", "")).strip() or None
        page_url = str(raw.get("page_url", "")).strip() or str(raw.get("actor_page_url", "")).strip() or None
        source_url = str(raw.get("source_url", "")).strip() or str(raw.get("url", "")).strip() or None
        is_favorite = self.as_bool(raw.get("favorite", True), True)

        result = await self.call_actor_subscription(
            name=name,
            actress_id=actress_id,
            page_url=page_url,
            source_url=source_url,
            is_subscribed=is_favorite,
            sync_jellyfin_person=True,
        )
        if result.get("success"):
            repair = await self.run_in_threadpool(self.ensure_followed_actress_subscriptions)
            result["subscription_repair"] = repair
            subscription_urls = await self.run_in_threadpool(
                self.get_actress_subscription_url_candidates,
                result.get("actress_id"),
                result.get("page_url") or page_url,
                result.get("source_url") or source_url,
                result.get("subscription_url"),
            )
            result["subscription_urls"] = result.get("subscription_urls") or subscription_urls
            result["subscription_visible"] = await self.run_in_threadpool(
                self.subscription_visible_for_actress,
                result.get("actress_id"),
                result.get("page_url") or page_url,
                result.get("source_url") or source_url,
                result.get("subscription_url"),
            )
        return result

    async def get_libraries(self) -> Dict[str, Any]:
        await self.require_available()
        libraries = await self.run_in_threadpool(self.jellyfin_client.get_libraries)
        preferred_name = self.db.get_config("jellyfin_library_name") or "\u9ec4\u7247"
        preferred_library_id = None
        for library in libraries:
            is_preferred = library.get("name") == preferred_name or library.get("name") == "\u9ec4\u7247"
            library["is_preferred"] = is_preferred
            if is_preferred and not preferred_library_id:
                preferred_library_id = library.get("id")
        return {"data": libraries, "preferred_library_id": preferred_library_id}

    async def get_stats(self) -> Dict[str, Any]:
        if self.is_circuit_open():
            return self.disconnected_stats("offline")
        server_info = await self.run_in_threadpool(self.probe_server_info, True)
        if not self.jellyfin_client.host or not self.jellyfin_client.api_key:
            return self.disconnected_stats("not_configured")
        if not server_info:
            return self.disconnected_stats("offline")

        raw_libraries = await self.run_in_threadpool(self.jellyfin_client.get_libraries)
        libraries = []
        total_items = 0

        async def fetch_library_count(lib: Dict[str, Any]) -> int:
            try:
                count_result = await self.run_in_threadpool(
                    self.jellyfin_client.get_library_items,
                    lib.get("id"),
                    None,
                    None,
                    "DateCreated",
                    "Descending",
                    None,
                    1,
                    0,
                )
                return self.parse_int_value(count_result.get("total"), 0, minimum=0)
            except Exception as count_error:
                self.logger.warning(
                    "Jellyfin library count failed for %s: %s",
                    self.display_value_for_log(lib.get("name")),
                    self.safe_exception_label(count_error),
                )
                return 0

        counts = await asyncio.gather(*(fetch_library_count(lib) for lib in raw_libraries))
        for library, item_count in zip(raw_libraries, counts):
            total_items += item_count
            libraries.append({**library, "item_count": item_count})

        favorites = await self.run_in_threadpool(
            self.jellyfin_client.get_library_items,
            None,
            None,
            True,
            "DateCreated",
            "Descending",
            None,
            1,
            0,
        )
        watched = await self.run_in_threadpool(
            self.jellyfin_client.get_library_items,
            None,
            True,
            None,
            "DateCreated",
            "Descending",
            None,
            1,
            0,
        )
        followed_actresses = await self.run_in_threadpool(self.db.get_followed_actresses)
        actresses_with_metadata = sum(
            1
            for actress in followed_actresses
            if actress.get("avatar") or actress.get("bio") or actress.get("wiki_url")
        )
        last_sync_time = (
            self.db.get_config("jellyfin_last_sync_time")
            or self.db.get_config("last_jellyfin_sync_time")
            or self.db.get_config("last_jellyfin_sync")
        )

        return {
            "connected": bool(server_info or libraries),
            "availability": "online",
            "host_configured": bool(self.jellyfin_client.host),
            "server_info": {
                "server_name": (server_info or {}).get("ServerName") or (server_info or {}).get("Name") or "Jellyfin",
                "version": (server_info or {}).get("Version") or "unknown",
            } if server_info else None,
            "libraries": libraries,
            "total_items": total_items,
            "favorites_count": self.parse_int_value(favorites.get("total"), 0, minimum=0),
            "watched_count": self.parse_int_value(watched.get("total"), 0, minimum=0),
            "total_followed_actresses": len(followed_actresses),
            "total_subscribed_actresses": len(followed_actresses),
            "actresses_with_metadata": actresses_with_metadata,
            "last_sync_time": self.safe_number(last_sync_time, None),
            "cleanup": {
                "enabled": self.as_bool(self.db.get_config("cleanup_enabled"), False),
                "last_run": self.db.get_config("cleanup_last_run"),
            },
        }

    async def get_library(
        self,
        library_id: Optional[str],
        watched: Optional[bool],
        liked: Optional[bool],
        sort_by: str,
        sort_order: str,
        query: Optional[str],
        limit: int,
        offset: int,
    ) -> Dict[str, Any]:
        await self.require_available()
        result = await self.run_in_threadpool(
            self.jellyfin_client.get_library_items,
            library_id,
            watched,
            liked,
            sort_by,
            sort_order,
            query,
            limit,
            offset,
        )
        return {
            "data": result["items"],
            "total": result["total"],
            "degraded": bool(result.get("degraded", False)),
            "message": result.get("message"),
        }

    async def get_behavior_insights(
        self,
        library_id: Optional[str],
        scan_limit: int,
        sample_limit: int,
        strong_preference_min_plays: int,
    ) -> Dict[str, Any]:
        await self.require_available()
        snapshot = await self.run_in_threadpool(self.fetch_behavior_items, library_id, scan_limit)
        items = snapshot["items"]
        buckets = {bucket: {"count": 0, "sample": []} for bucket in JELLYFIN_BEHAVIOR_BUCKETS}

        for movie in items:
            for bucket in JELLYFIN_BEHAVIOR_BUCKETS:
                if self.matches_behavior_bucket(movie, bucket, strong_preference_min_plays):
                    buckets[bucket]["count"] += 1
                    if len(buckets[bucket]["sample"]) < sample_limit:
                        buckets[bucket]["sample"].append(
                            self.behavior_movie_view(movie, bucket, strong_preference_min_plays)
                        )

        return {
            "total_available": snapshot["total_available"],
            "scanned": snapshot["scanned"],
            "is_truncated": snapshot["is_truncated"],
            "strong_preference_min_plays": strong_preference_min_plays,
            "buckets": buckets,
        }

    async def get_behavior_movies(
        self,
        bucket: str,
        library_id: Optional[str],
        limit: int,
        offset: int,
        scan_limit: int,
        strong_preference_min_plays: int,
    ) -> Dict[str, Any]:
        await self.require_available()
        snapshot = await self.run_in_threadpool(self.fetch_behavior_items, library_id, scan_limit)
        matched = [
            self.behavior_movie_view(movie, bucket, strong_preference_min_plays)
            for movie in snapshot["items"]
            if self.matches_behavior_bucket(movie, bucket, strong_preference_min_plays)
        ]
        return {
            "data": matched[offset:offset + limit],
            "total": len(matched),
            "bucket": bucket,
            "scan": {
                "total_available": snapshot["total_available"],
                "scanned": snapshot["scanned"],
                "is_truncated": snapshot["is_truncated"],
            },
        }

    async def delete_items(self, item_ids: List[str], reason: Optional[str] = None) -> Dict[str, Any]:
        self.jellyfin_client.reload_config()
        result = await self.run_in_threadpool(self.jellyfin_client.delete_items, item_ids)
        result["reason"] = reason
        return result

    def create_delete_task(self, item_ids: List[str], reason: Optional[str] = None) -> str:
        task_id = str(uuid.uuid4())
        with self.deletion_tasks_lock:
            self.deletion_tasks[task_id] = {
                "status": "pending",
                "total": len(item_ids),
                "completed": 0,
                "failed": 0,
                "progress": 0,
                "results": [],
                "reason": reason,
                "created_at": time.time(),
                "started_at": None,
                "completed_at": None,
            }
        return task_id

    async def delete_items_background(self, task_id: str, item_ids: List[str]) -> None:
        try:
            with self.deletion_tasks_lock:
                task = self.deletion_tasks[task_id]
                task["status"] = "running"
                task["started_at"] = time.time()

            self.jellyfin_client.reload_config()

            for idx, item_id in enumerate(item_ids):
                with self.deletion_tasks_lock:
                    if self.deletion_tasks[task_id]["status"] == "cancelled":
                        self.logger.info("Delete task %s cancelled", self.display_value_for_log(task_id))
                        return

                try:
                    success = await self.run_in_threadpool(self.jellyfin_client.delete_item, item_id)

                    with self.deletion_tasks_lock:
                        task = self.deletion_tasks[task_id]
                        task["results"].append({
                            "id": item_id,
                            "success": success,
                            "index": idx + 1,
                            "timestamp": time.time(),
                        })
                        if success:
                            task["completed"] += 1
                        else:
                            task["failed"] += 1
                        task["progress"] = round((idx + 1) / len(item_ids) * 100, 2)

                    self.logger.debug(
                        "Delete task %s: %s/%s - %s - %s",
                        self.display_value_for_log(task_id),
                        idx + 1,
                        len(item_ids),
                        self.display_value_for_log(item_id),
                        "success" if success else "failed",
                    )

                except Exception as e:
                    error_label = self.safe_exception_label(e)
                    with self.deletion_tasks_lock:
                        task = self.deletion_tasks[task_id]
                        task["failed"] += 1
                        task["results"].append({
                            "id": item_id,
                            "success": False,
                            "error": error_label,
                            "index": idx + 1,
                            "timestamp": time.time(),
                        })
                        task["progress"] = round((idx + 1) / len(item_ids) * 100, 2)

                    self.logger.error(
                        "Delete task %s item %s failed: %s",
                        self.display_value_for_log(task_id),
                        self.display_value_for_log(item_id),
                        error_label,
                    )

                await asyncio.sleep(0.2)

            with self.deletion_tasks_lock:
                task = self.deletion_tasks[task_id]
                task["status"] = "completed"
                task["completed_at"] = time.time()
                task["progress"] = 100

            self.logger.info(
                "Delete task %s completed: success=%s, failed=%s",
                self.display_value_for_log(task_id),
                task["completed"],
                task["failed"],
            )

        except Exception as e:
            error_label = self.safe_exception_label(e)
            self.logger.error("Delete task %s failed: %s", self.display_value_for_log(task_id), error_label)
            with self.deletion_tasks_lock:
                task = self.deletion_tasks[task_id]
                task["status"] = "error"
                task["error"] = error_label
                task["completed_at"] = time.time()

    def get_delete_progress(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self.deletion_tasks_lock:
            if task_id not in self.deletion_tasks:
                return None

            task = self.deletion_tasks[task_id]
            elapsed = None
            if task["started_at"]:
                end_time = task["completed_at"] if task["completed_at"] else time.time()
                elapsed = round(end_time - task["started_at"], 2)

            eta = None
            if task["status"] == "running" and task["completed"] > 0 and elapsed:
                avg_time_per_item = elapsed / task["completed"]
                remaining_items = task["total"] - task["completed"] - task["failed"]
                eta = round(avg_time_per_item * remaining_items, 2)

            return {
                "status": task["status"],
                "progress": task["progress"],
                "total": task["total"],
                "completed": task["completed"],
                "failed": task["failed"],
                "elapsed": elapsed,
                "eta": eta,
                "results": task["results"][-20:],
            }

    def cancel_delete_task(self, task_id: str) -> Dict[str, Any]:
        with self.deletion_tasks_lock:
            if task_id not in self.deletion_tasks:
                return {"found": False}

            task = self.deletion_tasks[task_id]
            if task["status"] in ["completed", "error", "cancelled"]:
                return {"found": True, "cancelled": False, "message": f"cannot_cancel_task:{task['status']}"}

            task["status"] = "cancelled"
            task["completed_at"] = time.time()

        self.logger.info("Delete task %s cancelled", self.display_value_for_log(task_id))
        return {"found": True, "cancelled": True, "message": "task_cancelled"}

    def list_delete_tasks(self, limit: int) -> List[Dict[str, Any]]:
        with self.deletion_tasks_lock:
            sorted_tasks = sorted(
                self.deletion_tasks.items(),
                key=lambda x: x[1].get("created_at", 0),
                reverse=True,
            )[:limit]

            return [
                {
                    "task_id": task_id,
                    "status": task["status"],
                    "progress": task["progress"],
                    "total": task["total"],
                    "completed": task["completed"],
                    "failed": task["failed"],
                    "created_at": task["created_at"],
                    "completed_at": task.get("completed_at"),
                }
                for task_id, task in sorted_tasks
            ]

    async def play_item_mpv(self, item_id: str, player: str = "mpv") -> dict:
        """
        通过 Jellyfin Session 远程控制指令唤起 jellyfin-mpv-shim 播放：
        1. GET /Sessions 找到 mpv-shim 注册的会话 SessionId；
        2. POST /Sessions/{sessionId}/Playing 下发 PlayNow 指令；
        3. 未找到 mpv-shim 会话时，返回 HLS 流地址供网页播放降级。
        """
        if player not in {"mpv", "browser"}:
            return {"success": False, "message": "不支持的播放器类型"}

        self.jellyfin_client.reload_config()
        host = (self.jellyfin_client.host or "").rstrip('/')
        api_key = self.jellyfin_client.api_key or ""

        if not host or not api_key:
            return {"success": False, "message": "Jellyfin 未配置，请先在设置页填写主机地址和 API Key"}

        headers = {
            "X-Emby-Token": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # 浏览器播放走后端代理，避免把 API Key 暴露给前端或浏览器历史
        stream_url = f"/api/jellyfin/stream/{item_id}"

        if player == "browser":
            return {
                "success": True,
                "player": "browser",
                "stream_url": stream_url,
                "item_id": item_id,
            }

        def _find_mpv_session():
            """从 /Sessions 中找到 jellyfin-mpv-shim 注册的会话。"""
            resp = requests.get(f"{host}/Sessions", headers=headers, timeout=5)
            if resp.status_code != 200:
                return None
            sessions = resp.json() if isinstance(resp.json(), list) else []
            # mpv-shim 客户端名称通常为 "Jellyfin MPV Shim" 或包含 "mpv"
            for s in sessions:
                client = str(s.get("Client", "")).lower()
                if "mpv" in client or "shim" in client:
                    return s.get("Id")
            return None

        def _send_play_command(session_id: str):
            """向指定 Session 下发 PlayNow 远程播放指令。"""
            resp = requests.post(
                f"{host}/Sessions/{session_id}/Playing",
                headers=headers,
                params={
                    "PlayCommand": "PlayNow",
                    "ItemIds": item_id,
                    "StartPositionTicks": 0,
                },
                timeout=5,
            )
            return resp.status_code in (200, 204)

        try:
            session_id = await self.run_in_threadpool(_find_mpv_session)
            if not session_id:
                return {
                    "success": True,
                    "mpv_launched": False,
                    "message": "未检测到运行中的 jellyfin-mpv-shim，请先启动本地播放器",
                    "stream_url": stream_url,
                    "item_id": item_id,
                }

            ok = await self.run_in_threadpool(_send_play_command, session_id)
            if ok:
                return {
                    "success": True,
                    "mpv_launched": True,
                    "message": "已成功唤起本地 MPV 播放器！",
                    "stream_url": stream_url,
                    "item_id": item_id,
                }
            else:
                return {
                    "success": True,
                    "mpv_launched": False,
                    "message": "发送播放指令失败，请检查本地 jellyfin-mpv-shim",
                    "stream_url": stream_url,
                    "item_id": item_id,
                }

        except Exception as e:
            self.logger.warning("play_item_mpv error: %s", e)
            return {
                "success": True,
                "mpv_launched": False,
                "message": "唤起失败，请检查本地 jellyfin-mpv-shim",
                "stream_url": stream_url,
                "item_id": item_id,
            }

