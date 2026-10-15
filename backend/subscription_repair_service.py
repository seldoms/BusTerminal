from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional

from person_name_utils import is_plausible_person_name as default_is_plausible_person_name


class SubscriptionRepairService:
    def __init__(
        self,
        *,
        db: Any,
        resolve_actress_subscription_url: Callable[..., Optional[str]],
        subscription_url_exists: Callable[[Optional[str]], bool],
        subscription_visible_for_actress: Callable[..., bool],
        get_actress_subscription_url_candidates: Callable[..., list],
        internal_actress_subscription_url: Callable[[Optional[str]], Optional[str]],
        internal_actor_subscription_url: Callable[[Optional[str]], Optional[str]],
        normalize_subscription_display_name: Callable[[Optional[str], str], str],
        is_plausible_person_name: Callable[[Any], bool] = default_is_plausible_person_name,
    ):
        self.db = db
        self.resolve_actress_subscription_url = resolve_actress_subscription_url
        self.subscription_url_exists = subscription_url_exists
        self.subscription_visible_for_actress = subscription_visible_for_actress
        self.get_actress_subscription_url_candidates = get_actress_subscription_url_candidates
        self.internal_actress_subscription_url = internal_actress_subscription_url
        self.internal_actor_subscription_url = internal_actor_subscription_url
        self.normalize_subscription_display_name = normalize_subscription_display_name
        self.is_plausible_person_name = is_plausible_person_name

    def follow_and_subscribe_actress(
        self,
        actress: Dict[str, Any],
        source: str,
        movie_id: Optional[str] = None,
        source_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        actress_id = actress.get("id")
        page_url = actress.get("page_url")
        if not actress_id:
            return {"followed": False, "subscribed": False, "reason": "missing_actress_id"}

        subscription_url = self.resolve_actress_subscription_url(actress_id, page_url, source_url)
        if not subscription_url:
            return {"followed": False, "subscribed": False, "reason": "missing_subscription_url"}

        subscription_preexisting = self.subscription_url_exists(subscription_url)
        subscribed = self.db.add_subscription(
            url=subscription_url,
            name=self.normalize_subscription_display_name(actress.get("name"), actress_id),
            category_type="star",
            start_date=(datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"),
        )
        subscription_visible = self.subscription_visible_for_actress(
            actress_id,
            page_url,
            source_url,
            subscription_url=subscription_url,
        )
        subscription_urls = self.get_actress_subscription_url_candidates(
            actress_id,
            page_url,
            source_url,
            subscription_url,
        )
        if not subscribed or not subscription_visible:
            return {
                "followed": False,
                "subscribed": False,
                "reason": "subscription_save_failed" if not subscribed else "subscription_not_visible",
                "subscription_url": subscription_url,
                "subscription_urls": subscription_urls,
                "subscription_visible": subscription_visible,
            }

        followed = self.db.toggle_follow_actress(actress_id, force_state=True)
        self.db.record_automation_event(
            None,
            "jellyfin_favorite_actor_auto_subscribed",
            actor="system",
            reason=source,
            payload={
                "movie_id": movie_id,
                "actress_id": actress_id,
                "page_url": page_url,
                "source_url": source_url,
                "subscription_url": subscription_url,
                "followed": followed,
                "subscribed": subscribed,
            },
        )
        return {
            "actress_id": actress_id,
            "name": actress.get("name"),
            "followed": followed,
            "subscribed": subscribed,
            "subscription_created": subscribed and not subscription_preexisting,
            "subscription_visible": subscription_visible,
            "page_url": page_url,
            "source_url": source_url,
            "subscription_url": subscription_url,
            "subscription_urls": subscription_urls,
        }

    def ensure_followed_actress_subscriptions(self) -> Dict[str, int]:
        followed_actresses = self.db.get_followed_actresses()
        subscriptions = self.db.get_subscriptions()
        existing_by_url = {sub.get("url"): sub for sub in subscriptions if sub.get("url")}
        repaired = 0
        normalized = 0
        unchanged = 0
        failed = 0
        skipped = 0
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        for actress in followed_actresses:
            actress_id = actress.get("id")
            if not actress_id:
                skipped += 1
                continue

            page_url = actress.get("page_url")
            candidate_urls = [
                url
                for url in (
                    self.resolve_actress_subscription_url(actress_id, page_url),
                    self.resolve_actress_subscription_url(actress_id),
                    self.internal_actress_subscription_url(actress_id),
                    self.internal_actor_subscription_url(actress_id),
                )
                if url
            ]
            if not candidate_urls:
                skipped += 1
                continue

            clean_name = self.normalize_subscription_display_name(actress.get("name"), actress_id)
            if not clean_name:
                skipped += 1
                continue
            # 「収录时间」这类 Jellyfin 字段标签一旦被关注过，每次订阅页加载都会
            # 被这里重新变成订阅。名字不像人就不补。
            if not self.is_plausible_person_name(clean_name):
                skipped += 1
                continue

            existing_match = next((url for url in candidate_urls if url in existing_by_url), None)
            target_url = existing_match or candidate_urls[0]
            # This runs on every subscription page load, so a write here costs one
            # committed upsert per followed actress. Only write when the row is
            # actually missing or stale.
            current = existing_by_url.get(target_url)
            if (
                current
                and current.get("name") == clean_name
                and (current.get("category_type") or "") == "star"
            ):
                unchanged += 1
                continue

            if self.db.add_subscription(
                url=target_url,
                name=clean_name,
                category_type="star",
                start_date=start_date,
            ):
                if existing_match:
                    normalized += 1
                else:
                    repaired += 1
                existing_by_url[target_url] = {"name": clean_name, "category_type": "star"}
            else:
                failed += 1

        return {
            "repaired": repaired,
            "normalized": normalized,
            "unchanged": unchanged,
            "failed": failed,
            "skipped": skipped,
            "total_followed": len(followed_actresses),
        }

    def normalize_legacy_subscription_names(self) -> Dict[str, int]:
        normalized = 0
        failed = 0
        for sub in self.db.get_subscriptions():
            url = sub.get("url")
            name = sub.get("name")
            clean_name = self.normalize_subscription_display_name(name)
            if not url or not clean_name or clean_name == name:
                continue
            if self.db.add_subscription(
                url=url,
                name=clean_name,
                category_type=sub.get("category_type") or "category",
                start_date=sub.get("start_date") or (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"),
                exclude_tags=sub.get("exclude_tags") or "",
            ):
                normalized += 1
            else:
                failed += 1
        return {"normalized": normalized, "failed": failed}
