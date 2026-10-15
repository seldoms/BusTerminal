from typing import Any, Callable, Dict, Iterable, List, Optional

from subscription_url_utils import (
    internal_actor_subscription_url,
    internal_actress_subscription_url,
    internal_subscription_url_aliases,
    is_valid_internal_subscription_subject,
    jellyfin_actor_subscription_url_from_actress_id,
    jellyfin_person_subscription_url_from_actress_id,
    normalize_internal_subscription_url,
)


NormalizeUrl = Callable[[Any], Optional[str]]


def resolve_actress_subscription_url(
    actress_id: Optional[str],
    page_url: Optional[str] = None,
    source_url: Optional[str] = None,
    *,
    normalize_resolved_url: NormalizeUrl,
) -> Optional[str]:
    page_subscription_url = normalize_resolved_url(page_url)
    if page_subscription_url:
        return page_subscription_url
    source_subscription_url = normalize_resolved_url(source_url)
    if source_subscription_url:
        return source_subscription_url
    internal_url = normalize_internal_subscription_url(actress_id)
    if internal_url:
        return internal_url
    person_url = jellyfin_person_subscription_url_from_actress_id(actress_id)
    if person_url:
        return person_url
    actor_url = jellyfin_actor_subscription_url_from_actress_id(actress_id)
    if actor_url:
        return actor_url
    if actress_id:
        return internal_actress_subscription_url(actress_id)
    return None


def subscription_url_exists(subscriptions: Iterable[Dict[str, Any]], url: Optional[str]) -> bool:
    if not url:
        return False
    return any(sub.get("url") == url for sub in subscriptions)


def iter_actress_subscription_urls(
    actress_id: Optional[str],
    page_url: Optional[str] = None,
    source_url: Optional[str] = None,
    *,
    normalize_resolved_url: NormalizeUrl,
) -> List[str]:
    urls: List[str] = []
    raw_actress_id = str(actress_id or "").strip()
    for value in (
        resolve_actress_subscription_url(
            actress_id,
            page_url,
            source_url,
            normalize_resolved_url=normalize_resolved_url,
        ),
        resolve_actress_subscription_url(actress_id, normalize_resolved_url=normalize_resolved_url),
        internal_actress_subscription_url(actress_id),
        internal_actor_subscription_url(actress_id),
        source_url,
    ):
        value = normalize_resolved_url(value)
        if value and value not in urls:
            urls.append(value)

    lower_actress_id = raw_actress_id.lower()
    if lower_actress_id.startswith("jellyfin:person:"):
        person_id = raw_actress_id.split(":", 2)[2].strip()
        if is_valid_internal_subscription_subject(person_id):
            urls.append(f"jf_person:{person_id}")
    elif lower_actress_id.startswith("jellyfin:actor:"):
        actor_id = raw_actress_id.split(":", 2)[2].strip()
        if is_valid_internal_subscription_subject(actor_id):
            urls.append(f"jf_actor:{actor_id}")
    elif raw_actress_id.startswith("jf_person:"):
        person_id = raw_actress_id.split(":", 1)[1].strip()
        if is_valid_internal_subscription_subject(person_id):
            urls.append(f"jellyfin:person:{person_id}")
    elif raw_actress_id.startswith("jf_actor:"):
        actor_id = raw_actress_id.split(":", 1)[1].strip()
        if is_valid_internal_subscription_subject(actor_id):
            urls.append(f"jellyfin:actor:{actor_id}")
    return list(dict.fromkeys(urls))


def get_actress_subscription_url_candidates(
    actress_id: Optional[str],
    page_url: Optional[str] = None,
    source_url: Optional[str] = None,
    subscription_url: Optional[str] = None,
    *,
    normalize_resolved_url: NormalizeUrl,
) -> List[str]:
    urls = iter_actress_subscription_urls(
        actress_id,
        page_url,
        source_url,
        normalize_resolved_url=normalize_resolved_url,
    )
    explicit_url = normalize_resolved_url(subscription_url)
    if explicit_url:
        urls.append(explicit_url)
    for value in list(urls):
        urls.extend(internal_subscription_url_aliases(value))
    return list(dict.fromkeys(urls))


def get_subscription_delete_url_candidates(
    raw_url: Any,
    *,
    normalize_subscription_source_url: NormalizeUrl,
) -> List[str]:
    urls = []
    raw = str(raw_url or "").strip()
    safe_url = normalize_subscription_source_url(raw)
    for value in (raw, safe_url):
        if not value:
            continue
        if value not in urls:
            urls.append(value)
        for alias in internal_subscription_url_aliases(value):
            if alias not in urls:
                urls.append(alias)
    return urls


def subscription_visible_for_actress(
    subscriptions: Iterable[Dict[str, Any]],
    actress_id: Optional[str],
    page_url: Optional[str] = None,
    source_url: Optional[str] = None,
    subscription_url: Optional[str] = None,
    *,
    normalize_resolved_url: NormalizeUrl,
) -> bool:
    candidate_urls = set(
        get_actress_subscription_url_candidates(
            actress_id,
            page_url,
            source_url,
            subscription_url,
            normalize_resolved_url=normalize_resolved_url,
        )
    )
    if not candidate_urls:
        return False
    return any(sub.get("url") in candidate_urls for sub in subscriptions)
