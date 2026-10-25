from fastapi import FastAPI, HTTPException, Query, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
import logging
import time
import os
import json
import uuid
import re
import hashlib
import hmac
from datetime import date, datetime, timedelta

# Centralized configuration module.
import config

from api_param_utils import (
    TRANSLATION_BATCH_LIMIT,
    TRANSLATION_TEXT_MAX_LENGTH,
    normalize_enum_value,
    normalize_item_id,
    normalize_item_ids,
    normalize_magnet_url,
    normalize_task_id,
    normalize_translation_texts,
)
from number_parsing import extract_number
from common_utils import (
    as_bool,
    display_path_for_log,
    format_bytes,
    format_speed,
    get_float_env,
    is_safe_cache_file,
    redact_log_message,
    safe_error_response,
    safe_exception_label,
    safe_number,
    write_cache_bytes_if_safe,
)
from scraper import scraper
from netflav_scraper import netflav_scraper
import qb_api
import auth_service
from security import hash_password, verify_password
from database import db, config_encryption_enabled, display_value_for_log, parse_int_value, sanitize_lifecycle_payload
import threading
from jable_scraper import jable_scraper
import organizer
import lifecycle_manager
import notifier
import health_deps
from discovery_scraper import discovery_scraper
from scrape_monitor import jellyfin_cleanup
from jellyfin_api import jellyfin_client
from task_registry import task_registry
from external_task_service import ExternalTaskService
from actress_service import ActressService
from actress_base_import import import_actress_base_if_available
from automation_service import AutomationService
from discovery_service import DiscoveryService
import image_cache_adapter
from image_cache_service import image_cache_service
from jellyfin_delete_guard import (
    JELLYFIN_DELETE_CONFIRMATION,
    JELLYFIN_DELETE_MAX_ITEMS,
    validate_jellyfin_delete_request as validate_jellyfin_delete_request_guard,
    validate_jellyfin_item_ids_payload,
)
from jellyfin_curation_service import (
    JELLYFIN_CURATION_CONFIRMATION,
    JELLYFIN_CURATION_MAX_ITEMS,
    JellyfinCurationService,
    normalize_item_ids as normalize_jellyfin_curation_item_ids,
)
from amane_client import (
    AmaneApiError,
    AmaneAuthError,
    AmaneError,
    AmaneNotConfigured,
    AmaneUnavailable,
)
from amane_heal_service import AmaneHealService
from amane_ingest_service import AmaneIngestService
from amane_service import AmaneService
from jellyfin_service import JellyfinService
from maintenance_service import MaintenanceService
from movie_download_service import (
    extract_movie_id_from_download_source,
)
from pending_download_service import (
    PENDING_DOWNLOAD_SCAN_INTERVAL_SECONDS,
    get_pending_next_retry_at,
    select_due_pending_downloads,
    summarize_pending_downloads,
)
from net_policy import internal_session
from network_security_utils import (
    build_system_proxy_config,
    is_blocked_remote_ip,
    is_fake_proxy_ip_address,
    is_http_url,
)
from qb_param_utils import (
    normalize_qb_category,
    normalize_qb_hashes,
    normalize_qb_root_path,
    normalize_qb_save_path as normalize_qb_save_path_for_roots,
)
from qb_service import QBService
from scraper_url_utils import (
    get_scraper_allowed_hosts as get_scraper_allowed_hosts_from_sources,
    normalize_scraper_page_url as normalize_scraper_page_url_for_sources,
    normalize_scraper_url as normalize_scraper_url_for_sources,
    normalize_subscription_source_url as normalize_subscription_source_url_with_external,
    resolve_movie_detail_url_for_download as resolve_movie_detail_url_for_download_with_normalizers,
)
from download_service import DownloadService
from subscription_service import SubscriptionService
from subscription_stats_service import SubscriptionStatsService
import subscription_visibility_utils
from subscription_repair_service import SubscriptionRepairService
from system_config_service import SystemConfigService, sanitize_pasted_config_payload
from onboarding_service import OnboardingService
import onboarding_service
from bi_dashboard_service import BIDashboardService, BI_REFRESH_MODE_KEY, BI_REFRESH_MODES
from bi_snapshot_service import BISnapshotService
from subscription_url_utils import (
    internal_actor_subscription_url,
    internal_actress_subscription_url,
    internal_subscription_url_aliases,
    is_internal_subscription_url,
    jellyfin_person_subscription_url_from_actress_id,
    mojibake_score,
    normalize_internal_subscription_url,
    normalize_legacy_internal_subscription_alias,
    normalize_subscription_display_name,
    repair_mojibake_text,
)

# Values from the centralized configuration module.

COVER_CACHE_DIR = config.COVER_CACHE_DIR
SAFE_REMOTE_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}
SAFE_REMOTE_IMAGE_EXTENSIONS = set(SAFE_REMOTE_IMAGE_TYPES.values())
SAFE_JELLYFIN_IMAGE_TYPES = {"image/jpeg", "image/jpg"}
CACHE_CLEANUP_IMAGE_SUFFIXES = SAFE_REMOTE_IMAGE_EXTENSIONS | {".jpg", ".jpeg"}

# Logging configuration.
logging.basicConfig(level=config.get_log_level())
logger = logging.getLogger(__name__)

# Global stop signal for background workers.
stop_event = threading.Event()

# Dashboard counts include the large movies table. Keep an exact, persistent
# snapshot briefly so opening the dashboard never turns into repeated table
# scans under normal page navigation.
DASHBOARD_METRICS_CACHE_KEY = "dashboard_metrics_snapshot_v1"
DASHBOARD_METRICS_CACHE_TTL = 30  # seconds
dashboard_metrics_refresh_lock = threading.Lock()
dashboard_metrics_refresh_scheduled = False

# In-memory log buffer for the frontend log panel.
from collections import deque
log_buffer = deque(maxlen=200)

# Jellyfin async delete task state.
from collections import defaultdict
import asyncio
deletion_tasks = defaultdict(dict)
deletion_tasks_lock = threading.Lock()
QB_CONTROL_ALL_CONFIRMATION = "CONTROL_ALL_QB_TORRENTS"
CACHE_INVALIDATE_ALL_CONFIRMATION = "CLEAR_ALL_CACHES"
JELLYFIN_WEBHOOK_TOKEN_ENV = "BUS_TERMINAL_JELLYFIN_WEBHOOK_TOKEN"
JELLYFIN_BEHAVIOR_BUCKETS = {
    "watched_not_favorite",
    "watched_and_favorite",
    "favorite_unwatched",
    "strong_preference",
}
JELLYFIN_LIBRARY_SORT_FIELDS = {
    "DateCreated",
    "PremiereDate",
    "SortName",
    "CommunityRating",
    "PlayCount",
}
JELLYFIN_LIBRARY_SORT_ORDERS = {"Ascending", "Descending"}
SUBSCRIPTION_JELLYFIN_SYNC_TIMEOUT = get_float_env("BUS_TERMINAL_SUBSCRIPTION_SYNC_TIMEOUT", 3.0, minimum=0.1)

_jellyfin_service: Optional[JellyfinService] = None
_automation_service: Optional[AutomationService] = None


def get_jellyfin_service() -> JellyfinService:
    global _jellyfin_service
    if _jellyfin_service is None:
        _jellyfin_service = JellyfinService(
            db=db,
            jellyfin_client=jellyfin_client,
            scraper=scraper,
            logger=logger,
            deletion_tasks=deletion_tasks,
            deletion_tasks_lock=deletion_tasks_lock,
            as_bool=as_bool,
            parse_int_value=parse_int_value,
            safe_number=safe_number,
            safe_exception_label=safe_exception_label,
            display_value_for_log=display_value_for_log,
            normalize_scraper_url=normalize_scraper_url,
            normalize_external_subscription_url=normalize_external_subscription_url,
            normalize_subscription_display_name=normalize_subscription_display_name,
            follow_and_subscribe_actress=follow_and_subscribe_actress,
            jellyfin_person_subscription_url_from_actress_id=jellyfin_person_subscription_url_from_actress_id,
            resolve_actress_subscription_url=resolve_actress_subscription_url,
            subscription_visible_for_actress=subscription_visible_for_actress,
            get_actress_subscription_url_candidates=get_actress_subscription_url_candidates,
            get_subscription_delete_url_candidates=get_subscription_delete_url_candidates,
            subscription_url_exists=subscription_url_exists,
            ensure_followed_actress_subscriptions=ensure_followed_actress_subscriptions,
            normalize_internal_subscription_url=normalize_internal_subscription_url,
            normalize_item_id=normalize_item_id,
            run_in_threadpool=run_in_threadpool,
            actor_subscription_handler=lambda **kwargs: apply_jellyfin_actor_subscription(**kwargs),
        )
    return _jellyfin_service


def get_automation_service() -> AutomationService:
    global _automation_service
    if _automation_service is None:
        _automation_service = AutomationService(
            db=db,
            jellyfin_service=get_jellyfin_service(),
            ensure_followed_actress_subscriptions=ensure_followed_actress_subscriptions,
            subscription_visible_for_actress=subscription_visible_for_actress,
            get_actress_subscription_url_candidates=get_actress_subscription_url_candidates,
            normalize_magnet_url=normalize_magnet_url,
            queue_download_if_absent=queue_download_if_absent,
        )
    return _automation_service


def get_jellyfin_webhook_token() -> str:
    return str(os.getenv(JELLYFIN_WEBHOOK_TOKEN_ENV, "") or "").strip()


def validate_jellyfin_webhook_request(request: Optional[Request]) -> None:
    expected_token = get_jellyfin_webhook_token()
    if not expected_token:
        return
    if request is None:
        raise HTTPException(status_code=403, detail="jellyfin_webhook_token_required")

    provided_token = (
        request.headers.get("X-BusTerminal-Webhook-Token")
        or request.headers.get("X-Bus-Terminal-Webhook-Token")
        or request.query_params.get("token")
        or ""
    ).strip()
    if not provided_token or not hmac.compare_digest(provided_token, expected_token):
        raise HTTPException(status_code=403, detail="invalid_jellyfin_webhook_token")

DISCOVERY_CACHE_TTL = config.get_int_env("BUS_TERMINAL_DISCOVERY_CACHE_TTL", 6 * 3600, minimum=1)
DISCOVERY_SOURCES = {"jable", "javbus", "javdb", "airav"}
DISCOVERY_RANK_TYPES = {"daily", "weekly", "monthly"}
INFORMATION_FLOW_PRELOAD_PAGES = config.INFORMATION_FLOW_PRELOAD_PAGES
# 翻译进度快照在 config 表里的键（重统计由后台任务定期写入，接口只读）
TRANSLATION_PROGRESS_SNAPSHOT_KEY = "translation_progress_snapshot"
INFORMATION_FLOW_CATEGORY_TARGETS = (
    ("home", "https://www.javbus.com"),
    ("subtitles", "/genre/sub"),
    ("hd", "/genre/hd"),
)
discovery_refreshing_keys = set()
discovery_refresh_lock = threading.Lock()

_discovery_service: Optional[DiscoveryService] = None


def get_discovery_service() -> DiscoveryService:
    global _discovery_service
    if _discovery_service is None:
        _discovery_service = DiscoveryService(
            db=db,
            scraper=scraper,
            jable_scraper=jable_scraper,
            discovery_scraper=discovery_scraper,
            config=config,
            logger=logger,
            stop_event=stop_event,
            cache_ttl_seconds=DISCOVERY_CACHE_TTL,
            safe_number=safe_number,
            safe_exception_label=safe_exception_label,
            display_value_for_log=display_value_for_log,
            normalize_magnet_url=normalize_magnet_url,
            evaluate_download_permission=evaluate_download_permission,
            queue_download_if_absent=queue_download_if_absent,
            schedule_image_cache_warmup=schedule_image_cache_warmup,
        )
    return _discovery_service

def get_allowed_qb_save_roots() -> List[str]:
    raw_roots = db.get_config("qb_allowed_save_roots") or os.getenv("BUS_TERMINAL_QB_ALLOWED_SAVE_ROOTS", "")
    roots = [config.DELIVERY_FOLDER, db.get_config("delivery_folder")]
    if raw_roots:
        roots.extend(re.split(r"[;,\n]+", str(raw_roots)))

    normalized = []
    for root in roots:
        value = normalize_qb_root_path(root)
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def normalize_qb_save_path(raw_path: Any) -> Optional[str]:
    return normalize_qb_save_path_for_roots(raw_path, get_allowed_qb_save_roots())


def get_qb_delivery_folder() -> Optional[str]:
    return normalize_qb_root_path(db.get_config('delivery_folder') or config.DELIVERY_FOLDER)


def validate_jellyfin_delete_request(payload: Dict, item_count: int) -> Dict[str, Any]:
    enabled = as_bool(db.get_config('jellyfin_delete_enabled'), False)
    result = validate_jellyfin_delete_request_guard(
        payload,
        item_count,
        delete_enabled=enabled,
        confirmation_phrase=JELLYFIN_DELETE_CONFIRMATION,
        max_items=JELLYFIN_DELETE_MAX_ITEMS,
    )
    if not result.get("success"):
        return result

    reason = str(result.get("reason") or "").strip()
    logger.warning("Jellyfin delete authorized: item_count=%s reason=%s", item_count, redact_log_message(reason))
    return {"success": True, "reason": reason}

_jellyfin_curation_service = None


def get_jellyfin_curation_service() -> JellyfinCurationService:
    global _jellyfin_curation_service
    if _jellyfin_curation_service is None:
        _jellyfin_curation_service = JellyfinCurationService()
    return _jellyfin_curation_service


def validate_jellyfin_curation_request(payload: Dict, item_count: int) -> Dict[str, Any]:
    """清洗写入的保护闸：DB 开关 + 确认码 + 单批上限（对齐删除保护的四重校验）。"""
    enabled = as_bool(db.get_config('jellyfin_curation_enabled'), False)
    dry_run = as_bool(payload.get('dry_run'), False)
    service = get_jellyfin_curation_service()
    result = service.validate_execute_request(
        payload.get("item_ids", []),
        confirmation=str(payload.get("confirm") or "").strip(),
        enabled=enabled,
        dry_run=dry_run,
        max_items=JELLYFIN_CURATION_MAX_ITEMS,
    )
    if not result.get("success"):
        return result
    if not dry_run:
        logger.warning(
            "Jellyfin curation authorized: item_count=%s",
            item_count,
        )
    return {"success": True, "item_ids": result["item_ids"], "dry_run": dry_run}

def fetch_jellyfin_behavior_items(library_id: Optional[str], scan_limit: int) -> Dict[str, Any]:
    return get_jellyfin_service().fetch_behavior_items(library_id, scan_limit)

def matches_behavior_bucket(movie: Dict[str, Any], bucket: str, strong_preference_min_plays: int) -> bool:
    return get_jellyfin_service().matches_behavior_bucket(movie, bucket, strong_preference_min_plays)

def behavior_movie_view(movie: Dict[str, Any], bucket: str, strong_preference_min_plays: int) -> Dict[str, Any]:
    return get_jellyfin_service().behavior_movie_view(movie, bucket, strong_preference_min_plays)

def decode_automation_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return get_automation_service().decode_candidate(candidate)

def extract_movie_id_from_name(name: str) -> Optional[str]:
    return get_jellyfin_service().extract_movie_id_from_name(name)

def iter_jellyfin_actor_people(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    return get_jellyfin_service().iter_actor_people(item)

def get_jellyfin_actor_people_for_item(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    return get_jellyfin_service().get_actor_people_for_item(item)

def get_system_proxy_config() -> Optional[Dict[str, str]]:
    return build_system_proxy_config(db.get_config("system_proxy") or config.SYSTEM_PROXY)

def remote_image_url_rejection_reason(image_url: str) -> Optional[str]:
    return image_cache_adapter.remote_image_url_rejection_reason(image_cache_service, COVER_CACHE_DIR, image_url)


def normalize_remote_image_cache_url(image_url: Any) -> Optional[str]:
    return image_cache_adapter.normalize_remote_image_cache_url(image_cache_service, COVER_CACHE_DIR, image_url)


def get_remote_image_cache_keys(image_url: Any) -> List[str]:
    return image_cache_adapter.get_remote_image_cache_keys(image_cache_service, COVER_CACHE_DIR, image_url)


def find_remote_image_cache_file(image_url: Any):
    return image_cache_adapter.find_remote_image_cache_file(image_cache_service, COVER_CACHE_DIR, image_url)


def sync_image_cache_service_root() -> None:
    image_cache_adapter.sync_image_cache_service_root(image_cache_service, COVER_CACHE_DIR)


IMAGE_CACHE_WARMUP_LIMIT = 80
image_cache_warmup_lock = threading.Lock()
image_cache_warming_keys = set()

def normalize_external_subscription_url(raw_url: Any) -> Optional[str]:
    # 订阅必须能定位到具体实体: 站点首页（https://www.javbus.com、https://www.seedmm.cyou/）
    # 会被当成合法 URL 存进订阅表，多个演员共用同一个首页时订阅行会互相覆盖。
    return normalize_scraper_page_url_for_sources(
        raw_url,
        base_url=getattr(scraper, "base_url", ""),
        all_domains=getattr(scraper, "all_domains", []),
    )

def normalize_resolved_subscription_url(raw_url: Any) -> Optional[str]:
    value = str(raw_url or "").strip()
    internal_url = normalize_internal_subscription_url(value)
    if internal_url:
        return internal_url
    legacy_internal_url = normalize_legacy_internal_subscription_alias(value)
    if legacy_internal_url:
        return legacy_internal_url
    return normalize_external_subscription_url(value)

def resolve_actress_subscription_url(
    actress_id: Optional[str],
    page_url: Optional[str] = None,
    source_url: Optional[str] = None,
) -> Optional[str]:
    return subscription_visibility_utils.resolve_actress_subscription_url(
        actress_id,
        page_url,
        source_url,
        normalize_resolved_url=normalize_resolved_subscription_url,
    )

def mojibake_score(text: str) -> int:
    suspicious_chars = "脙脗氓忙莽猫茅盲枚眉冒帽垄拢陇楼娄搂篓漏陋芦卢庐炉掳卤虏鲁麓碌露路赂鹿潞禄录陆戮驴"
    score = sum(1 for char in text if char in suspicious_chars)
    score += sum(2 for char in text if "\u0080" <= char <= "\u009f" or char == "\ufffd")
    return score


def repair_mojibake_text(text: str) -> str:
    original = str(text or "")
    if not original:
        return original
    best_text = original
    best_score = mojibake_score(original)
    for encoding in ("latin1", "cp1252"):
        try:
            candidate = original.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        candidate_score = mojibake_score(candidate)
        has_cjk = bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", candidate))
        if has_cjk and candidate_score < best_score:
            best_text = candidate
            best_score = candidate_score
    return best_text


def normalize_subscription_display_name(name: Optional[str], fallback: str = "") -> str:
    text = repair_mojibake_text(str(name or "")).strip()
    text = re.sub(r"^(?:subscription|follow|subscribed|followed|订阅|关注|關注|璁㈤槄|鍏虫敞|[?�]+)\s*[:：]\s*", "", text).strip()
    for prefix in ("subscription:", "follow:", "subscribed:", "followed:", "订阅:", "關注:", "璁㈤槄:", "???:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text or fallback


from subscription_url_utils import (
    mojibake_score as mojibake_score,
    normalize_subscription_display_name as normalize_subscription_display_name,
    repair_mojibake_text as repair_mojibake_text,
)


def subscription_url_exists(url: Optional[str]) -> bool:
    return subscription_visibility_utils.subscription_url_exists(db.get_subscriptions(), url)

def iter_actress_subscription_urls(
    actress_id: Optional[str],
    page_url: Optional[str] = None,
    source_url: Optional[str] = None,
) -> List[str]:
    return subscription_visibility_utils.iter_actress_subscription_urls(
        actress_id,
        page_url,
        source_url,
        normalize_resolved_url=normalize_resolved_subscription_url,
    )


def get_actress_subscription_url_candidates(
    actress_id: Optional[str],
    page_url: Optional[str] = None,
    source_url: Optional[str] = None,
    subscription_url: Optional[str] = None,
) -> List[str]:
    return subscription_visibility_utils.get_actress_subscription_url_candidates(
        actress_id,
        page_url,
        source_url,
        subscription_url,
        normalize_resolved_url=normalize_resolved_subscription_url,
    )


def get_subscription_delete_url_candidates(raw_url: Any) -> List[str]:
    return subscription_visibility_utils.get_subscription_delete_url_candidates(
        raw_url,
        normalize_subscription_source_url=normalize_subscription_source_url,
    )


def subscription_response_view(subscription: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(subscription)
    row["name"] = normalize_subscription_display_name(row.get("name"))
    row["is_subscribed"] = True
    return row


_subscription_stats_service: Optional[SubscriptionStatsService] = None
_subscription_repair_service: Optional[SubscriptionRepairService] = None


def get_subscription_stats_service() -> SubscriptionStatsService:
    global _subscription_stats_service
    if _subscription_stats_service is None:
        _subscription_stats_service = SubscriptionStatsService(
            db=db,
            internal_subscription_url_aliases=internal_subscription_url_aliases,
            normalize_subscription_display_name=normalize_subscription_display_name,
            normalize_internal_subscription_url=normalize_internal_subscription_url,
            normalize_legacy_internal_subscription_alias=normalize_legacy_internal_subscription_alias,
            safe_number=safe_number,
        )
    return _subscription_stats_service


def get_subscription_repair_service() -> SubscriptionRepairService:
    global _subscription_repair_service
    if _subscription_repair_service is None:
        _subscription_repair_service = SubscriptionRepairService(
            db=db,
            resolve_actress_subscription_url=resolve_actress_subscription_url,
            subscription_url_exists=subscription_url_exists,
            subscription_visible_for_actress=subscription_visible_for_actress,
            get_actress_subscription_url_candidates=get_actress_subscription_url_candidates,
            internal_actress_subscription_url=internal_actress_subscription_url,
            internal_actor_subscription_url=internal_actor_subscription_url,
            normalize_subscription_display_name=normalize_subscription_display_name,
        )
    _subscription_repair_service.resolve_actress_subscription_url = resolve_actress_subscription_url
    _subscription_repair_service.subscription_url_exists = subscription_url_exists
    _subscription_repair_service.subscription_visible_for_actress = subscription_visible_for_actress
    _subscription_repair_service.get_actress_subscription_url_candidates = get_actress_subscription_url_candidates
    _subscription_repair_service.internal_actress_subscription_url = internal_actress_subscription_url
    _subscription_repair_service.internal_actor_subscription_url = internal_actor_subscription_url
    _subscription_repair_service.normalize_subscription_display_name = normalize_subscription_display_name
    return _subscription_repair_service


def get_subscription_stats_keys(source_url: Optional[str]) -> List[str]:
    return get_subscription_stats_service().get_stats_keys(source_url)

def empty_subscription_stats() -> Dict[str, Any]:
    return get_subscription_stats_service().empty_stats()

def get_subscription_actress_cover_terms(subscription: Dict[str, Any]) -> Dict[str, List[str]]:
    return get_subscription_stats_service().get_actress_cover_terms(subscription)

def get_local_library_covers_for_subscription(subscription: Dict[str, Any], limit: int = 15) -> List[Dict[str, Any]]:
    return get_subscription_stats_service().get_local_library_covers_for_subscription(subscription, limit=limit)

def subscription_visible_for_actress(
    actress_id: Optional[str],
    page_url: Optional[str] = None,
    source_url: Optional[str] = None,
    subscription_url: Optional[str] = None,
) -> bool:
    return subscription_visibility_utils.subscription_visible_for_actress(
        db.get_subscriptions(),
        actress_id,
        page_url,
        source_url,
        subscription_url,
        normalize_resolved_url=normalize_resolved_subscription_url,
    )

def follow_and_subscribe_actress(
    actress: Dict[str, Any],
    source: str,
    movie_id: Optional[str] = None,
    source_url: Optional[str] = None,
) -> Dict[str, Any]:
    return get_subscription_repair_service().follow_and_subscribe_actress(
        actress,
        source,
        movie_id=movie_id,
        source_url=source_url,
    )

def ensure_followed_actress_subscriptions() -> Dict[str, int]:
    return get_subscription_repair_service().ensure_followed_actress_subscriptions()


def normalize_legacy_subscription_names() -> Dict[str, int]:
    return get_subscription_repair_service().normalize_legacy_subscription_names()


def sync_jellyfin_people_to_subscriptions(item: Dict[str, Any], movie_id: Optional[str]) -> Dict[str, Any]:
    return get_jellyfin_service().sync_people_to_subscriptions(item, movie_id)

    result = {"matched": 0, "created": 0, "subscribed": 0, "subscription_created": 0, "missing_page_url": 0, "unmatched": []}
    for person in get_jellyfin_actor_people_for_item(item):
        name = str(person.get("Name") or "").strip()
        actress = db.get_actress_by_name(name)
        if not actress:
            synced = sync_jellyfin_person_to_subscription(person)
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
        applied = follow_and_subscribe_actress(
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


def sync_jellyfin_person_to_subscription(person: Dict[str, Any]) -> Dict[str, Any]:
    return get_jellyfin_service().sync_person_to_subscription(person)

    name = str(person.get("Name") or person.get("name") or "").strip()
    jellyfin_id = str(person.get("Id") or person.get("id") or "").strip()
    if not name and not jellyfin_id:
        return {"matched": False, "subscribed": False, "reason": "missing_person_identity"}

    fallback_id = f"jf_person:{jellyfin_id}" if jellyfin_id else f"jf_actor:{name}"
    source_url = f"jellyfin:person:{jellyfin_id}" if jellyfin_id else None
    actress = db.get_actress(fallback_id) or (db.get_actress_by_name(name) if name else None)
    if not actress:
        db.save_actress({
            "id": fallback_id,
            "name": name or jellyfin_id,
            "is_followed": False,
            "source": "jellyfin",
        })
        actress = db.get_actress(fallback_id)

    if not actress:
        return {"matched": False, "subscribed": False, "reason": "actress_save_failed"}

    result = follow_and_subscribe_actress(
        actress,
        source="jellyfin_favorite_person_sync",
        source_url=source_url,
    )
    return {
        **result,
        "matched": True,
        "jellyfin_person_id": jellyfin_id,
    }

def sync_jellyfin_favorite_people_to_subscriptions() -> Dict[str, Any]:
    return get_jellyfin_service().sync_favorite_people_to_subscriptions()

    report = {"synced": 0, "subscribed": 0, "created": 0, "skipped": 0, "error": None}
    jellyfin_client.reload_config()
    if not jellyfin_client.host or not jellyfin_client.api_key:
        report["skipped"] = 1
        report["error"] = "jellyfin_not_configured"
        return report

    try:
        favorite_people = jellyfin_client.get_favorite_people()
        for person in favorite_people:
            result = sync_jellyfin_person_to_subscription(person)
            if result.get("matched"):
                report["synced"] += 1
            else:
                report["skipped"] += 1
            if result.get("subscribed"):
                report["subscribed"] += 1
            if result.get("subscription_created"):
                report["created"] += 1
    except Exception as exc:
        error_label = safe_exception_label(exc)
        logger.warning("Failed to sync Jellyfin favorite people into subscriptions: %s", error_label)
        report["error"] = error_label
    return report


def sync_jellyfin_favorite_movie_people_to_subscriptions() -> Dict[str, Any]:
    return get_jellyfin_service().sync_favorite_movie_people_to_subscriptions()

    report = {
        "movies": 0,
        "matched": 0,
        "subscribed": 0,
        "created": 0,
        "unmatched": 0,
        "skipped": 0,
        "error": None,
    }
    jellyfin_client.reload_config()
    if not jellyfin_client.host or not jellyfin_client.api_key:
        report["skipped"] = 1
        report["error"] = "jellyfin_not_configured"
        return report

    try:
        favorite_movies = jellyfin_client.get_favorite_movies()
        report["movies"] = len(favorite_movies)
        for item in favorite_movies:
            movie_id = extract_movie_id_from_name(item.get("Name", ""))
            people_result = sync_jellyfin_people_to_subscriptions(item, movie_id)
            report["matched"] += people_result["matched"] + people_result.get("created", 0)
            report["subscribed"] += people_result["subscribed"]
            report["created"] += people_result.get("subscription_created", 0)
            report["unmatched"] += len(people_result["unmatched"])
    except Exception as exc:
        error_label = safe_exception_label(exc)
        logger.warning("Failed to sync Jellyfin favorite movie people into subscriptions: %s", error_label)
        report["error"] = error_label
    return report


async def run_subscription_sync_with_timeout(sync_func, label: str) -> Dict[str, Any]:
    try:
        return await asyncio.wait_for(
            run_in_threadpool(sync_func),
            timeout=SUBSCRIPTION_JELLYFIN_SYNC_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("Jellyfin subscription sync timed out: %s", label)
        return {"skipped": 1, "error": "timeout"}
    except Exception as exc:
        error_label = safe_exception_label(exc)
        logger.warning("Jellyfin subscription sync failed: %s %s", label, error_label)
        return {"skipped": 1, "error": error_label}


def apply_approved_automation_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return get_jellyfin_service().apply_approved_automation_candidate(candidate)

def is_jellyfin_auto_approve_candidates_enabled() -> bool:
    return get_automation_service().get_settings()["jellyfin_auto_approve_candidates"]

def approve_pending_favorite_actor_candidates(
    actor: str = "system",
    reason: str = "Bulk approve favorite actor candidates",
    limit: int = 500,
) -> Dict[str, Any]:
    return get_jellyfin_service().approve_pending_favorite_actor_candidates(
        actor=actor,
        reason=reason,
        limit=limit,
    )

def sync_jellyfin_status_report(
    auto_approve_candidates: Optional[bool] = None,
    approval_actor: str = "system",
    approval_reason: str = "Jellyfin sync auto approval",
) -> Dict[str, Any]:
    return get_jellyfin_service().sync_status_report(
        auto_approve_candidates=auto_approve_candidates,
        approval_actor=approval_actor,
        approval_reason=approval_reason,
    )


_external_task_service: Optional[ExternalTaskService] = None


def get_external_task_service() -> ExternalTaskService:
    global _external_task_service
    if _external_task_service is None:
        _external_task_service = ExternalTaskService(
            db=db,
            logger=logger,
            safe_exception_label=safe_exception_label,
        )
    _external_task_service.register_handler(
        "jellyfin_automation_sync",
        lambda _payload: sync_jellyfin_status_report(
            approval_actor="system",
            approval_reason="Scheduled Jellyfin automation sync",
        ),
    )
    return _external_task_service


def enqueue_jellyfin_automation_sync(run_date: Any) -> Dict[str, Any]:
    date_key = run_date.isoformat() if hasattr(run_date, "isoformat") else str(run_date)
    return get_external_task_service().enqueue(
        "jellyfin_automation_sync",
        f"daily:{date_key}",
        payload={},
        source="daily_scheduler",
        max_retries=20,
    )


def process_jellyfin_automation_tasks(limit: int = 5) -> Dict[str, Any]:
    return get_external_task_service().run_due("jellyfin_automation_sync", limit=limit)


class DequeHandler(logging.Handler):
    def emit(self, record):
        # Filter noisy qB connection success logs.
        msg = record.getMessage()
        if "Loaded QB config" in msg or "QB connection successful" in msg:
            return

        log_buffer.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": record.levelname,
            "msg": redact_log_message(msg)
        })

# In-memory log handler for the frontend log panel.
handler = DequeHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logging.getLogger().addHandler(handler)

app = FastAPI(title="BusTerminal API")

from starlette.concurrency import run_in_threadpool


def extract_movie_id_from_text(text: str) -> Optional[str]:
    """Extract a standard movie code from torrent/Jellyfin/display text."""
    if not text:
        return None
    return extract_number(str(text))


def normalize_video_id_path_segment(raw_video_id: Any) -> Optional[str]:
    value = str(raw_video_id or "").strip()
    if not value or len(value) > 64:
        return None
    if value in {".", ".."} or ".." in value:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        return None
    return value.upper()


def start_background_task(
    name: str,
    target,
    *,
    description: str,
    category: str,
    interval: Optional[str] = None,
    args: tuple = (),
) -> threading.Thread:
    task_registry.register(
        name,
        description=description,
        category=category,
        interval=interval,
    )
    thread = threading.Thread(
        target=task_registry.run_task,
        args=(name, target, args),
        name=name,
        daemon=True,
    )
    thread.start()
    return thread

@app.get("/api/jable/hot")
async def get_jable_hot_movies(
    page: int = Query(1, ge=1, description="椤电爜"),
    limit: int = Query(30, ge=10, le=60, description="姣忛〉鏁伴噺"),
    sort_by: str = Query("video_viewed", pattern="^(video_viewed|video_viewed_month|video_viewed_week|video_viewed_today|post_date)$", description="鎺掑簭鏂瑰紡"),
    force_refresh: bool = Query(False, description="鏄惁寮哄埗鍒锋柊"),
    translate: bool = Query(True, description="鏄惁缈昏瘧鏍囬")
):
    """
    鑾峰彇 Jable.tv 鐑棬褰辩墖鍒楄〃銆?
    """
    try:
        movies = await run_in_threadpool(
            get_discovery_service().get_jable_hot_movies,
            page,
            limit,
            sort_by,
            force_refresh,
            translate
        )
        movies, pending_translation_count = await run_in_threadpool(
            scraper.filter_processed_movies, movies
        )
        return {
            "success": True,
            "data": movies,
            "page": page,
            "count": len(movies),
            "pending_translation_count": pending_translation_count,
            "source": "jable"
        }
    except Exception as e:
        logger.error("Jable hot movies fetch failed: %s", safe_exception_label(e))
        raise HTTPException(status_code=500, detail="jable_hot_fetch_failed")

@app.get("/api/jable/search_magnet")
async def search_jable_magnet(
    video_id: str = Query(..., min_length=1, max_length=64, description="Video ID, for example SSNI-647")
):
    safe_video_id = normalize_video_id_path_segment(video_id)
    if not safe_video_id:
        return {"success": False, "message": "invalid_video_id", "magnets": []}

    try:
        detail_url = f"{scraper.base_url}/{safe_video_id}"
        movie_detail = await run_in_threadpool(
            scraper.get_movie_detail,
            detail_url,
            perform_translate=False
        )
        if not movie_detail:
            return {"success": False, "message": f"Movie not found: {safe_video_id}", "magnets": []}
        magnets = movie_detail.get('magnets', [])
        return {
            "success": True,
            "video_id": safe_video_id,
            "title": movie_detail.get('title', ''),
            "magnets": magnets,
            "count": len(magnets)
        }
    except Exception as e:
        logger.error("Jable magnet search failed: %s", safe_exception_label(e))
        return {**safe_error_response("jable_magnet_search_failed", e), "magnets": []}

def get_discovery_cache_key(source: str, page: int, rank_type: str) -> str:
    return get_discovery_service().get_cache_key(source, page, rank_type)


def apply_cached_movie_title_translations(movies: List[Dict]) -> List[Dict]:
    return get_discovery_service().apply_cached_movie_title_translations(movies)


def normalize_discovery_source(raw_source: Any) -> Optional[str]:
    return get_discovery_service().normalize_source(raw_source)


def normalize_discovery_rank_type(raw_rank_type: Any) -> Optional[str]:
    return get_discovery_service().normalize_rank_type(raw_rank_type)


def fetch_discovery_movies(source: str, page: int, rank_type: str, force_refresh: bool = False) -> List[Dict]:
    return get_discovery_service().fetch_movies(source, page, rank_type, force_refresh=force_refresh)


def refresh_discovery_cache(source: str, page: int, rank_type: str, cache_key: Optional[str] = None, force_refresh: bool = True) -> List[Dict]:
    return get_discovery_service().refresh_cache(source, page, rank_type, cache_key=cache_key, force_refresh=force_refresh)


def get_cache_age_seconds(cache_full: Optional[Dict[str, Any]]) -> float:
    return get_discovery_service().get_cache_age_seconds(cache_full)


def schedule_discovery_refresh(source: str, page: int, rank_type: str, cache_key: str):
    return get_discovery_service().schedule_refresh(source, page, rank_type, cache_key)


DEFAULT_SCHEDULE_DISCOVERY_REFRESH = schedule_discovery_refresh


def preload_discovery_cache(max_pages: int = 1, force_refresh: bool = False):
    return get_discovery_service().preload_discovery_cache(max_pages=max_pages, force_refresh=force_refresh)


def preload_movie_feed_cache(max_pages: int = 3, force_refresh: bool = False):
    return get_discovery_service().preload_movie_feed_cache(max_pages=max_pages, force_refresh=force_refresh)


def preload_information_flow_cache(max_pages: int = 3, force_refresh: bool = False):
    return get_discovery_service().preload_information_flow_cache(max_pages=max_pages, force_refresh=force_refresh)


# MARK: - Discovery API

@app.get("/api/discovery/sources")
async def get_discovery_sources():
    """Get available discovery sources."""
    discovery_svc = await run_in_threadpool(get_discovery_service)
    sources = await run_in_threadpool(discovery_svc.get_sources)
    return {"success": True, "data": sources}

@app.get("/api/discovery/movies/raw")
async def get_discovery_movies(
    source: str = Query("javbus", max_length=20, description="Source ID"),
    page: int = Query(1, ge=1, le=100),
    rank_type: str = Query("daily", max_length=20, description="Rank type, such as daily, weekly, or monthly")
):
    """Fetch discovery movies directly from the selected source."""
    try:
        safe_source = normalize_discovery_source(source)
        if not safe_source:
            raise HTTPException(status_code=400, detail="unsupported_discovery_source")
        safe_rank_type = normalize_discovery_rank_type(rank_type)
        if not safe_rank_type:
            raise HTTPException(status_code=400, detail="invalid_rank_type")

        result = await run_in_threadpool(
            get_discovery_service().get_raw_movies,
            safe_source,
            page,
            safe_rank_type,
        )

        return {
            "success": True,
            **result,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Discovery fail [%s]: %s", source, safe_exception_label(e))
        raise HTTPException(status_code=502, detail="discovery_fetch_failed")

@app.get("/api/discovery/movies")
async def get_discovery_movies_cached(
    source: str = Query("javbus", max_length=20, description="discovery source id"),
    page: int = Query(1, ge=1, le=100),
    rank_type: str = Query("daily", max_length=20, description="rank type"),
    force_refresh: bool = Query(False, description="force remote refresh")
):
    try:
        safe_source = normalize_discovery_source(source)
        if not safe_source:
            raise HTTPException(status_code=400, detail="unsupported_discovery_source")
        safe_rank_type = normalize_discovery_rank_type(rank_type)
        if not safe_rank_type:
            raise HTTPException(status_code=400, detail="invalid_rank_type")

        discovery_service = get_discovery_service()
        original_refresh_scheduler = discovery_service.refresh_scheduler
        if schedule_discovery_refresh is not DEFAULT_SCHEDULE_DISCOVERY_REFRESH:
            discovery_service.refresh_scheduler = schedule_discovery_refresh
        try:
            result = await run_in_threadpool(
                discovery_service.get_cached_movies,
                safe_source,
                page,
                safe_rank_type,
                force_refresh,
            )
        finally:
            discovery_service.refresh_scheduler = original_refresh_scheduler

        # 页面只展示处理完的数据：未翻译条目先不下发，并插队翻译，刷新后可见
        if isinstance(result, dict) and isinstance(result.get("data"), list):
            processed, pending_translation_count = await run_in_threadpool(
                scraper.filter_processed_movies, result["data"]
            )
            # 全链路屏蔽词：命中的条目从发现流里摘掉（下载侧另有闸门兜底）
            processed, blocked_count = get_onboarding_service().filter_blocked_movies(processed)
            result = {
                **result,
                "data": processed,
                "pending_translation_count": pending_translation_count,
                "blocked_count": blocked_count,
            }

        return {"success": True, **result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Discovery cached endpoint fail [%s]: %s", source, safe_exception_label(e))
        raise HTTPException(status_code=502, detail="discovery_fetch_failed")


@app.post("/api/discovery/preload")
async def preload_discovery_movies(
    pages: int = Query(1, ge=1, le=30, description="pages to preload")
):
    start_background_task(
        "discovery_preload_manual",
        preload_discovery_cache,
        args=(pages,),
        description="Preload discovery source data into local cache",
        category="crawler",
        interval="manual",
    )
    return {"success": True, "message": "task_started", "pages": pages}

def fetch_dashboard_metrics() -> Dict[str, Any]:
    """Fetch exact local dashboard metrics for the persistent snapshot."""
    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        if db.is_postgres:
            cursor.execute("""
                SELECT
                    COUNT(*) AS movies_count,
                    COUNT(*) FILTER (WHERE is_watched IS TRUE) AS watched_count
                FROM movies
            """)
            movie_counts = cursor.fetchone()
            cursor.execute("""
                SELECT
                    COUNT(*) AS actresses_count,
                    COUNT(*) FILTER (WHERE is_followed IS TRUE) AS followed_actresses_count
                FROM actresses
            """)
            actress_counts = cursor.fetchone()
        else:
            cursor.execute("""
                SELECT
                    COUNT(*) AS movies_count,
                    COALESCE(SUM(CASE WHEN is_watched = 1 THEN 1 ELSE 0 END), 0) AS watched_count
                FROM movies
            """)
            movie_counts = cursor.fetchone()
            cursor.execute("""
                SELECT
                    COUNT(*) AS actresses_count,
                    COALESCE(SUM(CASE WHEN is_followed = 1 THEN 1 ELSE 0 END), 0) AS followed_actresses_count
                FROM actresses
            """)
            actress_counts = cursor.fetchone()

        cursor.execute("SELECT COUNT(*) AS count FROM downloads")
        downloads_count = cursor.fetchone()["count"]
        cursor.execute("SELECT COUNT(*) AS count FROM subscriptions")
        subscriptions_count = cursor.fetchone()["count"]
        queue_stats = db.get_queue_stats()

        if db.is_postgres:
            sql_trend = """
                SELECT TO_TIMESTAMP(downloaded_at)::date AS date, COUNT(*) AS count
                FROM downloads
                WHERE downloaded_at >= extract(epoch from (now() - interval '30 days'))
                GROUP BY date ORDER BY date ASC
            """
        else:
            sql_trend = """
                SELECT DATE(downloaded_at, 'unixepoch') AS date, COUNT(*) AS count
                FROM downloads
                WHERE downloaded_at >= strftime('%s', 'now', '-30 days')
                GROUP BY date ORDER BY date ASC
            """

        cursor.execute(sql_trend)
        trend_rows = cursor.fetchall()
        date_map = {str(row["date"]): row["count"] for row in trend_rows}
        today = datetime.now().date()
        trend = []
        for i in range(29, -1, -1):
            dt = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            trend.append({"date": dt, "count": date_map.get(dt, 0)})

        followed_count = actress_counts["followed_actresses_count"]
        return {
            "movies_count": movie_counts["movies_count"],
            "watched_count": movie_counts["watched_count"],
            "actresses_count": actress_counts["actresses_count"],
            "followed_actresses_count": followed_count,
            "subscribed_actresses_count": followed_count,
            "total_downloads": downloads_count,
            "subscriptions_count": subscriptions_count,
            "queue_stats": queue_stats,
            "download_trend": trend,
        }
    finally:
        db._release_connection(conn)


def refresh_dashboard_metrics_snapshot() -> Dict[str, Any]:
    metrics = fetch_dashboard_metrics()
    db.set_cache(DASHBOARD_METRICS_CACHE_KEY, metrics)
    db.record_cache_event(DASHBOARD_METRICS_CACHE_KEY, "refresh_success")
    return metrics


def schedule_dashboard_metrics_refresh() -> bool:
    global dashboard_metrics_refresh_scheduled
    with dashboard_metrics_refresh_lock:
        if dashboard_metrics_refresh_scheduled:
            db.record_cache_event(DASHBOARD_METRICS_CACHE_KEY, "refresh_deduplicated")
            return False
        dashboard_metrics_refresh_scheduled = True

    def refresh_once():
        global dashboard_metrics_refresh_scheduled
        try:
            refresh_dashboard_metrics_snapshot()
        except Exception as e:
            db.record_cache_event(DASHBOARD_METRICS_CACHE_KEY, "refresh_failure")
            logger.warning("Dashboard metrics background refresh failed: %s", safe_exception_label(e))
        finally:
            with dashboard_metrics_refresh_lock:
                dashboard_metrics_refresh_scheduled = False

    threading.Thread(
        target=refresh_once,
        name="dashboard_metrics_refresh",
        daemon=True,
    ).start()
    return True


def get_dashboard_metrics_snapshot() -> Dict[str, Any]:
    # Dashboard reads are intentionally not cache-metric events: recording a
    # hit would turn every page view into an extra database write.
    cached = db.get_cache_full(DASHBOARD_METRICS_CACHE_KEY, track=False)
    if cached and isinstance(cached.get("value"), dict):
        if get_cache_age_seconds(cached) >= DASHBOARD_METRICS_CACHE_TTL:
            db.record_cache_event(DASHBOARD_METRICS_CACHE_KEY, "stale")
            schedule_dashboard_metrics_refresh()
        return cached["value"]

    # A cold cache should still be exact. Serialize the first calculation so
    # concurrent dashboard requests do not all scan the movies table.
    with dashboard_metrics_refresh_lock:
        cached = db.get_cache_full(DASHBOARD_METRICS_CACHE_KEY, track=False)
        if cached and isinstance(cached.get("value"), dict):
            return cached["value"]
        return refresh_dashboard_metrics_snapshot()


def get_dashboard_status_hints() -> Dict[str, Dict[str, Any]]:
    # The dashboard page fetches live qB and Jellyfin states through dedicated
    # endpoints in parallel. Keep these hints outside the metrics snapshot so
    # configuration changes remain visible immediately.
    return {
        "qb_status": {
            "connected": None,
            "configured": bool(db.get_config("qb_config") or {}),
            "message": "status_available_via_qb_dashboard",
        },
        "jellyfin_status": {
            "connected": None,
            "configured": bool(jellyfin_client.host and jellyfin_client.api_key),
            "message": "status_available_via_jellyfin_stats",
        },
    }


@app.get("/api/stats/dashboard")
async def get_dashboard_stats():
    """Get dashboard statistics without blocking on a full table scan."""
    try:
        from starlette.concurrency import run_in_threadpool

        def build_dashboard_data() -> Dict[str, Any]:
            return {**get_dashboard_metrics_snapshot(), **get_dashboard_status_hints()}

        data = await run_in_threadpool(build_dashboard_data)
        return {"success": True, "data": data}
    except Exception as e:
        logger.error("Stats Dashboard Error: %s", safe_exception_label(e))
        raise HTTPException(status_code=500, detail="dashboard_stats_failed")

@app.get("/api/proxy/image")
async def proxy_remote_image(
    url: str = Query(..., min_length=1),
    w: Optional[int] = Query(None, ge=32, le=1200),
):
    # Proxy remote poster/avatar images and cache them locally.
    DEFAULT_POSTER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="300" height="420" viewBox="0 0 300 420">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#1e293b"/>
      <stop offset="100%" stop-color="#0f172a"/>
    </linearGradient>
  </defs>
  <rect width="100%" height="100%" fill="url(#bg)"/>
  <circle cx="150" cy="180" r="45" fill="none" stroke="#f5b841" stroke-width="3" opacity="0.6"/>
  <path d="M135 180 L165 180 M150 165 L150 195" stroke="#f5b841" stroke-width="3" stroke-linecap="round" opacity="0.8"/>
  <text x="150" y="260" font-family="sans-serif" font-size="14" font-weight="600" fill="#94a3b8" text-anchor="middle">BUSTerminal</text>
  <text x="150" y="285" font-family="sans-serif" font-size="11" fill="#64748b" text-anchor="middle">Media Poster</text>
</svg>"""

    image_url = normalize_remote_image_cache_url(url)
    rejection_reason = remote_image_url_rejection_reason(image_url) if image_url else "invalid_url"
    if not image_url or rejection_reason:
        return Response(
            content=DEFAULT_POSTER_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=60", "X-Cache": "INVALID_FALLBACK"}
        )

    result = await run_in_threadpool(
        image_cache_adapter.get_proxy_image,
        image_cache_service,
        COVER_CACHE_DIR,
        image_url,
        w,
    )
    DEFAULT_POSTER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="300" height="420" viewBox="0 0 300 420">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#1e293b"/>
      <stop offset="100%" stop-color="#0f172a"/>
    </linearGradient>
  </defs>
  <rect width="100%" height="100%" fill="url(#bg)"/>
  <circle cx="150" cy="180" r="45" fill="none" stroke="#f5b841" stroke-width="3" opacity="0.6"/>
  <path d="M135 180 L165 180 M150 165 L150 195" stroke="#f5b841" stroke-width="3" stroke-linecap="round" opacity="0.8"/>
  <text x="150" y="260" font-family="sans-serif" font-size="14" font-weight="600" fill="#94a3b8" text-anchor="middle">BUSTerminal</text>
  <text x="150" y="285" font-family="sans-serif" font-size="11" fill="#64748b" text-anchor="middle">Loading Poster...</text>
</svg>"""

    # 取流成功但缓存写不进去时（缓存位被目录/符号链接占住、只读、磁盘满），
    # 图片字节仍然是好的，要照常返回，不能一律退成占位图。
    if not result or (not result.path and not result.content):
        return Response(
            content=DEFAULT_POSTER_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=60", "X-Cache": "FALLBACK"}
        )

    headers = {"Cache-Control": "public, max-age=86400", "X-Cache": result.cache_status}
    if result.source_path:
        headers["X-Image-Source"] = "thumbnail"
    if result.path:
        return FileResponse(
            result.path,
            media_type=result.media_type,
            headers=headers,
        )
    return Response(
        content=result.content or b"",
        media_type=result.media_type,
        headers=headers,
    )


@app.get("/api/cache/images/stats")
async def get_image_cache_stats():
    stats = await run_in_threadpool(image_cache_adapter.get_stats, image_cache_service, COVER_CACHE_DIR)
    return {"success": True, "data": stats}


def get_cache_stats_payload() -> Dict[str, Any]:
    image_stats = image_cache_adapter.get_stats(image_cache_service, COVER_CACHE_DIR)
    structured_stats = db.get_structured_cache_stats()
    structured_stats["refreshing_count"] = get_discovery_service().get_refreshing_count()
    maintenance_service = get_maintenance_service()
    maintenance_service.cache_size_limit_provider = (
        get_cache_size_limit_gb
        if get_cache_size_limit_gb is not DEFAULT_GET_CACHE_SIZE_LIMIT_GB
        else None
    )
    limit_gb = maintenance_service.get_cache_size_limit_gb()
    total_size_bytes = int(image_stats.get("total_size_bytes") or 0)
    limit_bytes = int(limit_gb * (1024 ** 3))
    return {
        "limit_gb": limit_gb,
        "limit_bytes": limit_bytes,
        "total_size_bytes": total_size_bytes,
        "total_size_gb": round(total_size_bytes / (1024 ** 3), 3),
        "usage_ratio": round(total_size_bytes / limit_bytes, 4) if limit_bytes else 0,
        "trigger_ratio": 0.9,
        "target_ratio": 0.8,
        "images": image_stats,
        "structured": structured_stats,
        "last_cleanup": maintenance_service.last_cache_cleanup_summary,
    }


@app.get("/api/cache/stats")
async def get_cache_stats():
    return {"success": True, "data": await run_in_threadpool(get_cache_stats_payload)}


@app.post("/api/cache/cleanup")
async def cleanup_cache_to_safe_watermark():
    summary = await run_in_threadpool(run_cache_cleanup_once)
    return {
        "success": summary.get("status") != "already_running",
        "data": summary,
        "message": summary.get("status"),
    }


class CacheInvalidationRequest(BaseModel):
    category: str = Field(..., min_length=1, max_length=40)
    confirmation: Optional[str] = Field(None, max_length=100)


class CacheWarmupRequest(BaseModel):
    scope: str = Field("recent", pattern="^(recent|movies|actresses)$")
    limit: int = Field(80, ge=1, le=IMAGE_CACHE_WARMUP_LIMIT)


@app.post("/api/cache/invalidate")
async def invalidate_cache(payload: CacheInvalidationRequest):
    category = str(payload.category or "").strip().lower()
    structured_categories = {
        "discovery_feeds",
        "movie_details",
        "navigation",
        "translations",
        "other",
    }
    image_categories = {
        "remote_images",
        "jellyfin_images",
        "thumbnails",
        "failure_markers",
    }
    if category not in structured_categories | image_categories | {"all"}:
        raise HTTPException(status_code=400, detail="unsupported_cache_category")
    if category == "all" and payload.confirmation != CACHE_INVALIDATE_ALL_CONFIRMATION:
        raise HTTPException(status_code=400, detail="cache_invalidation_confirmation_required")

    result: Dict[str, Any] = {"category": category}
    if category in structured_categories | {"all"}:
        result["structured"] = await run_in_threadpool(db.invalidate_cache_category, category)
    if category in image_categories | {"all"}:
        result["images"] = await run_in_threadpool(
            image_cache_adapter.invalidate_category,
            image_cache_service,
            COVER_CACHE_DIR,
            category,
        )
    return {"success": True, "data": result}


@app.post("/api/cache/warmup")
async def warmup_cache(payload: CacheWarmupRequest):
    if payload.scope == "movies":
        items = await run_in_threadpool(db.search_movies, None, payload.limit, 0)
    elif payload.scope == "actresses":
        items = await run_in_threadpool(db.get_actresses, payload.limit, 0)
    else:
        movie_limit = (payload.limit + 1) // 2
        actress_limit = payload.limit - movie_limit
        movies, actresses = await asyncio.gather(
            run_in_threadpool(db.search_movies, None, movie_limit, 0),
            run_in_threadpool(db.get_actresses, actress_limit, 0),
        )
        items = [*movies, *actresses]

    image_urls = collect_remote_image_urls(items, payload.limit)
    if image_urls:
        schedule_image_cache_warmup(items, f"manual_{payload.scope}")
    return {
        "success": True,
        "message": "cache_warmup_started" if image_urls else "cache_warmup_empty",
        "data": {
            "scope": payload.scope,
            "accepted": len(image_urls),
            "in_progress": bool(image_urls),
        },
    }


@app.get("/api/jellyfin/image/{item_id}")
async def proxy_jellyfin_image(
    item_id: str,
    tag: str = None,
    max_width: int = Query(360, ge=1, le=2000),
    quality: int = Query(90, ge=1, le=100),
):
    # Proxy Jellyfin images with local cache support.
    import hashlib
    import mimetypes

    jf_service = get_jellyfin_service()
    if jf_service.is_circuit_open():
        raise HTTPException(status_code=503, detail="jellyfin_unavailable")

    item_id = normalize_item_id(item_id)
    if not item_id:
        raise HTTPException(status_code=400, detail="invalid_jellyfin_item_id")

    max_image_bytes = 10 * 1024 * 1024
    cache_key = f"{item_id}_{tag}_{max_width}_{quality}" if tag else f"{item_id}_{max_width}_{quality}"
    cache_filename = hashlib.md5(cache_key.encode()).hexdigest() + ".jpg"
    jellyfin_cache_dir = os.path.join(COVER_CACHE_DIR, "jellyfin")
    cache_path = os.path.join(jellyfin_cache_dir, cache_filename)

    if is_safe_cache_file(cache_path, jellyfin_cache_dir, {".jpg", ".jpeg"}):
        cache_age = time.time() - os.path.getmtime(cache_path)
        if cache_age < 86400:  # 24 hours
            os.utime(cache_path, None)
            logger.debug("Jellyfin image cache hit: %s", display_value_for_log(item_id))
            return FileResponse(
                cache_path,
                media_type="image/jpeg",
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "X-Cache": "HIT"
                }
            )
        else:
            logger.debug("Jellyfin image cache expired: %s", display_value_for_log(item_id))

    host = db.get_config('jellyfin_host')
    key = db.get_config('jellyfin_api_key')
    if not host or not key:
            raise HTTPException(status_code=404, detail="task_not_found")

    url = f"{host.rstrip('/')}/Items/{item_id}/Images/Primary"
    image_params = {
        "format": "jpg",
        "maxWidth": max_width,
        "quality": quality,
    }
    if tag:
        image_params["tag"] = tag
    image_headers = {
        "X-Emby-Token": key,
        "Accept": "image/*",
    }

    try:
        import requests
        from starlette.concurrency import run_in_threadpool

        def fetch_and_cache():
            try:
                resp = internal_session().get(url, headers=image_headers, params=image_params, stream=True, timeout=2)
                content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if resp.status_code == 200 and content_type in SAFE_JELLYFIN_IMAGE_TYPES:
                    content_length = parse_int_value(resp.headers.get("content-length"), 0, minimum=0)
                    if content_length > max_image_bytes:
                        logger.warning("Jellyfin image too large: %s", display_value_for_log(item_id))
                        return None
                    os.makedirs(jellyfin_cache_dir, exist_ok=True)

                    chunks = []
                    total = 0
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            total += len(chunk)
                            if total > max_image_bytes:
                                logger.warning("Jellyfin image stream exceeded size limit: %s", display_value_for_log(item_id))
                                return None
                            chunks.append(chunk)
                    content = b"".join(chunks)
                    if not write_cache_bytes_if_safe(cache_path, jellyfin_cache_dir, content):
                        return {"content": content, "content_type": content_type}

                    logger.info("Jellyfin image cached: %s", display_value_for_log(item_id))
                    return {"path": cache_path}
                else:
                    logger.warning("Jellyfin image not found: %s, status=%s", display_value_for_log(item_id), resp.status_code)
                    return None
            except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError) as e:
                logger.warning("Jellyfin image fetch connection error: %s", e)
                jf_service.record_failure()
                return None

        def fetch_fallback_cover():
            jellyfin_client.reload_config()
            uid = jellyfin_client._discover_user_id()
            if not uid:
                return None

            item_url = f"{host.rstrip('/')}/Users/{uid}/Items/{item_id}"
            try:
                item_resp = internal_session().get(
                    item_url,
                    headers={"X-Emby-Token": key, "Accept": "application/json"},
                    timeout=2
                )
                if item_resp.status_code != 200:
                    return None
            except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError) as e:
                logger.warning("Jellyfin image fallback connection error: %s", e)
                jf_service.record_failure()
                return None

            item = item_resp.json()
            text_parts = [item.get("Name", "")]
            for media_source in item.get("MediaSources", []) or []:
                text_parts.append(media_source.get("Path", ""))
                text_parts.append(media_source.get("Name", ""))

            movie_id = extract_movie_id_from_text(" ".join(text_parts))
            if not movie_id:
                return None

            movie = db.get_movie(movie_id) or {}
            cover_url = movie.get("cover")
            if not cover_url:
                detail = scraper.get_movie_detail(f"{scraper.base_url.rstrip('/')}/{movie_id}", perform_translate=False)
                cover_url = (detail or {}).get("cover")

            if not cover_url:
                return None

            logger.info(
                "Jellyfin image fallback via movie cover: item=%s movie=%s",
                display_value_for_log(item_id),
                display_value_for_log(movie_id),
            )
            return cache_remote_image_url(cover_url)

        cached_file = await run_in_threadpool(fetch_and_cache)

        if isinstance(cached_file, dict) and cached_file.get("content"):
            return Response(
                content=cached_file["content"],
                media_type=cached_file.get("content_type") or "image/jpeg",
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "X-Cache": "BYPASS"
                }
            )
        cached_path = cached_file.get("path") if isinstance(cached_file, dict) else cached_file
        if cached_path and is_safe_cache_file(cached_path, jellyfin_cache_dir, {".jpg", ".jpeg"}):
            return FileResponse(
                cached_path,
                media_type="image/jpeg",
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "X-Cache": "MISS"
                }
            )
        fallback_file = await run_in_threadpool(fetch_fallback_cover)
        if fallback_file and is_safe_cache_file(fallback_file, COVER_CACHE_DIR, SAFE_REMOTE_IMAGE_EXTENSIONS):
            fallback_media_type = mimetypes.guess_type(str(fallback_file))[0] or "image/jpeg"
            return FileResponse(
                fallback_file,
                media_type=fallback_media_type,
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "X-Cache": "FALLBACK"
                }
            )
        else:
            raise HTTPException(status_code=404, detail="task_not_found")

    except requests.exceptions.Timeout:
        logger.error("Jellyfin image timeout: %s", display_value_for_log(item_id))
        raise HTTPException(status_code=504, detail="Jellyfin request timeout")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Image proxy fail: %s", safe_exception_label(e))
        raise HTTPException(status_code=500, detail="image_proxy_failed")


def daily_maintenance_task():
    return get_maintenance_service().daily_maintenance_task()


JELLYFIN_AUTOMATION_SYNC_HOUR = 3
JELLYFIN_AUTOMATION_SYNC_DATE_KEY = "jellyfin_automation_sync_last_date"


def read_automation_sync_date():
    """上次入队的日期（落库，进程重启也不丢）。"""
    try:
        raw = db.get_config(JELLYFIN_AUTOMATION_SYNC_DATE_KEY)
    except Exception:
        return None
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError:
        return None


def store_automation_sync_date(value) -> None:
    try:
        db.set_config(JELLYFIN_AUTOMATION_SYNC_DATE_KEY, value.isoformat())
    except Exception as e:
        logger.warning("Failed to persist automation sync date: %s", safe_exception_label(e))


def jellyfin_automation_sync_task():
    # Persist scheduled work so external outages and process restarts do not lose it.
    # 上次入队日期落库，且不再限制在 3:00-3:29 这半小时窗口内 —— 容器重启/发布
    # 落在窗口外时整天都会被跳过。
    last_enqueued_date = read_automation_sync_date()
    while not stop_event.is_set():
        jellyfin_client.reload_config()
        if not jellyfin_client.host or not jellyfin_client.api_key:
            task_registry.mark_success(
                "jellyfin_automation_sync_task",
                summary={"skipped": True, "reason": "jellyfin_not_configured"},
            )
            stop_event.wait(60)
            continue

        now = datetime.now()
        today = now.date()
        should_enqueue = now.hour >= JELLYFIN_AUTOMATION_SYNC_HOUR and last_enqueued_date != today

        if should_enqueue:
            try:
                enqueue_jellyfin_automation_sync(today)
                last_enqueued_date = today
                store_automation_sync_date(today)
            except Exception as e:
                logger.error("Jellyfin automation sync enqueue failed: %s", safe_exception_label(e))
                task_registry.mark_error("jellyfin_automation_sync_task", e)

        try:
            summary = process_jellyfin_automation_tasks(limit=5)
            if summary.get("completed"):
                task_registry.mark_success("jellyfin_automation_sync_task", summary=summary)
            if summary.get("deferred") or summary.get("needs_attention"):
                task_registry.mark_error(
                    "jellyfin_automation_sync_task",
                    RuntimeError("jellyfin_external_tasks_deferred"),
                )
        except Exception as e:
            logger.error("Jellyfin automation task processing failed: %s", safe_exception_label(e))
            task_registry.mark_error("jellyfin_automation_sync_task", e)

        stop_event.wait(60)


def cache_remote_image_url(url: str) -> Optional[str]:
    return image_cache_adapter.cache_remote_image_url(image_cache_service, COVER_CACHE_DIR, url)


def collect_remote_image_urls(items: Any, limit: int = IMAGE_CACHE_WARMUP_LIMIT) -> List[str]:
    return image_cache_adapter.collect_remote_image_urls(image_cache_service, COVER_CACHE_DIR, items, limit)


def warm_remote_image_cache_once(image_urls: List[str], source: str = "api_list") -> None:
    task_source = re.sub(r"[^a-zA-Z0-9_-]", "_", str(source or "api_list"))[:60]
    task_name = f"image_cache_warmup_{task_source}"
    pending = []
    with image_cache_warmup_lock:
        for image_url in image_urls[:IMAGE_CACHE_WARMUP_LIMIT]:
            keys = get_remote_image_cache_keys(image_url)
            marker = keys[0] if keys else image_url
            if marker in image_cache_warming_keys:
                continue
            image_cache_warming_keys.add(marker)
            pending.append((marker, image_url))

    if not pending:
        return

    task_registry.register(
        task_name,
        description=f"Warm image cache for {source}",
        category="cache",
        interval="on-demand",
    )
    task_registry.mark_started(task_name)
    task_registry.mark_success(
        task_name,
        summary={"source": source, "total": len(pending), "in_progress": True},
    )

    try:
        summary = image_cache_adapter.warm_remote_cache(
            image_cache_service,
            COVER_CACHE_DIR,
            [image_url for _marker, image_url in pending],
            stop_event,
        )
        summary.update({"source": source, "in_progress": False})
        task_registry.mark_success(task_name, stopped=True, summary=summary)
        logger.info("Image cache warmup completed: source=%s summary=%s", source, summary)
    except Exception as exc:
        task_registry.mark_error(task_name, exc, fatal=True)
        raise
    finally:
        with image_cache_warmup_lock:
            for marker, _image_url in pending:
                image_cache_warming_keys.discard(marker)


def schedule_image_cache_warmup(items: Any, source: str = "api_list") -> None:
    image_urls = collect_remote_image_urls(items)
    if not image_urls:
        return
    thread = threading.Thread(
        target=warm_remote_image_cache_once,
        args=(image_urls, source),
        name=f"image_cache_warmup_{source}",
        daemon=True,
    )
    thread.start()


ACTRESS_WARMUP_CURSOR_KEY = "image_warmup_actress_offset"
ACTRESS_WARMUP_BATCH_SIZE = 200
ACTRESS_WARMUP_BATCHES_PER_CYCLE = 5


def active_image_downloader_task():
    while not stop_event.is_set():
        try:
            image_urls = []
            for movie in db.search_movies(limit=50, offset=0):
                if movie.get("cover"):
                    image_urls.append(movie["cover"])

            # 女优头像增量预热：按游标每周期处理若干批，扫完全表后回到开头，
            # 保证全量头像逐步落到本地缓存，而不是只覆盖前 50 个
            offset = db.get_config(ACTRESS_WARMUP_CURSOR_KEY) or 0
            if not isinstance(offset, int) or offset < 0:
                offset = 0
            for _ in range(ACTRESS_WARMUP_BATCHES_PER_CYCLE):
                batch = db.get_actresses(limit=ACTRESS_WARMUP_BATCH_SIZE, offset=offset)
                if not batch:
                    # 游标越界（表变小了），回零重扫一次
                    offset = 0
                    batch = db.get_actresses(limit=ACTRESS_WARMUP_BATCH_SIZE, offset=0)
                    if not batch:
                        break
                for actress in batch:
                    if actress.get("avatar"):
                        image_urls.append(actress["avatar"])
                offset += len(batch)
                if len(batch) < ACTRESS_WARMUP_BATCH_SIZE:
                    db.set_config(ACTRESS_WARMUP_CURSOR_KEY, 0)
                    break
                db.set_config(ACTRESS_WARMUP_CURSOR_KEY, offset)

            summary = image_cache_adapter.warm_remote_cache(
                image_cache_service,
                COVER_CACHE_DIR,
                list(dict.fromkeys(image_urls)),
                stop_event,
            )
            processed = int(summary.get("hit") or 0) + int(summary.get("cached") or 0)
            if summary.get("failed") and processed == 0:
                task_registry.mark_error(
                    "active_image_downloader_task",
                    RuntimeError("image_sources_unavailable"),
                    summary=summary,
                )
            else:
                summary["degraded"] = bool(summary.get("failed"))
                task_registry.mark_success("active_image_downloader_task", summary=summary)
            logger.info("Active image cache cycle completed: %s", summary)
        except Exception as e:
            logger.warning("Active image downloader task failed: %s", safe_exception_label(e))
            task_registry.mark_error("active_image_downloader_task", e)

        stop_event.wait(1800)


@app.on_event("startup")
async def startup_event():
    # 库内敏感配置（Jellyfin API Key / qB 口令 / amane token 等）只在**写入时**加密
    # （见 database._encrypt_config_value），所以没配 BUS_TERMINAL_CONFIG_SECRET、
    # 或密钥是后补的，库里就会留着明文。这里启动时点一次名：不拦截启动（老部署
    # 会因此起不来），但要在日志里留下确切证据，别让人以为配了密钥就安全了。
    try:
        plaintext_keys = db.plaintext_sensitive_config_keys()
    except Exception as e:  # 审计失败不该拖垮启动
        logger.warning("Sensitive config audit skipped: %s", safe_exception_label(e))
    else:
        if plaintext_keys:
            joined = ", ".join(plaintext_keys)
            if config_encryption_enabled():
                logger.warning(
                    "Config values still stored in PLAINTEXT: %s — they were written before "
                    "BUS_TERMINAL_CONFIG_SECRET was set; re-save these settings to encrypt them.",
                    joined,
                )
            else:
                logger.warning(
                    "BUS_TERMINAL_CONFIG_SECRET is not set: %s stored in PLAINTEXT in the config "
                    "table. Set the env var and re-save these settings (KI-044/KI-048).",
                    joined,
                )

    # 翻译回填只读写本地库 + 内网大模型，没有抓取/下载等外部副作用，
    # 因此不受 BUS_TERMINAL_DISABLE_STARTUP_TASKS 限制，单独由 TRANSLATION_BACKFILL_ENABLED 控制。
    if config.TRANSLATION_BACKFILL_ENABLED:
        start_background_task(
            "translation_backfill_loop",
            translation_backfill_loop,
            description="Backfill historical title translations with the local LLM, one batch at a time",
            category="maintenance",
            interval=f"{config.TRANSLATION_BACKFILL_INTERVAL_SECONDS}s per batch",
        )

    # feed 预热同样只写本地缓存，不受 DISABLE_STARTUP_TASKS 限制
    if config.FEED_PRELOAD_REFRESH_ENABLED:
        start_background_task(
            "feed_preload_refresh_loop",
            feed_preload_refresh_loop,
            description="Refresh home/subtitle/HD/discovery feed caches periodically so pages open instantly with fresh data",
            category="crawler",
            interval=f"{config.FEED_PRELOAD_REFRESH_SECONDS}s",
        )

    # 下载闭环（下发 → 做种 → 投递）必须常驻，**不能**被 BUS_TERMINAL_DISABLE_STARTUP_TASKS
    # 一并禁掉：那个开关的本意是压住启动时的爬虫/预热（NAS 上外网走路由器、CPU 也弱，
    # 没必要开机就抓一批），但它把 groom 一起挡住的话，下完的种子永远不会被投递到
    # amane 库内，入库闭环直接断掉（2026-10-10 实测：external_tasks 恒为 0 就是这个原因）。
    start_background_task(
        "groom_torrents_task",
        groom_torrents_task,
        description="Filter qBittorrent files and process completed downloads",
        category="download",
        interval=f"{config.GROOM_TASK_INTERVAL}s",
    )

    start_background_task(
        "download_dispatcher_task",
        download_dispatcher_task,
        description="Dispatch queued downloads to qBittorrent",
        category="download",
        interval=f"{config.DOWNLOAD_DISPATCHER_INTERVAL}s",
    )

    # 辅助服务（备份 / 守卫）的健康看护同样必须常驻：NAS 上没有别的监控，
    # 它被那个开关挡住的话，备份静默失败就永远没人知道。
    start_background_task(
        "health_deps_alert_loop",
        health_deps_alert_loop,
        description="Check helper service heartbeats and backup freshness, alert with a cooldown",
        category="maintenance",
        interval=f"{config.HEALTH_DEPS_CHECK_SECONDS}s",
    )

    # 翻译进度快照（只读库）
    start_background_task(
        "translation_progress_snapshot_loop",
        translation_progress_snapshot_loop,
        description="Compute translation progress snapshot for the settings page",
        category="maintenance",
        interval=f"{config.TRANSLATION_PROGRESS_SNAPSHOT_SECONDS}s",
    )

    # 演员头像补全与 groom 同理放常驻区：缺口 61%，跑一轮补一批，
    # 靠它才能把演员页的头像慢慢填满；被 DISABLE_STARTUP_TASKS 挡住就永远补不上。
    # 自身幂等（只取缺头像的行、且只覆盖空值），全部补完后自动变成空转。
    if config.GFRIENDS_AVATAR_SYNC_ENABLED:
        start_background_task(
            "gfriends_avatar_sync_loop",
            gfriends_avatar_sync_loop,
            description="Backfill missing actress avatars from the gfriends public index",
            category="maintenance",
            interval=f"{config.GFRIENDS_AVATAR_SYNC_INTERVAL_SECONDS}s per batch",
        )

    # 入库自愈巡检：默认关闭（AMANE_HEAL_ENABLED）。与 groom 同理放在常驻区——
    # 它修复的正是「下载完成却卡在入库」这一段，被 DISABLE_STARTUP_TASKS 挡掉就失去意义；
    # 但它会自动写 amane，所以由独立开关控制，开启后每轮限量处理。
    if config.AMANE_HEAL_ENABLED:
        start_background_task(
            "amane_heal_loop",
            amane_heal_loop,
            description="Retry downloads that finished but never got organized into amane",
            category="media",
            interval=f"{config.AMANE_HEAL_INTERVAL_SECONDS}s",
        )

    if str(os.getenv("BUS_TERMINAL_DISABLE_STARTUP_TASKS", "")).lower() in ("1", "true", "yes", "on"):
        logger.warning("Startup background tasks disabled by BUS_TERMINAL_DISABLE_STARTUP_TASKS")
        if not os.path.exists(COVER_CACHE_DIR):
            os.makedirs(COVER_CACHE_DIR)
        if str(os.getenv("BUS_TERMINAL_PRELOAD_ON_STARTUP", "")).lower() in ("1", "true", "yes", "on"):
            start_background_task(
                "information_flow_preload_startup",
                preload_information_flow_cache,
                args=(INFORMATION_FLOW_PRELOAD_PAGES, False),
                description="Preload home, discovery, subtitle, and HD feeds without starting other background tasks",
                category="crawler",
                interval="startup-once",
            )
        return

    try:
        report = await run_in_threadpool(import_actress_base_if_available)
        if report.get("imported"):
            logger.info(
                "Actress base DB import completed: imported=%s actresses=%s movies=%s",
                report.get("imported"),
                report.get("actresses"),
                report.get("movies"),
            )
    except Exception as e:
        logger.error("Actress base DB import failed: %s", safe_exception_label(e))

    jable_scraper.reload_config()
    discovery_scraper.reload_config()

    start_background_task(
        "information_flow_preload_startup",
        preload_information_flow_cache,
        args=(INFORMATION_FLOW_PRELOAD_PAGES, False),
        description="Preload home, subtitle, HD, and discovery feeds into local cache",
        category="crawler",
        interval="startup-once",
    )

    start_background_task(
        "crawl_recent_movies",
        scraper.crawl_recent_movies,
        args=(5,),
        description="Crawl recent movies on startup and warm local movie cache",
        category="crawler",
        interval="startup-once",
    )

    start_background_task(
        "auto_crawl_subscriptions",
        auto_crawl_subscriptions,
        description="Check subscriptions periodically and discover new movies",
        category="crawler",
        interval=f"{config.SUBSCRIPTION_CHECK_INTERVAL}s",
    )

    # groom_torrents_task 与 download_dispatcher_task 已在本函数前面、
    # BUS_TERMINAL_DISABLE_STARTUP_TASKS 判断之前注册过（下载闭环不能被该开关禁掉），
    # 此处不再重复注册。

    if not os.path.exists(COVER_CACHE_DIR):
        os.makedirs(COVER_CACHE_DIR)
        logger.info("Created cover cache directory: %s", COVER_CACHE_DIR)

    start_background_task(
        "actress_metadata_backfill_loop",
        actress_metadata_backfill_loop,
        description="Low-frequency Netflav actress metadata completion for actors discovered during downloads",
        category="crawler",
        interval=f"{ACTRESS_METADATA_RETRY_SECONDS}s",
    )

    start_background_task(
        "cache_cleanup_task",
        cache_cleanup_task,
        description="Clean local image cache by configured size limit",
        category="maintenance",
        interval="3600s",
    )

    # BI 看板快照：默认关闭（会全量扫库 + 分页读 Jellyfin）。
    # 开了才每天自动跑两次（11:30 / 18:00），没开时看板按需现算。
    if as_bool(db.get_config("bi_dashboard_enabled"), False):
        start_background_task(
            "bi_snapshot_task",
            bi_snapshot_loop,
            description="Refresh BI dashboard snapshot twice a day (11:30 / 18:00)",
            category="maintenance",
            interval="60s",
        )

    start_background_task(
        "disk_space_monitor_task",
        disk_space_monitor_task,
        description="Monitor disk space and trigger download throttling",
        category="maintenance",
        interval=f"{config.DISK_MONITOR_INTERVAL}s",
    )

    start_background_task(
        "daily_maintenance_task",
        daily_maintenance_task,
        description="Daily maintenance for navigation, categories, actresses, and Jellyfin metadata",
        category="maintenance",
        interval="daily 02:00",
    )

    start_background_task(
        "jellyfin_automation_sync_task",
        jellyfin_automation_sync_task,
        description="Daily Jellyfin favorite/watched sync and optional candidate auto approval",
        category="automation",
        interval="daily 03:00",
    )

    # homepage_refresh_thread = threading.Thread(target=homepage_refresh_task)
    # homepage_refresh_thread.daemon = True
    # homepage_refresh_thread.start()

    start_background_task(
        "active_image_downloader_task",
        active_image_downloader_task,
        description="Actively cache cover and avatar images",
        category="maintenance",
        interval="1800s",
    )

    start_background_task(
        "jable_hot_auto_task_loop",
        jable_hot_auto_task_loop,
        description="Discover Jable hot movies and enqueue them when enabled",
        category="crawler",
        interval="daily 22:00",
    )

    start_background_task(
        "pending_download_refresh_loop",
        pending_download_refresh_loop,
        description="Retry pending downloads that are missing magnet links",
        category="download",
        interval=f"{PENDING_DOWNLOAD_SCAN_INTERVAL_SECONDS}s with backoff",
    )

    start_background_task(
        "jellyfin_cleanup_candidate_scan",
        jellyfin_cleanup.run_registered_loop,
        args=(stop_event, task_registry),
        description="Scan Jellyfin watched items into non-destructive cleanup candidates",
        category="maintenance",
        interval="hourly; candidate generation only",
    )

    start_background_task(
        "auto_delete_watched_daily",
        auto_delete_watched_daily_task,
        description="Auto-delete watched-once non-favorite movies daily (Jellyfin+files+torrents, keep DB)",
        category="maintenance",
        interval="hourly check; runs at auto_delete_run_hour when enabled",
    )

    logger.info("Background tasks started")

@app.on_event("shutdown")
async def shutdown_event():
    # Signal background threads to stop.
    logger.info("Stopping background tasks...")
    stop_event.set()
    jellyfin_cleanup.stop()
    logger.info("Background tasks stopped")

def get_qb_session():
    return get_qb_service().get_session()


DEFAULT_GET_QB_SESSION = get_qb_session


def sync_qb_service_runtime_dependencies() -> QBService:
    service = get_qb_service()
    service.session_provider = get_qb_session if get_qb_session is not DEFAULT_GET_QB_SESSION else None
    service.run_jav_file_filter_once = run_jav_file_filter_once
    service.config_payload_normalizer = (
        normalize_qb_config_payload
        if normalize_qb_config_payload is not DEFAULT_NORMALIZE_QB_CONFIG_PAYLOAD
        else None
    )
    return service

def get_bool_config(key: str, default: bool = False) -> bool:
    value = db.get_config(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")

def get_today_start_ts() -> float:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return today.timestamp()

ACTRESS_METADATA_RETRY_SECONDS = 6 * 3600
ACTRESS_METADATA_BATCH_SIZE = 10
ACTRESS_METADATA_MAX_ATTEMPTS = 5
_actress_service: Optional[ActressService] = None
_system_config_service: Optional[SystemConfigService] = None


def get_actress_service() -> ActressService:
    global _actress_service
    if _actress_service is None:
        _actress_service = ActressService(
            db=db,
            netflav_scraper=netflav_scraper,
            logger=logger,
            task_registry=task_registry,
            stop_event=stop_event,
            safe_number=safe_number,
            safe_exception_label=safe_exception_label,
            display_value_for_log=display_value_for_log,
            as_bool=as_bool,
            resolve_actress_subscription_url=resolve_actress_subscription_url,
            get_actress_subscription_url_candidates=get_actress_subscription_url_candidates,
            get_subscription_delete_url_candidates=get_subscription_delete_url_candidates,
            subscription_url_exists=subscription_url_exists,
            subscription_visible_for_actress=subscription_visible_for_actress,
            normalize_subscription_display_name=normalize_subscription_display_name,
            metadata_retry_seconds=ACTRESS_METADATA_RETRY_SECONDS,
            metadata_batch_size=ACTRESS_METADATA_BATCH_SIZE,
            metadata_max_attempts=ACTRESS_METADATA_MAX_ATTEMPTS,
        )
    _actress_service.stop_event = stop_event
    _actress_service.netflav_scraper = netflav_scraper
    _actress_service.resolve_actress_subscription_url = resolve_actress_subscription_url
    _actress_service.get_actress_subscription_url_candidates = get_actress_subscription_url_candidates
    _actress_service.get_subscription_delete_url_candidates = get_subscription_delete_url_candidates
    _actress_service.subscription_url_exists = subscription_url_exists
    _actress_service.subscription_visible_for_actress = subscription_visible_for_actress
    _actress_service.normalize_subscription_display_name = normalize_subscription_display_name
    return _actress_service

def actress_has_basic_metadata(actress: Optional[Dict]) -> bool:
    return get_actress_service().has_basic_metadata(actress)


def get_auto_download_daily_limit() -> int:
    value = safe_number(db.get_config("auto_download_daily_limit"), 3)
    if value < -1:
        return 3
    return int(value)


def get_disk_threshold_bytes() -> float:
    raw_threshold = db.get_config("disk_threshold_gb")
    if raw_threshold in (None, ""):
        return float(config.DISK_SPACE_THRESHOLD)
    threshold_gb = safe_number(raw_threshold, -1)
    if threshold_gb < 0:
        return float(config.DISK_SPACE_THRESHOLD)
    return threshold_gb * (1024 ** 3)

def queue_actress_metadata_from_detail(detail: Optional[Dict], source: str = "download_detail") -> int:
    return get_actress_service().queue_metadata_from_detail(detail, source)

def actress_metadata_backfill_loop():
    return get_actress_service().metadata_backfill_loop()

def evaluate_download_permission(
    movie_id: Optional[str],
    magnet: Optional[str],
    title: str,
    source_url: Optional[str],
    *,
    automated: bool = True,
) -> Dict[str, Any]:
    return get_download_service().evaluate_download_permission(
        movie_id,
        magnet,
        title,
        source_url,
        automated=automated,
    )


def queue_download_if_absent(
    magnet: str,
    movie_id: str,
    title: str,
    source_url: Optional[str],
    save_path: Optional[str] = None,
    category: Optional[str] = None,
) -> Dict[str, Any]:
    return get_download_service().queue_download_if_absent(
        magnet,
        movie_id,
        title,
        source_url,
        save_path,
        category,
    )


_download_service: Optional[DownloadService] = None
_qb_service: Optional[QBService] = None


def get_qb_service() -> QBService:
    global _qb_service
    if _qb_service is None:
        _qb_service = QBService(
            db=db,
            qb_api=qb_api,
            config=config,
            logger=logger,
            safe_number=safe_number,
            format_bytes=format_bytes,
            format_speed=format_speed,
            display_value_for_log=display_value_for_log,
            normalize_qb_root_path=normalize_qb_root_path,
            normalize_qb_hashes=normalize_qb_hashes,
            normalize_qb_category=normalize_qb_category,
            run_jav_file_filter_once=run_jav_file_filter_once,
            control_all_confirmation=QB_CONTROL_ALL_CONFIRMATION,
        )
    return _qb_service


def get_download_service() -> DownloadService:
    global _download_service
    if _download_service is None:
        _download_service = DownloadService(
            db=db,
            qb_api=qb_api,
            config=config,
            task_registry=task_registry,
            logger=logger,
            stop_event=stop_event,
            normalize_magnet_url=normalize_magnet_url,
            get_bool_config=get_bool_config,
            get_automation_mode=lambda: get_automation_service().get_settings()["automation_mode"],
            get_auto_download_daily_limit=get_auto_download_daily_limit,
            get_today_start_ts=get_today_start_ts,
            get_qb_session=lambda: get_qb_session(),
            get_disk_threshold_bytes=get_disk_threshold_bytes,
            safe_exception_label=safe_exception_label,
            display_value_for_log=display_value_for_log,
            display_path_for_log=display_path_for_log,
            safe_number=safe_number,
            parse_int_value=parse_int_value,
            extract_movie_id_from_text=extract_movie_id_from_text,
            get_qb_delivery_folder=get_qb_delivery_folder,
            on_download_completed=lambda movie_id, torrent: schedule_amane_ingest(
                movie_id, title=(torrent or {}).get("name")
            ),
            check_library_presence=lambda movie_id: get_amane_ingest_service().check_library_presence(
                movie_id
            ),
            is_blocked=lambda *texts: get_onboarding_service().is_blocked(*texts),
        )
    return _download_service


from routers.download_queue import router as download_queue_router

app.include_router(download_queue_router)



_subscription_service: Optional[SubscriptionService] = None


def get_subscription_service() -> SubscriptionService:
    global _subscription_service
    if _subscription_service is None:
        _subscription_service = SubscriptionService(
            db=db,
            scraper=scraper,
            logger=logger,
            is_internal_subscription_url=is_internal_subscription_url,
            normalize_scraper_url=normalize_scraper_url,
            normalize_magnet_url=normalize_magnet_url,
            queue_actress_metadata_from_detail=queue_actress_metadata_from_detail,
            evaluate_download_permission=evaluate_download_permission,
            queue_download_if_absent=queue_download_if_absent,
            safe_exception_label=safe_exception_label,
            display_value_for_log=display_value_for_log,
        )
    return _subscription_service


MIN_JAV_VIDEO_FILE_SIZE_BYTES = 400 * 1024 * 1024
JAV_FILE_FILTER_TAG = "smart_filter_400mb"
SEEDING_LIMIT_TAG = "seeding_limit_set"
DOWNLOAD_RECORDED_TAG = "download_recorded"
VIDEO_FILE_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".rmvb", ".ts", ".m2ts"}

def normalize_qb_tags(raw_tags: Any) -> List[str]:
    return get_download_service().normalize_qb_tags(raw_tags)

def get_qb_file_index(file_entry: Dict[str, Any], fallback_index: int) -> int:
    return get_download_service().get_qb_file_index(file_entry, fallback_index)

def is_groomable_torrent(torrent: Dict[str, Any]) -> bool:
    return get_download_service().is_groomable_torrent(torrent)

def filter_jav_torrent_files(session, base_url: str, torrent: Dict[str, Any]) -> Dict[str, Any]:
    return get_download_service().filter_jav_torrent_files(session, base_url, torrent)

def run_jav_file_filter_once() -> Dict[str, Any]:
    service = get_download_service()
    service.get_qb_session = get_qb_session
    return service.run_jav_file_filter_once(filter_torrent_files=filter_jav_torrent_files)

def groom_torrents_task():
    service = get_download_service()
    service.stop_event = stop_event
    service.get_qb_session = get_qb_session
    return service.groom_torrents_task(filter_torrent_files=filter_jav_torrent_files)

# Global switch for pausing subscription background work.
GLOBAL_SUBSCRIPTION_PAUSED = False
_maintenance_service: Optional[MaintenanceService] = None


def get_global_subscription_paused() -> bool:
    return GLOBAL_SUBSCRIPTION_PAUSED


def set_global_subscription_paused(value: bool) -> None:
    global GLOBAL_SUBSCRIPTION_PAUSED
    GLOBAL_SUBSCRIPTION_PAUSED = bool(value)


def get_maintenance_service() -> MaintenanceService:
    global _maintenance_service
    if _maintenance_service is None:
        _maintenance_service = MaintenanceService(
            db=db,
            qb_api=qb_api,
            config=config,
            task_registry=task_registry,
            scraper=scraper,
            logger=logger,
            stop_event=stop_event,
            cover_cache_dir=COVER_CACHE_DIR,
            cache_cleanup_image_suffixes=CACHE_CLEANUP_IMAGE_SUFFIXES,
            pending_scan_interval_seconds=PENDING_DOWNLOAD_SCAN_INTERVAL_SECONDS,
            information_flow_preload_pages=INFORMATION_FLOW_PRELOAD_PAGES,
            get_qb_session=get_qb_session,
            get_disk_threshold_bytes=get_disk_threshold_bytes,
            get_subscription_paused=get_global_subscription_paused,
            set_subscription_paused=set_global_subscription_paused,
            safe_number=safe_number,
            safe_exception_label=safe_exception_label,
            display_path_for_log=display_path_for_log,
            normalize_magnet_url=normalize_magnet_url,
            evaluate_download_permission=evaluate_download_permission,
            queue_download_if_absent=queue_download_if_absent,
            select_due_pending_downloads=select_due_pending_downloads,
            summarize_pending_downloads=summarize_pending_downloads,
            preload_information_flow_cache=preload_information_flow_cache,
            is_safe_cache_file=is_safe_cache_file,
        )
    _maintenance_service.stop_event = stop_event
    _maintenance_service.cover_cache_dir = COVER_CACHE_DIR
    _maintenance_service.get_qb_session = get_qb_session
    _maintenance_service.auto_delete_watched_provider = run_auto_delete_watched_once
    return _maintenance_service

def check_disk_space_logic(trigger_source="auto"):
    return get_maintenance_service().check_disk_space_logic(trigger_source=trigger_source)


def disk_space_monitor_task():
    return get_maintenance_service().disk_space_monitor_task()


def get_cache_size_limit_gb() -> float:
    return get_maintenance_service().get_cache_size_limit_gb()


DEFAULT_GET_CACHE_SIZE_LIMIT_GB = get_cache_size_limit_gb


def run_cache_cleanup_once() -> Dict[str, Any]:
    service = get_maintenance_service()
    service.cache_size_limit_provider = get_cache_size_limit_gb if get_cache_size_limit_gb is not DEFAULT_GET_CACHE_SIZE_LIMIT_GB else None
    return service.run_cache_cleanup_once()


def cache_cleanup_task():
    return get_maintenance_service().cache_cleanup_task()

def download_dispatcher_task():
    return get_download_service().download_dispatcher_task()

def process_subscription(sub: dict, force_refresh: bool = False) -> dict:
    return get_subscription_service().process_subscription(sub, force_refresh=force_refresh)

def auto_crawl_subscriptions():
    # Background task: crawl subscriptions independently so one source cannot block the rest.
    while not stop_event.is_set():
        try:
            if not GLOBAL_SUBSCRIPTION_PAUSED:
                logger.info("Starting subscription crawl cycle")
                subscriptions = db.get_subscriptions()
                summary = {
                    "subscription_count": len(subscriptions),
                    "processed_count": 0,
                    "failed_count": 0,
                    "candidate_count": 0,
                    "queued_count": 0,
                }

                for sub in subscriptions:
                    if stop_event.is_set():
                        break
                    try:
                        report = process_subscription(sub, force_refresh=True)
                        summary["processed_count"] += 1
                        summary["failed_count"] += int(report.get("failed_count") or 0)
                        summary["candidate_count"] += int(report.get("pending_count") or 0)
                        summary["queued_count"] += int(report.get("added_count") or 0)
                    except Exception as exc:
                        summary["failed_count"] += 1
                        logger.error(
                            "Subscription source failed; continuing remaining sources: %s",
                            safe_exception_label(exc),
                        )

                if summary["failed_count"]:
                    task_registry.mark_error(
                        "auto_crawl_subscriptions",
                        RuntimeError("subscription_sources_unavailable"),
                    )
                else:
                    task_registry.mark_success("auto_crawl_subscriptions", summary=summary)

                logger.info("Subscription crawl cycle finished; next run in %s hours", config.SUBSCRIPTION_CHECK_INTERVAL // 3600)
            else:
                logger.info("Subscription crawl is paused; waiting for resume conditions")
                task_registry.mark_success(
                    "auto_crawl_subscriptions",
                    summary={"paused": True},
                )

        except Exception as e:
            logger.error("Subscription crawl loop failed: %s", safe_exception_label(e))
            task_registry.mark_error("auto_crawl_subscriptions", e)

        stop_event.wait(config.SUBSCRIPTION_CHECK_INTERVAL)

def run_auto_delete_watched_once(reason_tag: str = "auto_delete") -> Dict[str, Any]:
    """执行一次已看片自动删除：删 Jellyfin 条目 + 磁盘文件 + qBittorrent 种子，保留 DB 记录。
    供每日定时任务与空间告急紧急清理共用。"""
    try:
        watch_count_max = int(db.get_config("auto_delete_watch_count_max") or 1)
        retention_days = int(db.get_config("auto_delete_retention_days") or 30)
        batch_limit = int(db.get_config("auto_delete_batch_limit") or 50)
        keep_favorite = get_bool_config("auto_delete_keep_favorite", True)

        session_base = get_qb_session()
        session = session_base[0] if session_base else None
        base_url = session_base[1] if session_base else None

        def _delete_jellyfin_item(jellyfin_id):
            try:
                service = get_jellyfin_service()
                service.jellyfin_client.reload_config()
                return bool(service.jellyfin_client.delete_item(jellyfin_id))
            except Exception as exc:
                logger.error("Auto-delete: 删除 Jellyfin 条目 %s 失败: %s", safe_exception_label(str(jellyfin_id)), safe_exception_label(exc))
                return False

        summary = lifecycle_manager.run_auto_delete_watched(
            session,
            base_url,
            _delete_jellyfin_item,
            watch_count_max=watch_count_max,
            retention_days=retention_days,
            batch_limit=batch_limit,
            keep_favorite=keep_favorite,
            reason_tag=reason_tag,
        )
        logger.info("Auto-delete (%s) summary: %s", reason_tag, summary)
        return summary
    except Exception as exc:
        logger.error("Auto-delete (%s) 执行失败: %s", reason_tag, safe_exception_label(exc))
        return {"reason_tag": reason_tag, "error": safe_exception_label(exc)}

def auto_delete_watched_daily_task():
    """每天在 auto_delete_run_hour 执行一次已看片自动删除。
    每小时唤醒检查，命中运行小时且当日未执行则触发。"""
    last_run_date = None
    while not stop_event.is_set():
        try:
            enabled = get_bool_config("jellyfin_auto_delete_enabled", False)
            mode = str(db.get_config("auto_delete_mode") or "both").strip().lower()
            run_hour = int(db.get_config("auto_delete_run_hour") or 3)

            today = datetime.now().strftime("%Y-%m-%d")
            due = (
                enabled
                and mode in ("daily", "both")
                and last_run_date != today
                and datetime.now().hour == run_hour
            )
            if due:
                logger.info("Auto-delete daily task triggered (hour=%s)", run_hour)
                run_auto_delete_watched_once(reason_tag="daily_retention")
                last_run_date = today
                task_registry.mark_success(
                    "auto_delete_watched_daily",
                    summary={"reason_tag": "daily_retention", "date": today},
                )
        except Exception as exc:
            logger.error("Auto-delete daily loop error: %s", safe_exception_label(exc))
            task_registry.mark_error("auto_delete_watched_daily", exc)

        stop_event.wait(3600)

def jable_hot_auto_task_loop():
    return get_discovery_service().jable_hot_auto_task_loop()


def pending_download_refresh_loop():
    return get_maintenance_service().pending_download_refresh_loop()


def amane_heal_loop():
    """入库自愈巡检：把「下载已完成、却迟迟没被整理入库」的影片自动补号 + 刮削 + 整理。

    绕开 amane refresh 失败（见 docs/AMANE_INGEST_HEAL.md）。**默认关闭**
    （AMANE_HEAL_ENABLED=false）——这是会自动写 amane 的操作，开关交给运维。
    每轮限量、串行处理；amane 不可用时只记日志，不中断循环。
    """
    interval = config.AMANE_HEAL_INTERVAL_SECONDS
    while not stop_event.is_set():
        try:
            report = get_amane_heal_service().heal_batch(
                limit=config.AMANE_HEAL_BATCH_LIMIT,
                older_than_seconds=config.AMANE_HEAL_STUCK_AFTER_SECONDS,
                dry_run=False,
            )
            attempted = int(report.get("attempted") or 0)
            if attempted:
                logger.info(
                    "amane heal loop: scanned=%s attempted=%s healed=%s failed=%s",
                    report.get("scanned"), attempted,
                    report.get("healed"), report.get("failed"),
                )
            task_registry.mark_success(
                "amane_heal_loop",
                summary={"scanned": report.get("scanned"), "healed": report.get("healed")},
            )
        except Exception as e:
            logger.error("amane heal loop failed: %s", safe_exception_label(e))
        stop_event.wait(interval)


def gfriends_avatar_sync_loop():
    """演员头像定时补全：持续把 gfriends 头像库里的图回填到缺头像的演员身上。

    生产实测缺口 62,175 / 102,314（61% 演员无头像），页面上演员卡片大量空白。
    **默认关闭**（GFRIENDS_AVATAR_SYNC_ENABLED=false）——会持续出网拉索引并写库，开关交给运维。

    幂等，可放心长期跑：
      · 只取 `get_actresses_missing_avatar()`，已补过的不在候选里；
      · `update_actress_avatars()` 只覆盖原本为空的行，不会动已有头像；
      · 全部补完后 candidates 会降到 0，循环自然变成空转，不产生额外请求。
    """
    from gfriends_sync import GfriendsAvatarSyncer

    interval = config.GFRIENDS_AVATAR_SYNC_INTERVAL_SECONDS
    limit = config.GFRIENDS_AVATAR_SYNC_BATCH
    # 命中率实测只有 2~15%，所以每轮要扫远多于 limit 条才能补到量；
    # randomize 避开「永远只扫存储顺序前 N 行」的饥饿（否则循环空转、零产出）。
    scan_window = min(max(limit * config.GFRIENDS_AVATAR_SYNC_SCAN_FACTOR, limit), 50000)
    syncer = GfriendsAvatarSyncer(db)
    while not stop_event.is_set():
        try:
            report = syncer.sync(limit=scan_window, randomize=True)
            applied = int(report.get("applied") or 0)
            matched = int(report.get("matched") or 0)
            candidates = int(report.get("candidates") or 0)
            if candidates or applied:
                logger.info(
                    "gfriends avatar loop: scan=%s matched=%s applied=%s (%.1f%%)",
                    candidates, matched, applied,
                    100.0 * matched / candidates if candidates else 0.0,
                )
            task_registry.mark_success(
                "gfriends_avatar_sync_loop",
                summary={"candidates": candidates, "matched": matched, "applied": applied},
            )
        except Exception as e:
            # 拉索引失败（网络/上游改版）不该中断循环，下一轮继续
            logger.error("gfriends avatar loop failed: %s", safe_exception_label(e))
        stop_event.wait(interval)


def translation_backfill_loop():
    return get_maintenance_service().translation_backfill_loop()

def homepage_refresh_task():
    # Background task: refresh homepage cache.
    while not stop_event.is_set():
        try:
            scraper.refresh_homepage()
        except Exception as e:
            logger.error("Homepage refresh task failed: %s", safe_exception_label(e))

        stop_event.wait(config.HOMEPAGE_REFRESH_INTERVAL)


def translation_progress_snapshot_loop():
    """
    定期统计翻译进度（全量去重统计要扫 1.4M 行、20~30 秒），写进 config 供页面读取。

    只读数据库，没有外部副作用，所以不受 BUS_TERMINAL_DISABLE_STARTUP_TASKS 限制。
    """
    interval = config.TRANSLATION_PROGRESS_SNAPSHOT_SECONDS
    while not stop_event.is_set():
        try:
            progress = db.get_translation_progress()
            db.set_config(
                TRANSLATION_PROGRESS_SNAPSHOT_KEY,
                {**progress, "computed_at": time.time()},
            )
            task_registry.mark_success(
                "translation_progress_snapshot_loop",
                summary={
                    "translated_total": progress.get("translated_total"),
                    "pending_total": progress.get("pending_total"),
                },
            )
        except Exception as e:
            logger.error("Translation progress snapshot failed: %s", safe_exception_label(e))
            task_registry.mark_error("translation_progress_snapshot_loop", e)

        stop_event.wait(interval)


def feed_preload_refresh_loop():
    """
    周期性把首页/字幕/高清/发现 feed 重新预热一遍：打开页面既是新数据、又命中本地缓存秒开。

    只写本地 feed 缓存（不触发下载、Jellyfin 写入等副作用），所以不受
    BUS_TERMINAL_DISABLE_STARTUP_TASKS 限制，单独由 FEED_PRELOAD_REFRESH_* 控制。
    """
    while not stop_event.is_set():
        try:
            preload_information_flow_cache(
                max_pages=INFORMATION_FLOW_PRELOAD_PAGES,
                force_refresh=True,
            )
            task_registry.mark_success(
                "feed_preload_refresh_loop",
                summary={
                    "max_pages": INFORMATION_FLOW_PRELOAD_PAGES,
                    "interval_s": config.FEED_PRELOAD_REFRESH_SECONDS,
                },
            )
        except Exception as e:
            logger.error("Feed preload refresh failed: %s", safe_exception_label(e))
            task_registry.mark_error("feed_preload_refresh_loop", e)

        stop_event.wait(config.FEED_PRELOAD_REFRESH_SECONDS)

# Configure authentication (added before CORS so CORS stays the outermost middleware).
app.add_middleware(auth_service.AuthMiddleware)

# Configure CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.get_cors_origins(),
    allow_credentials=config.get_cors_allow_credentials(),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Catch-all for uncaught route exceptions; HTTPException and
    # RequestValidationError still use FastAPI's default JSON handlers.
    logger.exception(
        "Unhandled exception on %s %s", request.method, request.url.path
    )
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "message": "服务器内部错误",
            "error_type": safe_exception_label(exc),
        },
    )

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(STATIC_DIR):
    assets_dir = os.path.join(STATIC_DIR, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    logger.info("Frontend static directory mounted: %s", display_path_for_log(STATIC_DIR))



class DownloadRequest(BaseModel):
    # Download request.
    movie_url: str = Field(..., min_length=1, max_length=2048)
    preference: str = Field("1080P", max_length=32)  # 4K, 1080P, 720P
    movie_id: Optional[str] = Field(None, max_length=128)
    title: Optional[str] = Field(None, max_length=500)
    save_path: Optional[str] = Field(None, max_length=512)
    category: Optional[str] = Field(None, max_length=64)


class MagnetDownloadRequest(BaseModel):
    # Magnet download request.
    magnet_url: str = Field(..., min_length=1, max_length=8192)
    save_path: Optional[str] = Field(None, max_length=512)
    category: Optional[str] = Field(None, max_length=64)


class QBConfig(BaseModel):
    # qBittorrent config.
    host: str = Field(..., max_length=2048)
    port: int
    username: str = Field("admin", max_length=128)
    password: str = Field("", max_length=512)


class QBTorrentControlRequest(BaseModel):
    hashes: List[str] = Field(default_factory=list, max_items=5000)
    action: str = Field(..., min_length=1, max_length=16)
    all: bool = False
    confirm: Optional[str] = Field(None, max_length=64)


class AmaneConfigPayload(BaseModel):
    base_url: Optional[str] = Field(None, max_length=2048)
    token: Optional[str] = Field(None, max_length=512)


class AmaneRefreshPayload(BaseModel):
    library_id: int
    path: str = Field("", max_length=2048)
    scan: Optional[List[str]] = None
    scrape: Optional[List[str]] = None
    wait: bool = False


class AmaneOrganizePayload(BaseModel):
    library_id: int
    path: str = Field("", max_length=2048)
    media_file_ids: Optional[List[int]] = None
    wait: bool = False


class AmaneScrapePayload(BaseModel):
    number: Optional[str] = Field(None, max_length=128)
    media_id: Optional[int] = None
    content_type: Optional[str] = Field(None, max_length=32)
    wait: bool = False


class AmaneIngestConfigPayload(BaseModel):
    enabled: Optional[bool] = None
    library_id: Optional[int] = None
    scan_path: Optional[str] = Field(None, max_length=2048)
    organize_enabled: Optional[bool] = None
    verify_visible: Optional[bool] = None
    dedupe_enabled: Optional[bool] = None


class AmaneIngestPayload(BaseModel):
    movie_id: str = Field(..., min_length=1, max_length=128)
    number: Optional[str] = Field(None, max_length=128)
    title: Optional[str] = Field(None, max_length=500)
    force: bool = False


class AmaneIngestBatchPayload(BaseModel):
    items: List[AmaneIngestPayload] = Field(default_factory=list, max_items=200)


class SubscriptionRequest(BaseModel):
    # Subscription request.
    url: str = Field(..., min_length=1, max_length=2048)
    name: str = Field(..., min_length=1, max_length=200)
    category_type: str = Field(..., min_length=1, max_length=64)
    start_date: str = Field(..., min_length=4, max_length=20)  # YYYY-MM-DD
    exclude_tags: str = Field("", max_length=500)


# MARK: - Models


DEFAULT_JELLYFIN_LIBRARY_NAME = "\u9ec4\u7247"


class SystemConfig(BaseModel):
    # System config.
    disk_threshold_gb: Optional[float] = None
    seeding_time_limit_min: Optional[int] = None
    translation_provider: Optional[str] = "google_free" # google_free, baidu_dev, ollama_local
    baidu_app_id: Optional[str] = None
    baidu_secret: Optional[str] = None
    ollama_base_url: Optional[str] = None
    ollama_model: Optional[str] = None
    system_proxy: Optional[str] = None
    enable_domain_rotation: bool = True
    fixed_domain: Optional[str] = "https://www.javbus.com"
    cache_size_limit_gb: float = config.CACHE_SIZE_LIMIT_GB
    jellyfin_host: Optional[str] = ""
    jellyfin_api_key: Optional[str] = ""
    jable_hot_auto_download: bool = False
    # 内网服务(amane/Jellyfin/qB)是否跟随系统代理环境变量。生产保持 false，
    # 只有开发机确实被代理挡住时才打开。外网抓取走 system_proxy，与此无关。
    internal_proxy_enabled: bool = config.INTERNAL_PROXY_ENABLED
    automation_mode: str = Field("suggest", pattern="^(off|suggest|auto_approve_low_risk)$")
    auto_follow_enabled: bool = False
    auto_subscribe_enabled: bool = False
    auto_download_enabled: bool = False
    auto_cleanup_enabled: bool = False
    auto_download_daily_limit: int = 3
    delivery_folder: Optional[str] = config.DELIVERY_FOLDER

    # Jellyfin cleanup config.
    jellyfin_library_name: Optional[str] = DEFAULT_JELLYFIN_LIBRARY_NAME
    cleanup_enabled: bool = False
    cleanup_progress_threshold: int = 80
    cleanup_run_hour: int = 3
    jellyfin_delete_enabled: bool = False
    jellyfin_auto_approve_candidates: bool = False

    # Email notification config.
    email_enabled: bool = False
    email_host: Optional[str] = "smtp.qq.com"
    email_port: int = 465
    email_user: Optional[str] = ""
    email_pass: Optional[str] = ""
    email_to: Optional[str] = ""

class ActressSubscriptionRequest(BaseModel):
    actress_id: str = Field(..., min_length=1, max_length=128)


class JellyfinWebhookRequest(BaseModel):
    Name: Optional[str] = Field(None, max_length=500)
    ItemId: Optional[str] = Field(None, max_length=128)
    ItemType: Optional[str] = Field(None, max_length=64)
    NotificationType: Optional[str] = Field(None, max_length=128)
    UserData: Optional[Dict[str, Any]] = None
    PlayedToCompletion: Optional[bool] = None

    class Config:
        extra = "allow"


class AutomationCandidateDecisionRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)
    actor: Optional[str] = Field("user", max_length=64)


class AutomationSettingsRequest(BaseModel):
    automation_mode: Optional[str] = Field(None, pattern="^(off|suggest|auto_approve_low_risk)$")
    auto_follow_enabled: Optional[bool] = None
    auto_subscribe_enabled: Optional[bool] = None
    auto_download_enabled: Optional[bool] = None
    auto_cleanup_enabled: Optional[bool] = None
    jellyfin_auto_approve_candidates: Optional[bool] = None



from starlette.concurrency import run_in_threadpool


def get_system_config_service() -> SystemConfigService:
    global _system_config_service
    if _system_config_service is None:
        _system_config_service = SystemConfigService(
            db=db,
            config=config,
            normalize_qb_root_path=normalize_qb_root_path,
            default_jellyfin_library_name=DEFAULT_JELLYFIN_LIBRARY_NAME,
        )
    _system_config_service.normalize_qb_root_path = normalize_qb_root_path
    return _system_config_service


def get_system_config_payload() -> Dict[str, Any]:
    payload = get_system_config_service().payload()
    payload.update(get_automation_service().get_settings())
    return payload


def should_skip_sensitive_config_update(key: str, value: Any) -> bool:
    return get_system_config_service().should_skip_sensitive_update(key, value)


def is_valid_http_config_url(
    value: Any,
    *,
    allow_credentials: bool = False,
    allow_path: bool = False,
) -> bool:
    return get_system_config_service().is_valid_http_config_url(
        value,
        allow_credentials=allow_credentials,
        allow_path=allow_path,
    )


def validate_system_config_payload(data: Dict[str, Any]) -> List[str]:
    return get_system_config_service().validate_payload(data)


def normalize_qb_host(raw_host: Any) -> Optional[str]:
    return get_qb_service().normalize_host(raw_host)


def normalize_qb_config_payload(payload: QBConfig, existing_password: str = "") -> Dict[str, Any]:
    return get_qb_service().normalize_config_payload(payload, existing_password)


DEFAULT_NORMALIZE_QB_CONFIG_PAYLOAD = normalize_qb_config_payload


def safe_qb_config_port(raw_port: Any, default: int = 8080) -> int:
    return get_qb_service().safe_config_port(raw_port, default)


def safe_qb_config_error(exc: ValueError) -> str:
    return get_qb_service().safe_config_error(exc)


def get_scraper_allowed_hosts() -> set:
    return get_scraper_allowed_hosts_from_sources(
        getattr(scraper, "base_url", None),
        getattr(scraper, "all_domains", []),
    )


def normalize_scraper_url(raw_url: Any) -> Optional[str]:
    return normalize_scraper_url_for_sources(
        raw_url,
        base_url=getattr(scraper, "base_url", ""),
        all_domains=getattr(scraper, "all_domains", []),
    )


def resolve_movie_detail_url_for_download(
    raw_url: Any,
    *,
    movie_id: Any = None,
    title: Any = None,
) -> Optional[Dict[str, Optional[str]]]:
    return resolve_movie_detail_url_for_download_with_normalizers(
        raw_url,
        normalize_url=normalize_scraper_url,
        normalize_video_id=normalize_video_id_path_segment,
        movie_id=movie_id,
        title=title,
    )


def normalize_subscription_source_url(raw_url: Any) -> Optional[str]:
    return normalize_subscription_source_url_with_external(
        raw_url,
        normalize_external_url=normalize_scraper_url,
    )


def qb_config_payload() -> Dict[str, Any]:
    return get_qb_service().config_payload()


def qb_unavailable_response() -> Dict[str, Any]:
    return get_qb_service().unavailable_response()


QB_TORRENT_RESPONSE_FIELDS = {
    "hash",
    "name",
    "state",
    "progress",
    "size",
    "total_size",
    "dlspeed",
    "upspeed",
    "tags",
    "category",
    "added_on",
    "addedOn",
    "added_at",
    "eta",
    "ratio",
    "downloaded",
    "uploaded",
}


def sanitize_qb_torrent_for_response(torrent: Dict[str, Any]) -> Dict[str, Any]:
    return get_qb_service().sanitize_torrent_for_response(torrent)


def sanitize_qb_torrents_for_response(torrents: Any) -> List[Dict[str, Any]]:
    return get_qb_service().sanitize_torrents_for_response(torrents)


def build_qb_dashboard(session, base_url: str) -> Dict[str, Any]:
    return get_qb_service().build_dashboard(session, base_url)


# MARK: - API: System & Logs

@app.get("/")
async def read_root():
    # Serve the frontend index when available, otherwise return API info.
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "BusTerminal API 1.0"}

@app.get("/api/logs")
async def get_logs():
    # Return recent system logs.
    return [
        {
            **entry,
            "msg": redact_log_message(entry.get("msg")),
        }
        for entry in log_buffer
    ]


@app.get("/api/translation/status")
async def get_translation_status():
    """
    翻译进度总览：给设置页的"多语言翻译"面板用。

    全量去重统计要扫 1.4M 行（20~30 秒），所以由后台任务定期算好快照（
    config.translation_progress_snapshot），接口只读快照 + 几个轻量实时数字。
    """
    snapshot = db.get_config(TRANSLATION_PROGRESS_SNAPSHOT_KEY) or {}
    translated_total = await run_in_threadpool(db.count_translations, 600)
    translator = getattr(scraper, "ollama_translator", None)
    queue_size = scraper.pending_translation_queue_size()
    cursor = db.get_config("translation_backfill_cursor")

    # 待翻数量随翻译推进下降，用快照基数减掉已经翻完的部分
    pending_base = snapshot.get("pending_total")
    translated_at_snapshot = int(snapshot.get("translated_total") or 0)
    pending_total = None
    if pending_base is not None:
        pending_total = max(int(pending_base) - max(translated_total["total"] - translated_at_snapshot, 0), 0)

    rate = float(snapshot.get("rate_per_min") or 0) or round(translated_total["recent"] / 10.0, 1)
    payload = {
        "translated_total": translated_total["total"],
        "recent_total": translated_total["recent"],
        "recent_seconds": 600,
        "rate_per_min": rate,
        "pending_total": pending_total,
        "pending_base": pending_base,
        "by_year": snapshot.get("by_year") or [],
        "snapshot_at": snapshot.get("computed_at"),
        "queue_size": queue_size,
        "cursor": cursor,
        "provider": getattr(scraper, "provider", None),
        "model": getattr(translator, "model", None) if translator else None,
        "backfill_enabled": bool(config.TRANSLATION_BACKFILL_ENABLED),
        "eta_days": round(pending_total / rate / 60 / 24, 1) if (pending_total and rate > 0) else None,
    }
    return {"success": True, "data": payload}


@app.get("/api/tasks/status")
async def get_task_status():
    # Return background task status.
    data = task_registry.snapshot()
    try:
        data["external_queue"] = {
            "available": True,
            **await run_in_threadpool(db.get_external_task_stats),
        }
    except Exception as exc:
        data["external_queue"] = {
            "available": False,
            "error_type": safe_exception_label(exc),
        }
    try:
        data["metadata_queue"] = {
            "available": True,
            **await run_in_threadpool(
                db.get_actress_metadata_task_stats,
                ACTRESS_METADATA_MAX_ATTEMPTS,
            ),
        }
    except Exception as exc:
        data["metadata_queue"] = {
            "available": False,
            "error_type": safe_exception_label(exc),
        }
    return {"success": True, "data": data}


@app.get("/api/tasks/external")
async def get_external_tasks(
    status: Optional[str] = Query(
        None,
        pattern="^(pending|processing|waiting_external_service|needs_attention|completed)$",
    ),
    task_type: Optional[str] = Query(None, min_length=1, max_length=64, pattern="^[a-z][a-z0-9_]*$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    rows = await run_in_threadpool(db.get_external_tasks, status, task_type, limit, offset)
    public_rows = [
        {
            "id": row.get("id"),
            "task_type": row.get("task_type"),
            "source": redact_log_message(row.get("source")),
            "status": row.get("status"),
            "retry_count": int(row.get("retry_count") or 0),
            "max_retries": int(row.get("max_retries") or 0),
            "first_error_at": row.get("first_error_at"),
            "last_error_at": row.get("last_error_at"),
            "last_error": redact_log_message(row.get("last_error")),
            "next_retry_at": row.get("next_retry_at"),
            "result": sanitize_lifecycle_payload(row.get("result") or {}),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "completed_at": row.get("completed_at"),
        }
        for row in rows
    ]
    return {
        "success": True,
        "data": public_rows,
        "count": len(public_rows),
        "limit": limit,
        "offset": offset,
    }


def get_health_payload():
    return {"success": True, "status": "ok", "timestamp": time.time()}


def get_health_check_payload():
    schema = db.get_schema_migration_status()
    return {
        "success": True,
        "status": "degraded" if schema.get("migration_required") else "ok",
        "timestamp": time.time(),
        "database": {"schema": schema},
    }


def get_health_deps_payload():
    """辅助服务（备份 / 守卫）的心跳与备份产物新鲜度。

    只读 DATA_DIR/heartbeat 下的 JSON 文件，没有任何外部副作用，
    所以不受 BUS_TERMINAL_DISABLE_STARTUP_TASKS 限制（见下方 startup_event）。
    """
    return health_deps.evaluate(directory=health_deps.heartbeat_dir())


def health_deps_alert_loop():
    """周期检查辅助服务心跳，异常时按 key 去重 + 冷却后告警。

    NAS 上已经没有任何监控，备份又是典型的静默失败场景，所以这条看护
    **不能被 BUS_TERMINAL_DISABLE_STARTUP_TASKS 关掉** —— 那个开关的本意是压住
    启动时的爬虫/预热，不该把健康看护一起挡住（与 groom/dispatcher 同理）。
    """
    while not stop_event.is_set():
        try:
            report = get_health_deps_payload()
            issues = report.get("issues") or []
            for issue in issues:
                logger.warning("Health deps unavailable: %s", issue)
            sent = health_deps.dispatch_health_alerts(
                report["alerts"],
                cooldown_seconds=config.HEALTH_ALERT_COOLDOWN_SECONDS,
                state_path=os.path.join(health_deps.heartbeat_dir(),
                                        health_deps.ALERT_STATE_FILENAME),
                sender=notifier.notifier.send_system_alert,
                logger=logger,
            )
            if sent:
                logger.warning("Health deps alerts sent: %s",
                               ", ".join(alert["key"] for alert in sent))
            task_registry.mark_success(
                "health_deps_alert_loop",
                summary={
                    "status": report["status"],
                    "alerts": len(report["alerts"]),
                    "sent": len(sent),
                },
            )
        except Exception as e:
            logger.error("Health deps check failed: %s", safe_exception_label(e))
            task_registry.mark_error("health_deps_alert_loop", e)

        stop_event.wait(config.HEALTH_DEPS_CHECK_SECONDS)


# MARK: - API: Auth

class AuthCredentials(BaseModel):
    # Login/setup credentials.
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=512)


@app.get("/api/auth/status")
async def auth_status():
    return {
        "success": True,
        "data": {
            "auth_required": auth_service.auth_required(),
            "setup_required": not db.has_any_user(),
        },
    }


@app.post("/api/auth/setup")
async def auth_setup(credentials: AuthCredentials):
    username = credentials.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="invalid_username")
    if len(credentials.password) < 8:
        raise HTTPException(status_code=400, detail="password_too_short")
    if db.has_any_user():
        raise HTTPException(status_code=403, detail="setup_already_completed")
    created = await run_in_threadpool(db.create_user, username, hash_password(credentials.password))
    if not created:
        raise HTTPException(status_code=403, detail="setup_already_completed")
    logger.info("Initial admin user created: %s", username)
    return {"success": True, "data": {"token": auth_service.create_token(username)}}


@app.post("/api/auth/login")
async def auth_login(credentials: AuthCredentials, request: Request):
    username = credentials.username.strip()
    rate_key = auth_service.login_rate_limit_key(request, username)
    if auth_service.is_login_rate_limited(rate_key):
        raise HTTPException(status_code=429, detail="too_many_attempts")
    stored_hash = await run_in_threadpool(db.get_user_password_hash, username)
    if not stored_hash or not verify_password(credentials.password, stored_hash):
        auth_service.record_login_failure(rate_key)
        logger.warning("Login failed for user: %s", username)
        raise HTTPException(status_code=401, detail="invalid_credentials")
    auth_service.clear_login_failures(rate_key)
    logger.info("Login success for user: %s", username)
    return {"success": True, "data": {"token": auth_service.create_token(username)}}


@app.get("/api/health/live")
async def health_live():
    return get_health_payload()


@app.get("/api/health/check")
async def health_check():
    return await run_in_threadpool(get_health_check_payload)


@app.get("/api/health/deps")
async def health_dependencies():
    """辅助服务心跳 + 备份产物新鲜度（免鉴权，供容器探针与外部健康检查用）。"""
    return await run_in_threadpool(get_health_deps_payload)


# ============ amane 对接（本地媒体库执行层） ============
# BusTerminal 不碰文件：扫描 / 整理 / 刮削 / 清理全部经 amane 的 API 触发并回读状态。
_amane_service: Optional[AmaneService] = None


def get_amane_service() -> AmaneService:
    global _amane_service
    if _amane_service is None:
        _amane_service = AmaneService(db=db, logger=logger, safe_exception_label=safe_exception_label)
    return _amane_service


_amane_ingest_service: Optional[AmaneIngestService] = None
_amane_ingest_inflight: set = set()
_amane_ingest_inflight_lock = threading.Lock()


def get_amane_ingest_service() -> AmaneIngestService:
    global _amane_ingest_service
    if _amane_ingest_service is None:
        _amane_ingest_service = AmaneIngestService(
            db=db,
            amane_service=get_amane_service(),
            logger=logger,
            safe_exception_label=safe_exception_label,
            record_lifecycle_event=lambda **kwargs: db.record_lifecycle_event(**kwargs),
            jellyfin_client=jellyfin_client,
        )
    return _amane_ingest_service


_amane_heal_service: Optional[AmaneHealService] = None


def get_amane_heal_service() -> AmaneHealService:
    """入库自愈服务：绕开 amane refresh 失败，把滞留影片直接补号 + 刮削 + 整理。"""
    global _amane_heal_service
    if _amane_heal_service is None:
        _amane_heal_service = AmaneHealService(
            db=db,
            amane_service=get_amane_service(),
            logger=logger,
            safe_exception_label=safe_exception_label,
            display_value_for_log=display_value_for_log,
            config=config,
        )
    return _amane_heal_service


def run_amane_ingest(movie_id: str, *, title: Optional[str] = None, source: str = "qb_download_completed") -> None:
    """后台执行一次入库闭环（不阻塞下载巡检循环）。"""
    with _amane_ingest_inflight_lock:
        if movie_id in _amane_ingest_inflight:
            return
        _amane_ingest_inflight.add(movie_id)
    try:
        trace = get_amane_ingest_service().ingest(movie_id, title=title, source=source)
        logger.info(
            "amane ingest %s -> %s (%s)",
            movie_id, trace.get("organize_status"), trace.get("reason") or trace.get("error") or "ok",
        )
    except Exception as exc:
        logger.warning("amane ingest crashed for %s: %s", movie_id, safe_exception_label(exc))
    finally:
        with _amane_ingest_inflight_lock:
            _amane_ingest_inflight.discard(movie_id)


def schedule_amane_ingest(movie_id: str, *, title: Optional[str] = None) -> bool:
    """下载完成钩子：amane 未配置时静默跳过，避免刷日志。

    返回是否真的调度了入库线程；调用方据此决定是否打 Delivered 标签，
    免得 amane 没配置时种子被永久挂起。
    """
    if not movie_id:
        return False
    try:
        if not get_amane_service().get_settings()["configured"]:
            return False
    except Exception:
        return False
    threading.Thread(
        target=run_amane_ingest,
        args=(movie_id,),
        kwargs={"title": title},
        name=f"amane-ingest-{movie_id}",
        daemon=True,
    ).start()
    return True


def raise_amane_http_error(exc: AmaneError) -> None:
    """把 amane 客户端异常翻译成对外的 HTTP 语义。"""
    if isinstance(exc, (AmaneNotConfigured, AmaneUnavailable)):
        status = 503
    else:
        status = 502
    detail = exc.code
    if isinstance(exc, AmaneApiError):
        detail = f"{exc.code}:{exc.status_code}"
    logger.warning("amane request failed: %s (%s)", detail, exc.message)
    raise HTTPException(status_code=status, detail=detail)


@app.get("/api/amane/status")
async def get_amane_status():
    """amane 连通性 / 鉴权 / 库数量快照。"""
    return {"success": True, "data": await run_in_threadpool(get_amane_service().get_status)}


@app.get("/api/amane/config")
async def get_amane_config():
    return {"success": True, "data": await run_in_threadpool(get_amane_service().get_settings)}


@app.post("/api/amane/config")
async def update_amane_config(payload: AmaneConfigPayload):
    service = get_amane_service()
    data = await run_in_threadpool(
        lambda: service.save_settings(base_url=payload.base_url, token=payload.token)
    )
    return {"success": True, "data": data}


@app.get("/api/amane/libraries")
async def list_amane_libraries(force: bool = False):
    service = get_amane_service()
    try:
        libraries = await run_in_threadpool(lambda: service.list_libraries(force=force))
    except AmaneError as exc:
        raise_amane_http_error(exc)
    return {"success": True, "data": {"items": libraries, "total": len(libraries)}}


@app.get("/api/amane/inventory")
async def get_amane_inventory(force: bool = False):
    """库配置盘点：每个库的现状 + 按入库要求给出的待改项。"""
    service = get_amane_service()
    try:
        data = await run_in_threadpool(lambda: service.get_inventory(force=force))
    except AmaneError as exc:
        raise_amane_http_error(exc)
    return {"success": True, "data": data}


@app.get("/api/amane/selfcheck")
async def get_amane_selfcheck():
    """媒体栈自检：链路上哪一环没通。"""
    return {"success": True, "data": await run_in_threadpool(get_amane_service().get_selfcheck)}


@app.get("/api/amane/tasks")
async def list_amane_tasks(limit: int = 20, status: Optional[str] = None):
    service = get_amane_service()
    statuses = [s.strip() for s in str(status or "").split(",") if s.strip()] or None
    try:
        data = await run_in_threadpool(lambda: service.list_tasks(limit=limit, status=statuses))
    except AmaneError as exc:
        raise_amane_http_error(exc)
    return {"success": True, "data": data}


@app.get("/api/amane/tasks/{task_id}")
async def get_amane_task(task_id: int):
    service = get_amane_service()
    try:
        data = await run_in_threadpool(service.get_task, task_id)
    except AmaneError as exc:
        raise_amane_http_error(exc)
    return {"success": True, "data": data}


@app.post("/api/amane/tasks/refresh")
async def submit_amane_refresh(payload: AmaneRefreshPayload):
    """触发 amane 扫描指定库（默认只新增 + 刮削 pending 条目）。"""
    service = get_amane_service()
    try:
        data = await run_in_threadpool(
            lambda: service.trigger_refresh(
                payload.library_id,
                path=payload.path,
                scan=payload.scan,
                scrape=payload.scrape,
                wait=payload.wait,
            )
        )
    except AmaneError as exc:
        raise_amane_http_error(exc)
    return {"success": True, "data": data}


@app.post("/api/amane/tasks/organize")
async def submit_amane_organize(payload: AmaneOrganizePayload):
    """触发 amane 整理（改名 / 归档 / 写 NFO）。"""
    service = get_amane_service()
    try:
        data = await run_in_threadpool(
            lambda: service.trigger_organize(
                payload.library_id,
                path=payload.path,
                media_file_ids=payload.media_file_ids,
                wait=payload.wait,
            )
        )
    except AmaneError as exc:
        raise_amane_http_error(exc)
    return {"success": True, "data": data}


@app.post("/api/amane/tasks/scrape")
async def submit_amane_scrape(payload: AmaneScrapePayload):
    """按番号或 media_id 触发 amane 刮削。"""
    service = get_amane_service()
    try:
        data = await run_in_threadpool(
            lambda: service.trigger_scrape(
                number=payload.number,
                media_id=payload.media_id,
                content_type=payload.content_type,
                wait=payload.wait,
            )
        )
    except AmaneError as exc:
        raise_amane_http_error(exc)
    return {"success": True, "data": data}


@app.get("/api/amane/ingest/config")
async def get_amane_ingest_config():
    return {"success": True, "data": await run_in_threadpool(get_amane_ingest_service().get_config)}


@app.post("/api/amane/ingest/config")
async def update_amane_ingest_config(payload: AmaneIngestConfigPayload):
    service = get_amane_ingest_service()
    data = await run_in_threadpool(
        lambda: service.save_config(**payload.dict(exclude_unset=True))
    )
    return {"success": True, "data": data}


@app.post("/api/amane/ingest")
async def run_amane_ingest_endpoint(payload: AmaneIngestPayload, wait: bool = True):
    """执行一次入库闭环：扫描 → 刮削 → 整理 → 回写生命周期。"""
    service = get_amane_ingest_service()
    trace = await run_in_threadpool(
        lambda: service.ingest(
            payload.movie_id,
            number=payload.number,
            title=payload.title,
            wait=wait,
            force=bool(payload.force),
            source="api",
        )
    )
    return {"success": trace.get("organize_status") == "completed", "data": trace}


@app.post("/api/amane/ingest/visibility")
async def confirm_amane_ingest_visibility(payload: AmaneIngestPayload, refresh: bool = True):
    """闭环收尾：触发 Jellyfin 扫描并确认该片可见（入库后补跑或人工确认）。"""
    service = get_amane_ingest_service()
    data = await run_in_threadpool(
        lambda: service.confirm_visibility(
            payload.movie_id, number=payload.number, title=payload.title,
            refresh=refresh, source="api",
        )
    )
    return {"success": data.get("status") == "visible", "data": data}


@app.get("/api/amane/presence")
async def check_amane_library_presence(movie_id: str, number: Optional[str] = None):
    """查一部片是不是已经在 amane / Jellyfin 库里（下载前的去重依据）。"""
    data = await run_in_threadpool(
        lambda: get_amane_ingest_service().check_library_presence(movie_id, number=number)
    )
    return {"success": True, "data": data}


@app.post("/api/amane/ingest/batch")
async def run_amane_ingest_batch(payload: AmaneIngestBatchPayload, wait: bool = True):
    service = get_amane_ingest_service()
    # payload.items 是 Pydantic 模型，ingest_many 内部按 dict 取值（item.get），
    # 不转成 dict 会直接抛 AttributeError，批量端点等于完全不可用。
    items = [item.model_dump() for item in payload.items]
    data = await run_in_threadpool(lambda: service.ingest_many(items, wait=wait))
    return {"success": data.get("failed") == 0, "data": data}


@app.get("/api/amane/media")
async def search_amane_media(number: str = "", limit: int = 20):
    """按番号查 amane 已索引的媒体文件，判断某片是否真的进了库。"""
    if not number.strip():
        raise HTTPException(status_code=400, detail="number_required")
    service = get_amane_service()
    try:
        data = await run_in_threadpool(lambda: service.find_media(number, limit=limit))
    except AmaneError as exc:
        raise_amane_http_error(exc)
    return {"success": True, "data": data}


@app.get("/api/amane/stuck")
async def list_stuck_amane_movies(
    limit: int = Query(20, ge=1, le=200),
    older_than_seconds: int = Query(7200, ge=60),
):
    """列出投递很久却一直没整理入库的影片（只读检测，不改任何数据）。"""
    data = await run_in_threadpool(
        lambda: get_amane_heal_service().detect_stuck(
            limit=limit, older_than_seconds=older_than_seconds
        )
    )
    return {"success": True, "data": data, "count": len(data)}


@app.post("/api/amane/heal")
async def heal_stuck_amane_movies(
    limit: int = Query(10, ge=1, le=100),
    older_than_seconds: int = Query(7200, ge=60),
    dry_run: bool = Query(True),
):
    """批量自愈滞留影片：按番号定位 media_file → 补号 → 刮削 → 整理。

    绕开 amane 的 refresh 失败（`UNIQUE constraint failed: media_files.path`）。
    `dry_run` 默认 True，只报告会处理哪些，不写 amane；确认后再传 dry_run=false 执行。
    """
    data = await run_in_threadpool(
        lambda: get_amane_heal_service().heal_batch(
            limit=limit, older_than_seconds=older_than_seconds, dry_run=dry_run
        )
    )
    return {"success": True, "data": data}


@app.post("/api/amane/heal/{movie_id}")
async def heal_one_amane_movie(movie_id: str, number: Optional[str] = None):
    """自愈单部影片（明确指定了 movie_id，直接执行；批量接口请用 /api/amane/heal）。"""
    data = await run_in_threadpool(
        lambda: get_amane_heal_service().heal_one(movie_id, number=number)
    )
    return {"success": bool(data.get("healed")), "data": data}


@app.get("/api/config/system")
async def get_system_config():
    return {"success": True, "data": await run_in_threadpool(get_system_config_payload)}


@app.post("/api/config/system")
async def update_system_config(payload: SystemConfig):
    # 粘贴型字段先清洗（实测生产存进过带 `;;;;;` 的 API Key，导致 Jellyfin 一直 401）
    data = sanitize_pasted_config_payload(payload.dict(exclude_unset=True))
    errors = validate_system_config_payload(data)
    if errors:
        return {"success": False, "message": "invalid_system_config", "errors": errors}

    automation_keys = {
        "automation_mode",
        "auto_follow_enabled",
        "auto_subscribe_enabled",
        "auto_download_enabled",
        "auto_cleanup_enabled",
        "jellyfin_auto_approve_candidates",
    }
    automation_updates = {key: value for key, value in data.items() if key in automation_keys}
    for key, value in data.items():
        if key in automation_keys:
            continue
        if should_skip_sensitive_config_update(key, value):
            continue
        await run_in_threadpool(db.set_config, key, value)
    if automation_updates:
        await run_in_threadpool(get_automation_service().update_settings, automation_updates)

    await run_in_threadpool(scraper.reload_config)
    await run_in_threadpool(jellyfin_client.reload_config)
    await run_in_threadpool(notifier.notifier.reload_config)
    return {"success": True, "data": await run_in_threadpool(get_system_config_payload)}


# ============ 首次部署引导（onboarding） ============
# 上线后第一次打开前端时把基础配置一次填完：接口与授权 / 下载习惯 / 屏蔽关键词 /
# 缓存策略 / 是否接入现有数据库 / Jellyfin 自动刮削补齐。
# 强拦截由前端 SetupGate 负责，这里只提供状态与写入能力。

_onboarding_service: Optional[OnboardingService] = None


def _probe_qb_for_onboarding() -> Dict[str, Any]:
    """qBittorrent 只读探针：取一次服务端状态，不做任何写操作。"""
    qb_service = get_qb_service()
    config_payload = qb_service.config_payload()
    if not config_payload.get("host"):
        return {"ok": False, "configured": False, "message": "qb_not_configured"}
    status = qb_service.get_status()
    connected = bool(status.get("connected"))
    return {
        "ok": connected,
        "configured": True,
        "message": status.get("message") or ("ok" if connected else "qb_unreachable"),
        "version": status.get("version") or "",
    }


def _probe_jellyfin_for_onboarding() -> Dict[str, Any]:
    """Jellyfin 只读探针：取服务器信息。"""
    jellyfin_client.reload_config()
    if not jellyfin_client.host or not jellyfin_client.api_key:
        return {"ok": False, "configured": False, "message": "jellyfin_not_configured"}
    info = get_jellyfin_service().probe_server_info(True)
    if not info:
        return {"ok": False, "configured": True, "message": "jellyfin_unreachable"}
    return {
        "ok": True,
        "configured": True,
        "message": "ok",
        "server_name": info.get("ServerName") or info.get("server_name") or "",
        "version": info.get("Version") or info.get("version") or "",
    }


def _probe_amane_for_onboarding() -> Dict[str, Any]:
    """amane 只读探针：复用媒体栈自检的连通性判断。"""
    status = get_amane_service().get_status()
    if not status.get("configured"):
        return {"ok": False, "configured": False, "message": "amane_not_configured"}
    return {
        "ok": bool(status.get("ok", True)),
        "configured": True,
        "message": status.get("message") or "ok",
        "library_count": status.get("library_count"),
    }


def _probe_translation_for_onboarding(payload: Dict[str, Any]) -> Dict[str, Any]:
    """翻译服务只读探针：翻一句话，确认授权真的可用。

    传了 provider/base_url 时先临时覆盖再看结果，方便用户在保存前就验证填的值对不对。
    """
    provider = str(payload.get("provider") or "").strip()
    if not provider:
        provider = str(db.get_config("translation_provider") or config.TRANSLATION_PROVIDER)
    if provider not in {"google_free", "baidu_dev", "ollama_local"}:
        return {"ok": False, "message": "translation_provider_unsupported"}

    if provider == "ollama_local":
        base_url = str(payload.get("base_url") or db.get_config("ollama_base_url") or config.OLLAMA_BASE_URL).rstrip("/")
        model = str(payload.get("model") or db.get_config("ollama_model") or config.OLLAMA_MODEL)
        url = f"{base_url}/api/tags"
        try:
            resp = internal_session().get(url, timeout=8)
            if resp.status_code != 200:
                return {"ok": False, "message": f"ollama_http_{resp.status_code}", "provider": provider}
            models = [m.get("name") for m in (resp.json().get("models") or []) if isinstance(m, dict)]
            return {
                "ok": True,
                "provider": provider,
                "message": "ok",
                "model_present": model in models,
                "model": model,
                "model_count": len(models),
            }
        except Exception as exc:
            return {"ok": False, "provider": provider, "message": safe_exception_label(exc)}

    if provider == "baidu_dev":
        app_id = str(payload.get("baidu_app_id") or db.get_config("baidu_app_id") or config.BAIDU_APP_ID)
        secret = str(payload.get("baidu_secret") or db.get_config("baidu_secret") or config.BAIDU_SECRET)
        if not app_id or not secret:
            return {"ok": False, "provider": provider, "message": "baidu_credentials_missing"}
        return {"ok": True, "provider": provider, "message": "credentials_present"}

    # google_free 无需授权，只要不是被墙死就算通
    try:
        resp = internal_session().get("https://translate.googleapis.com", timeout=8)
        return {
            "ok": resp.status_code < 500,
            "provider": provider,
            "message": "ok" if resp.status_code < 500 else f"http_{resp.status_code}",
        }
    except Exception as exc:
        return {"ok": False, "provider": provider, "message": safe_exception_label(exc)}


def get_onboarding_service() -> OnboardingService:
    global _onboarding_service
    if _onboarding_service is None:
        _onboarding_service = OnboardingService(
            db=db,
            config=config,
            logger=logger,
            is_valid_http_url=is_valid_http_config_url,
            normalize_qb_root_path=normalize_qb_root_path,
            probe_qb=_probe_qb_for_onboarding,
            probe_jellyfin=_probe_jellyfin_for_onboarding,
            probe_amane=_probe_amane_for_onboarding,
            probe_translation=_probe_translation_for_onboarding,
            config_payload_provider=lambda: get_qb_service().config_payload(),
            save_qb_config=lambda section: get_qb_service().save_config(
                _as_qb_config_payload(section)
            ),
        )
    return _onboarding_service


def _as_qb_config_payload(section: Dict[str, Any]) -> QBConfig:
    """把引导页的 qB 表单段转成 QBConfig，host 归一化交给 qb_service 自己校验。"""
    try:
        port = int(section.get("port") or 8080)
    except (TypeError, ValueError):
        port = 8080
    return QBConfig(
        host=str(section.get("host") or ""),
        port=port,
        username=str(section.get("username") or ""),
        password=str(section.get("password") or ""),
    )


@app.get("/api/onboarding/status")
async def get_onboarding_status(include_settings: bool = Query(False, description="是否附带完整配置（仅引导页需要）")):
    """引导状态。免鉴权：首次部署时还没有任何用户，前端必须能读到。

    ⚠️ 默认**只回引导标记**，不附带完整配置。
    这个接口是 SetupGate 在**每个页面**加载时都会调的，早期版本附带
    current_settings()（读 40+ 个配置键），把全站首屏拖慢了好几秒。
    只有引导页自己需要回填表单时才显式带 include_settings=true。
    """
    service = get_onboarding_service()
    data = await run_in_threadpool(service.status)
    if include_settings:
        data["settings"] = await run_in_threadpool(service.current_settings)
    return {"success": True, "data": data}


@app.post("/api/onboarding/apply")
async def apply_onboarding(payload: Dict[str, Any] = Body(...)):
    """一次性写入引导表单。免鉴权的原因同 status：首次部署时还没有用户。"""
    data = await run_in_threadpool(
        lambda: get_onboarding_service().apply(
            payload if isinstance(payload, dict) else {},
            completed=bool(payload.get("completed", True)) if isinstance(payload, dict) else True,
        )
    )
    if not data.get("success"):
        return {"success": False, "message": "invalid_onboarding_payload", "errors": data.get("errors", [])}

    # 写入后立刻让运行时重载，否则 qB / Jellyfin / 代理仍用旧配置
    await run_in_threadpool(scraper.reload_config)
    await run_in_threadpool(jellyfin_client.reload_config)
    await run_in_threadpool(get_amane_service().reload_config)
    return {"success": True, "data": data}


@app.post("/api/onboarding/test/{target}")
async def test_onboarding_target(target: str, payload: Dict[str, Any] = Body(default={})):
    """连通性测试（只读）。target: qb / jellyfin / amane / translation。"""
    result = await run_in_threadpool(
        lambda: get_onboarding_service().test_target(
            str(target or "").strip().lower(), payload if isinstance(payload, dict) else {}
        )
    )
    return {"success": True, "data": result}


@app.post("/api/onboarding/database/check")
async def check_onboarding_database(payload: Dict[str, Any] = Body(default={})):
    """数据库只读体检 + 与期望接入库的比对（运行期不可切库，如实反馈）。"""
    expected = payload.get("expected_url") if isinstance(payload, dict) else None
    data = await run_in_threadpool(lambda: get_onboarding_service().database_check(expected))
    return {"success": True, "data": data}


@app.post("/api/onboarding/block-keywords/preview")
async def preview_block_keywords(payload: Dict[str, Any] = Body(default={})):
    """按填好的屏蔽词试算：从本地库里捞出会被挡掉的条目，让用户保存前先看效果。"""
    raw = payload.get("block_keywords") if isinstance(payload, dict) else None
    limit = 20
    if isinstance(payload, dict):
        try:
            limit = max(1, min(int(payload.get("limit") or 20), 50))
        except (TypeError, ValueError):
            limit = 20

    def run_preview() -> Dict[str, Any]:
        service = get_onboarding_service()
        saved = service.db.get_config(onboarding_service.BLOCK_KEYWORDS_KEY)
        keywords = onboarding_service.parse_block_keywords(raw)
        # 试算期间临时把候选词写进 config，让 is_blocked/filter 用到同一份词
        if keywords != onboarding_service.parse_block_keywords(saved):
            service.db.set_config(
                onboarding_service.BLOCK_KEYWORDS_KEY, ",".join(keywords)
            )
        try:
            matched = service.preview_blocked_movies(limit)
            return {"keywords": keywords, "items": matched}
        finally:
            service.db.set_config(
                onboarding_service.BLOCK_KEYWORDS_KEY, ",".join(onboarding_service.parse_block_keywords(saved))
            )

    data = await run_in_threadpool(run_preview)
    return {"success": True, "data": data}


@app.post("/api/onboarding/reset")
async def reset_onboarding():
    """解除拦截重新走引导。只清完成标记，已写入的业务配置保持不变。"""
    data = await run_in_threadpool(get_onboarding_service().reset)
    return {"success": True, "data": data}


# ============ BI 看板（每日两次快照） ============
# 聚合要 2 秒（145 万本地库 + 分页读 Jellyfin），不能放在页面请求路径上。
# 按需求每天只算两次（11:30 / 18:00），结果落 config 表，接口只读快照。

_bi_snapshot_service: Optional[BISnapshotService] = None

# 进程启动时刻，用于算运行时长
_PROCESS_STARTED_AT = time.time()


def get_process_uptime_hours() -> Optional[float]:
    """运行时长（小时）。取不到就返回 None，由页面显示「暂不可用」。"""
    try:
        return (time.time() - _PROCESS_STARTED_AT) / 3600.0
    except Exception:
        return None


def bi_snapshot_loop():
    """BI 快照巡检：每天命中 11:30 / 18:00 时各刷新一次。"""
    return get_bi_snapshot_service().loop(stop_event)


def get_bi_snapshot_service() -> BISnapshotService:
    global _bi_snapshot_service
    if _bi_snapshot_service is None:
        _bi_snapshot_service = BISnapshotService(
            db=db,
            builder=lambda **kwargs: BIDashboardService(
                db=db, config=config, logger=logger, jellyfin_client=jellyfin_client
            ).build(**kwargs),
            logger=logger,
            uptime_provider=get_process_uptime_hours,
        )
    return _bi_snapshot_service


@app.get("/api/bi/overview")
async def get_bi_overview(refresh: bool = Query(False, description="强制现算并更新快照")):
    """BI 看板数据。**默认只读快照**（秒开，不触发任何重算）；
    只有显式 refresh=true（如设置页点「立即重算」）才现算。"""
    service = get_bi_snapshot_service()
    if refresh:
        data = await run_in_threadpool(service.refresh, reason="manual")
    else:
        data = await run_in_threadpool(service.get_payload, allow_refresh=False)
    return {"success": True, "data": data}


@app.get("/api/bi/settings")
async def get_bi_settings():
    """看板刷新频率设置（只读）。"""
    service = get_bi_snapshot_service()
    mode = service.read_mode()
    return {
        "success": True,
        "data": {
            "refresh_mode": mode,
            "refresh_slots": service.slot_labels(mode),
            "modes": list(BI_REFRESH_MODES),
        },
    }


@app.post("/api/bi/settings")
async def update_bi_settings(payload: dict = Body(...)):
    """调整看板刷新频率：daily（每天一次）/ twice（每天两次）/ realtime（打开即算）。"""
    mode = str((payload or {}).get("refresh_mode") or "").strip()
    if mode not in BI_REFRESH_MODES:
        return {"success": False, "message": "invalid_refresh_mode"}
    await run_in_threadpool(db.set_config, BI_REFRESH_MODE_KEY, mode)
    service = get_bi_snapshot_service()
    # 频率变了，当天的汇总节奏也跟着变，立刻重算一次免得等到下一个时刻
    data = await run_in_threadpool(service.refresh, reason="mode_changed")
    return {"success": True, "data": {"refresh_mode": mode, "snapshot": data}}


@app.post("/api/jellyfin/play/{item_id}")
async def play_jellyfin_item_mpv(item_id: str, player: str = "mpv"):
    """按指定播放器播放 Jellyfin 条目。"""
    service = get_jellyfin_service()
    return await service.play_item_mpv(item_id, player=player)

@app.get("/api/jellyfin/stream/{item_id}")
async def proxy_jellyfin_stream(item_id: str, request: Request):
    """通过后端代理转发 Jellyfin 视频流，避免把 API Key 暴露给浏览器。"""
    jf_service = get_jellyfin_service()
    if jf_service.is_circuit_open():
        raise HTTPException(status_code=503, detail="jellyfin_unavailable")

    item_id = normalize_item_id(item_id)
    if not item_id:
        raise HTTPException(status_code=400, detail="invalid_jellyfin_item_id")

    jf_service.jellyfin_client.reload_config()
    host = (jf_service.jellyfin_client.host or "").rstrip('/')
    api_key = jf_service.jellyfin_client.api_key or ""
    if not host or not api_key:
        raise HTTPException(status_code=503, detail="jellyfin_not_configured")

    upstream_url = f"{host}/Videos/{item_id}/stream.mp4?static=true"
    upstream_headers = {
        "X-Emby-Token": api_key,
        "Accept": request.headers.get("accept", "*/*"),
        "User-Agent": request.headers.get("user-agent", "Mozilla/5.0"),
    }
    if request.headers.get("range"):
        upstream_headers["Range"] = request.headers["range"]

    def _open_stream():
        import requests
        return internal_session().get(
            upstream_url,
            headers=upstream_headers,
            stream=True,
            timeout=(5, 60),
            allow_redirects=True,
        )

    upstream = await run_in_threadpool(_open_stream)
    if upstream.status_code not in (200, 206):
        detail = "jellyfin_stream_failed"
        try:
            upstream.close()
        except Exception:
            pass
        raise HTTPException(status_code=upstream.status_code, detail=detail)

    response_headers = {}
    for header_name in (
        "content-type",
        "content-length",
        "accept-ranges",
        "content-range",
        "content-disposition",
        "last-modified",
        "etag",
        "cache-control",
    ):
        value = upstream.headers.get(header_name)
        if value:
            response_headers[header_name] = value

    def _iter_chunks():
        try:
            for chunk in upstream.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    yield chunk
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    return StreamingResponse(
        _iter_chunks(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers=response_headers,
    )


@app.get("/api/jellyfin/sessions")
async def list_jellyfin_sessions():
    """诊断接口：列出所有活跃的 Jellyfin Sessions，用于排查 mpv-shim 注册情况。"""
    from starlette.concurrency import run_in_threadpool
    jellyfin_client.reload_config()
    host = (jellyfin_client.host or "").rstrip('/')
    api_key = jellyfin_client.api_key or ""
    if not host or not api_key:
        return {"success": False, "message": "Jellyfin 未配置"}

    def _fetch():
        import requests as req
        resp = req.get(
            f"{host}/Sessions",
            headers={"X-Emby-Token": api_key, "Accept": "application/json"},
            timeout=5,
        )
        if resp.status_code != 200:
            return []
        return [
            {
                "id": s.get("Id"),
                "client": s.get("Client"),
                "device_name": s.get("DeviceName"),
                "user": s.get("UserName"),
                "supports_media_control": s.get("SupportsMediaControl"),
            }
            for s in (resp.json() if isinstance(resp.json(), list) else [])
        ]

    sessions = await run_in_threadpool(_fetch)
    return {"success": True, "data": sessions}


@app.get("/api/qb/config")
async def get_qb_config():
    return {"success": True, "data": await run_in_threadpool(qb_config_payload)}


@app.post("/api/qb/config")
async def update_qb_config(payload: QBConfig):
    try:
        data = await run_in_threadpool(sync_qb_service_runtime_dependencies().save_config, payload)
    except ValueError as exc:
        return {"success": False, "message": safe_qb_config_error(exc)}
    return {"success": True, "data": data}


@app.get("/api/qb/status")
async def get_qb_status():
    return await run_in_threadpool(sync_qb_service_runtime_dependencies().get_status)


@app.get("/api/qb/dashboard")
async def get_qb_dashboard():
    return await run_in_threadpool(sync_qb_service_runtime_dependencies().get_dashboard)


@app.get("/api/qb/torrents")
async def get_qb_torrents(
    category: Optional[str] = Query(None),
    limit: int = Query(2000, ge=1, le=5000),
    offset: int = Query(0, ge=0, le=100000),
):
    return await run_in_threadpool(sync_qb_service_runtime_dependencies().get_torrents, category, limit, offset)


@app.post("/api/qb/torrents/control")
async def control_qb_torrents(payload: QBTorrentControlRequest):
    return await run_in_threadpool(sync_qb_service_runtime_dependencies().control_torrents, payload)


@app.post("/api/qb/torrents/filter-jav-files")
async def filter_qb_jav_files():
    return await run_in_threadpool(sync_qb_service_runtime_dependencies().filter_jav_files)


@app.get("/api/navigation")
async def get_navigation(force_refresh: bool = Query(False)):
    data = await run_in_threadpool(scraper.get_navigation, force_refresh)
    return {"success": True, "data": data}


@app.get("/api/genres")
async def get_genres(type: str = Query("censored"), force_refresh: bool = Query(False)):
    genre_type = str(type or "").strip().lower()
    if genre_type not in {"censored", "uncensored"}:
        raise HTTPException(status_code=400, detail="invalid_genre_type")
    data = await run_in_threadpool(
        scraper.get_genres,
        genre_type == "uncensored",
        force_refresh,
    )
    return {"success": True, "data": data}


@app.get("/api/movies")
async def get_movies(
    url: str = Query(...),
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=120),
    force_refresh: bool = Query(False),
    translate_mode: str = Query("full"),
):
    perform_translate = translate_mode != "none"
    safe_url = normalize_scraper_url(url)
    if not safe_url:
        return {"success": False, "message": "invalid_scraper_url", "data": [], "count": 0}
    movies = await run_in_threadpool(
        scraper.get_movies,
        safe_url,
        page,
        limit,
        force_refresh,
        perform_translate,
        translate_mode,
    )
    # 页面只展示处理完的数据：未翻译条目先不下发，并插队翻译，刷新后可见
    movies, pending_translation_count = await run_in_threadpool(
        scraper.filter_processed_movies, movies
    )
    schedule_image_cache_warmup(movies, "movies")
    # 全链路屏蔽词：命中的条目连展示都不给（下载侧另有 evaluate_download_permission 兜底）
    movies, blocked_count = get_onboarding_service().filter_blocked_movies(movies)
    return {
        "success": True,
        "data": movies,
        "count": len(movies),
        "pending_translation_count": pending_translation_count,
        "blocked_count": blocked_count,
    }


@app.get("/api/detail")
async def get_movie_detail(
    url: str = Query(...),
    movie_id: Optional[str] = Query(None, max_length=128),
    title: Optional[str] = Query(None, max_length=500),
):
    # 非 JavBus 来源（Jable/JavDB/AirAV）的详情链接靠番号回查到 JavBus 详情页，
    # 不能直接在门口按域名拒绝
    resolved = resolve_movie_detail_url_for_download(url, movie_id=movie_id, title=title)
    if not resolved:
        return {"success": False, "message": "movie_code_not_found", "data": {}}
    safe_url = resolved["url"]
    # 详情页策略：库里已存的结构化详情（detail_json）直接渲染，标题沿用已有译文，
    # 不必每次都去原站抓一遍（r18.dev 源实测要 48 秒）。库里没有才回退实时抓取。
    detail = await run_in_threadpool(
        scraper.get_movie_detail_cached_first, safe_url, resolved.get("movie_id")
    )
    if detail is None:
        detail = await run_in_threadpool(scraper.get_movie_detail, safe_url, False)
    queue_actress_metadata_from_detail(detail, "movie_detail")
    return {"success": True, "data": detail}


@app.get("/api/magnets/select")
async def select_magnet(
    url: str = Query(...),
    preference: str = Query("1080P"),
    movie_id: Optional[str] = Query(None, max_length=128),
    title: Optional[str] = Query(None, max_length=500),
):
    # 同 /api/detail：允许非 JavBus 来源链接走番号回查
    resolved = resolve_movie_detail_url_for_download(url, movie_id=movie_id, title=title)
    if not resolved:
        return {"success": False, "message": "movie_code_not_found", "data": None, "magnets": []}
    safe_url = resolved["url"]
    # 同 /api/detail：先吃缓存，缓存里没磁力才去原站抓
    detail = await run_in_threadpool(
        scraper.get_movie_detail_cached_first, safe_url, resolved.get("movie_id")
    )
    magnets = (detail or {}).get("magnets") or []
    # r18 源压根不提供磁力，实时抓一次要几十秒且必然为空，别白跑
    if not magnets and (detail or {}).get("source") != "r18":
        detail = await run_in_threadpool(scraper.get_movie_detail, safe_url, False)
        magnets = detail.get("magnets") or []
    best = await run_in_threadpool(scraper.select_best_magnet, magnets, preference)
    return {"success": True, "data": best, "magnets": magnets, "resolved_url": safe_url, "movie_id": resolved.get("movie_id")}


@app.post("/api/translate/batch")
async def batch_translate(payload: Dict[str, Any] = Body(...)):
    normalized = normalize_translation_texts(payload.get("texts") if isinstance(payload, dict) else None)
    if not normalized.get("success"):
        return {"success": False, "message": normalized.get("message"), "data": {}}
    cleaned = normalized["texts"]
    if not cleaned:
        return {"success": True, "data": {}}

    translated = await run_in_threadpool(scraper.batch_translate_to_chinese, cleaned)
    return {"success": True, "data": dict(zip(cleaned, translated))}


@app.post("/api/homepage/refresh")
async def refresh_homepage():
    count = await run_in_threadpool(scraper.refresh_homepage)
    return {"success": True, "count": count}


@app.post("/api/category/refresh")
async def refresh_category(url: str = Query(...)):
    safe_url = normalize_scraper_url(url)
    if not safe_url:
        return {"success": False, "message": "invalid_scraper_url", "count": 0}
    count = await run_in_threadpool(scraper.refresh_category, safe_url)
    return {"success": True, "count": count}


@app.post("/api/download")
async def download_movie(payload: DownloadRequest):
    if str(payload.save_path or "").strip() or str(payload.category or "").strip():
        return {
            "success": False,
            "message": "download_queue_options_unsupported",
        }

    # 同 /api/detail：允许非 JavBus 来源链接走番号回查，解析结果本身已做过域名归一化校验
    resolved = resolve_movie_detail_url_for_download(
        payload.movie_url,
        movie_id=payload.movie_id,
        title=payload.title,
    )
    if not resolved:
        return {"success": False, "message": "movie_code_not_found"}
    safe_movie_url = resolved["url"]
    detail = await run_in_threadpool(scraper.get_movie_detail, safe_movie_url, False)
    magnets = detail.get("magnets") or []
    movie_id = (
        resolved.get("movie_id")
        or extract_movie_id_from_text(detail.get("original_title") or detail.get("title") or safe_movie_url)
    )
    title = detail.get("original_title") or detail.get("title") or payload.title or movie_id or safe_movie_url
    if not magnets:
        if movie_id:
            await run_in_threadpool(
                db.save_pending_download,
                movie_id,
                title,
                "manual_download",
                "No magnet on source; retry later",
            )
        logger.info(
            "Manual download has no magnets: movie_id=%s url=%s",
            display_value_for_log(movie_id),
            display_value_for_log(safe_movie_url),
        )
        return {
            "success": True,
            "queued": False,
            "pending": True,
            "message": "no_magnets_available_pending",
            "movie_id": movie_id,
            "resolved_url": safe_movie_url,
            "magnet_count": 0,
        }

    best = await run_in_threadpool(scraper.select_best_magnet, magnets, payload.preference)
    if not best:
        if movie_id:
            await run_in_threadpool(
                db.save_pending_download,
                movie_id,
                title,
                "manual_download",
                "No suitable magnet found",
            )
        return {
            "success": True,
            "queued": False,
            "pending": True,
            "message": "no_suitable_magnet_pending",
            "movie_id": movie_id,
            "resolved_url": safe_movie_url,
            "magnet_count": len(magnets),
        }

    selected_magnet = normalize_magnet_url(best.get("magnet"))
    selected = {**best, "magnet": selected_magnet} if selected_magnet else best
    existing_task = await run_in_threadpool(
        db.get_active_queue_entry,
        movie_id=movie_id,
        magnet=selected_magnet,
    )
    if existing_task:
        return {
            "success": True,
            "task_id": existing_task.get("id"),
            "queued": False,
            "duplicate": True,
            "magnet": selected,
            "selected_magnet": selected,
        }
    permission = await run_in_threadpool(
        evaluate_download_permission,
        movie_id,
        selected_magnet,
        title,
        safe_movie_url,
        automated=False,
    )
    if not permission["allowed"]:
        return {"success": False, "message": "download_blocked", "reasons": permission["reasons"]}

    queue_result = await run_in_threadpool(
        queue_download_if_absent,
        selected_magnet,
        movie_id or str(uuid.uuid4()),
        title,
        safe_movie_url,
    )
    return {
        "success": True,
        "task_id": queue_result["task_id"],
        "queued": queue_result["queued"],
        "duplicate": queue_result["duplicate"],
        "magnet": selected,
        "selected_magnet": selected,
    }


@app.post("/api/download/magnet")
async def download_magnet(payload: MagnetDownloadRequest):
    magnet = normalize_magnet_url(payload.magnet_url)
    if not magnet:
        return {"success": False, "message": "invalid_magnet_url"}

    save_path = normalize_qb_save_path(payload.save_path)
    if payload.save_path and not save_path:
        return {
            "success": False,
            "message": "invalid_save_path",
        }

    category = normalize_qb_category(payload.category)
    if payload.category and not category:
        return {"success": False, "message": "invalid_qb_category"}

    permission = await run_in_threadpool(
        evaluate_download_permission,
        None,
        magnet,
        "Manual magnet download",
        "manual_magnet",
        automated=False,
    )
    if not permission["allowed"]:
        return {"success": False, "message": "download_blocked", "reasons": permission["reasons"]}

    task_movie_id = f"MAGNET-{hashlib.sha256(magnet.encode('utf-8')).hexdigest()[:16].upper()}"
    queue_result = await run_in_threadpool(
        queue_download_if_absent,
        magnet,
        task_movie_id,
        "Manual magnet download",
        "manual_magnet",
        save_path,
        category,
    )
    return {
        "success": True,
        "task_id": queue_result["task_id"],
        "queued": queue_result["queued"],
        "duplicate": queue_result["duplicate"],
        "status": queue_result["status"],
    }


@app.get("/api/downloads/pending/stats")
async def get_pending_download_stats(limit: int = Query(20, ge=1, le=100)):
    rows = await run_in_threadpool(db.get_pending_downloads_list, 500)
    summary = summarize_pending_downloads(rows)
    recent = rows[:limit]
    return {
        "success": True,
        "data": {
            **summary,
            "items": [
                {
                    "movie_id": item.get("movie_id"),
                    "title": item.get("title"),
                    "source": item.get("source"),
                    "status": item.get("status"),
                    "retry_count": item.get("retry_count"),
                    "last_checked_at": item.get("last_checked_at"),
                    "next_retry_at": get_pending_next_retry_at(item),
                    "note": item.get("note"),
                }
                for item in recent
            ],
        },
    }

# MARK: - API: Jellyfin Webhook

def is_jellyfin_favorite_event(payload: JellyfinWebhookRequest) -> bool:
    return get_jellyfin_service().is_favorite_event(payload)


def jellyfin_favorite_state(payload: JellyfinWebhookRequest) -> bool:
    return get_jellyfin_service().favorite_state(payload)


def is_jellyfin_watched_event(payload: JellyfinWebhookRequest) -> bool:
    return get_jellyfin_service().is_watched_event(payload)


async def apply_jellyfin_actor_subscription(
    *,
    name: str,
    actress_id: Optional[str] = None,
    page_url: Optional[str] = None,
    source_url: Optional[str] = None,
    is_subscribed: bool = True,
    sync_jellyfin_person: bool = False,
) -> Dict[str, Any]:
    return await get_jellyfin_service().apply_actor_subscription(
        name=name,
        actress_id=actress_id,
        page_url=page_url,
        source_url=source_url,
        is_subscribed=is_subscribed,
        sync_jellyfin_person=sync_jellyfin_person,
    )


@app.post("/api/webhooks/jellyfin")
async def jellyfin_webhook_route(payload: JellyfinWebhookRequest, request: Request):
    validate_jellyfin_webhook_request(request)
    return await jellyfin_webhook(payload)


async def jellyfin_webhook(payload: JellyfinWebhookRequest):
    return await get_jellyfin_service().handle_webhook(payload)


# MARK: - API: Lifecycle & Automation

@app.get("/api/lifecycle/movies")
async def get_lifecycle_movies(
    movie_id: Optional[str] = Query(None),
    discovery_status: Optional[str] = Query(None),
    decision_status: Optional[str] = Query(None),
    download_status: Optional[str] = Query(None),
    organize_status: Optional[str] = Query(None),
    jellyfin_status: Optional[str] = Query(None),
    cleanup_status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    rows = await run_in_threadpool(
        db.get_movie_lifecycle,
        movie_id,
        discovery_status=discovery_status,
        decision_status=decision_status,
        download_status=download_status,
        organize_status=organize_status,
        jellyfin_status=jellyfin_status,
        cleanup_status=cleanup_status,
        limit=limit,
        offset=offset,
    )
    return {"success": True, "data": rows}


@app.get("/api/lifecycle/movies/{movie_id}/events")
async def get_lifecycle_events(movie_id: str, limit: int = Query(100, ge=1, le=500)):
    rows = await run_in_threadpool(db.get_movie_lifecycle_events, movie_id, limit)
    return {"success": True, "data": rows}


@app.get("/api/cleanup/candidates")
async def get_cleanup_candidates(
    status: str = Query("candidate", pattern="^(candidate|approved|rejected|deleted)?$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    rows = await run_in_threadpool(db.get_cleanup_candidates, status, limit, offset)
    public_rows = [cleanup_candidate_view(row) for row in rows]
    return {
        "success": True,
        "data": public_rows,
        "count": len(public_rows),
        "limit": limit,
        "offset": offset,
    }


def cleanup_candidate_view(row: Dict[str, Any]) -> Dict[str, Any]:
    public = {key: value for key, value in row.items() if key != "file_path"}
    for key in ("title", "reason", "source", "decision_actor", "decision_reason"):
        if public.get(key):
            public[key] = redact_log_message(public[key])
    public.pop("conflict", None)
    return public


def decide_cleanup_candidate(candidate_id: str, status: str, decision: AutomationCandidateDecisionRequest):
    normalized_id = normalize_task_id(candidate_id)
    if not normalized_id:
        raise HTTPException(status_code=400, detail="invalid_cleanup_candidate_id")
    reason = str(decision.reason or "").strip()
    if len(reason) < 2:
        return {"success": False, "message": "cleanup_decision_reason_required"}
    row = db.decide_cleanup_candidate(
        normalized_id,
        status,
        decision.actor or "user",
        reason,
    )
    if not row:
        return {"success": False, "message": "cleanup_candidate_not_found"}
    if row.get("conflict"):
        return {
            "success": False,
            "message": "cleanup_candidate_state_conflict",
            "status": row.get("status"),
        }
    return {
        "success": True,
        "duplicate": bool(row.get("duplicate")),
        "delete_performed": False,
        "data": cleanup_candidate_view(row),
    }


@app.post("/api/cleanup/candidates/{candidate_id}/approve")
async def approve_cleanup_candidate(
    candidate_id: str,
    decision: AutomationCandidateDecisionRequest = Body(...),
):
    return await run_in_threadpool(decide_cleanup_candidate, candidate_id, "approved", decision)


@app.post("/api/cleanup/candidates/{candidate_id}/reject")
async def reject_cleanup_candidate(
    candidate_id: str,
    decision: AutomationCandidateDecisionRequest = Body(...),
):
    return await run_in_threadpool(decide_cleanup_candidate, candidate_id, "rejected", decision)


@app.get("/api/automation/candidates")
async def get_automation_candidates(
    status: Optional[str] = Query("candidate", pattern="^(candidate|approved|rejected)?$"),
    candidate_type: Optional[str] = Query(None, pattern="^(favorite_actor|subscription_download)?$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    data = await run_in_threadpool(
        get_automation_service().list_candidates,
        status,
        candidate_type,
        limit,
        offset,
    )
    return {
        "success": True,
        "data": data,
        "count": len(data),
        "limit": limit,
        "offset": offset,
    }


@app.post("/api/automation/candidates/{candidate_id}/approve")
async def approve_automation_candidate(
    candidate_id: str,
    decision: AutomationCandidateDecisionRequest = Body(...),
):
    return await run_in_threadpool(
        get_automation_service().approve_candidate,
        candidate_id,
        decision.actor or "user",
        decision.reason,
    )


@app.post("/api/automation/candidates/{candidate_id}/reject")
async def reject_automation_candidate(
    candidate_id: str,
    decision: AutomationCandidateDecisionRequest = Body(...),
):
    return await run_in_threadpool(
        get_automation_service().reject_candidate,
        candidate_id,
        decision.actor or "user",
        decision.reason,
    )


@app.post("/api/automation/candidates/approve_all")
async def approve_all_automation_candidates(
    decision: Optional[AutomationCandidateDecisionRequest] = Body(None),
):
    decision = decision or AutomationCandidateDecisionRequest()
    report = await run_in_threadpool(
        get_automation_service().approve_all,
        decision.actor or "user",
        decision.reason or "Bulk approve automation candidates",
    )
    return {"success": True, "report": report}


@app.get("/api/automation/settings")
async def get_automation_settings():
    return {"success": True, "data": await run_in_threadpool(get_automation_service().get_settings)}


@app.post("/api/automation/settings")
async def update_automation_settings(payload: AutomationSettingsRequest):
    updates = payload.model_dump(exclude_unset=True)
    return {
        "success": True,
        "data": await run_in_threadpool(
            get_automation_service().update_settings,
            updates,
        ),
    }

# MARK: - API: Actresses

@app.get("/api/actresses")
async def get_actresses(
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=100),
    query: Optional[str] = Query(None),
    followed_only: bool = Query(False),
    subscribed_only: Optional[bool] = Query(None),
    subscription_state: Optional[str] = Query(None, pattern="^(all|subscribed|unsubscribed)$"),
    sort_by_new: bool = Query(False),
    sort_by_cup: bool = Query(False),
):
    return await run_in_threadpool(
        get_actress_service().list_actresses,
        page=page,
        limit=limit,
        query=query,
        followed_only=followed_only,
        subscribed_only=subscribed_only,
        subscription_state=subscription_state,
        sort_by_new=sort_by_new,
        sort_by_cup=sort_by_cup,
    )


@app.get("/api/actress/{actress_id}")
async def get_actress_detail(actress_id: str):
    return await run_in_threadpool(get_actress_service().get_detail, actress_id)


@app.post("/api/actresses/avatar/sync")
async def sync_actress_avatars(
    dry_run: bool = Query(False),
    limit: Optional[int] = Query(None, ge=1),
):
    """从 gfriends 公开头像库回填缺头像的演员。只写 URL，图片按需走 /api/proxy/image 缓存。"""
    from gfriends_sync import GfriendsAvatarSyncer

    def _run() -> Dict[str, Any]:
        return GfriendsAvatarSyncer(db).sync(dry_run=dry_run, limit=limit)

    try:
        report = await run_in_threadpool(_run)
    except Exception as e:
        logger.error("Actress avatar sync failed: %s", safe_exception_label(e))
        raise HTTPException(status_code=502, detail="actress_avatar_sync_failed")
    return {"success": True, "data": report, "report": report}


@app.post("/api/actresses/mark_viewed")
async def mark_actresses_viewed():
    return await run_in_threadpool(get_actress_service().mark_viewed)


@app.post("/api/actresses/sync_all_jellyfin")
async def sync_all_actresses_jellyfin():
    report = await run_in_threadpool(sync_jellyfin_status_report)
    return {"success": True, "data": report, "report": report}


@app.post("/api/actresses/{actress_id}/sync_jellyfin")
async def sync_actress_jellyfin(actress_id: str):
    actress = await run_in_threadpool(db.get_actress, actress_id)
    if not actress:
        return {"success": False, "message": "actress_not_found"}

    report = await run_in_threadpool(sync_jellyfin_status_report)
    return {
        "success": True,
        "message": "single_actress_sync_uses_full_jellyfin_scan",
        "data": {"actress": actress, "report": report},
    }


@app.post("/api/actresses/subscribe")
@app.post("/api/actresses/follow", include_in_schema=False)
async def toggle_actress_subscription(payload: ActressSubscriptionRequest):
    return await run_in_threadpool(get_actress_service().toggle_subscription, payload.actress_id)


# MARK: - API: Subscriptions

@app.get("/api/subscriptions")
async def get_subscriptions(sync: bool = Query(False)):
    if sync:
        favorite_people_sync, favorite_movie_people_sync = await asyncio.gather(
            run_subscription_sync_with_timeout(sync_jellyfin_favorite_people_to_subscriptions, "favorite_people"),
            run_subscription_sync_with_timeout(sync_jellyfin_favorite_movie_people_to_subscriptions, "favorite_movie_people"),
        )
    else:
        favorite_people_sync = {"skipped": 1, "error": None}
        favorite_movie_people_sync = {"skipped": 1, "error": None}
    jellyfin_sync = {
        "synced": parse_int_value(favorite_people_sync.get("synced"), 0, minimum=0)
        + parse_int_value(favorite_movie_people_sync.get("matched"), 0, minimum=0),
        "subscribed": parse_int_value(favorite_people_sync.get("subscribed"), 0, minimum=0)
        + parse_int_value(favorite_movie_people_sync.get("subscribed"), 0, minimum=0),
        "created": parse_int_value(favorite_people_sync.get("created"), 0, minimum=0)
        + parse_int_value(favorite_movie_people_sync.get("created"), 0, minimum=0),
        "skipped": parse_int_value(favorite_people_sync.get("skipped"), 0, minimum=0)
        + parse_int_value(favorite_movie_people_sync.get("skipped"), 0, minimum=0),
        "favorite_people": favorite_people_sync,
        "favorite_movie_people": favorite_movie_people_sync,
    }
    repair = await run_in_threadpool(ensure_followed_actress_subscriptions)
    name_normalization = await run_in_threadpool(normalize_legacy_subscription_names)
    subscriptions = await run_in_threadpool(db.get_subscriptions)
    subscription_views = [subscription_response_view(row) for row in subscriptions]
    return {
        "success": True,
        "data": subscription_views,
        "jellyfin_sync": jellyfin_sync,
        "repair": repair,
        "name_normalization": name_normalization,
    }


@app.post("/api/subscriptions")
async def add_subscription(payload: SubscriptionRequest):
    url = payload.url.strip()
    name = payload.name.strip()
    category_type = payload.category_type.strip() or "category"
    start_date = payload.start_date.strip()
    exclude_tags = (payload.exclude_tags or "").strip()

    if not url or not name:
        return {"success": False, "message": "missing_subscription_url_or_name"}

    safe_url = normalize_subscription_source_url(url)
    if not safe_url:
        return {"success": False, "message": "invalid_subscription_url"}

    success = await run_in_threadpool(
        db.add_subscription,
        safe_url,
        name,
        category_type,
        start_date,
        exclude_tags,
    )
    return {"success": bool(success), "message": "subscription_saved" if success else "subscription_save_failed"}


@app.delete("/api/subscriptions")
async def delete_subscription(url: str = Query(...)):
    target = url.strip()
    if not target:
        return {"success": False, "message": "missing_subscription_url"}

    safe_target = normalize_subscription_source_url(target)
    if not safe_target:
        return {"success": False, "message": "invalid_subscription_url"}
    for item in get_subscription_delete_url_candidates(target):
        await run_in_threadpool(db.delete_subscription, item)
    return {"success": True, "message": "task_cancelled"}


@app.post("/api/subscriptions/refresh")
async def refresh_subscription(url: str = Query(...)):
    target = url.strip()
    if not target:
        return {"success": False, "message": "missing_subscription_url"}
    safe_target = normalize_subscription_source_url(target)
    if not safe_target:
        return {"success": False, "message": "invalid_subscription_url"}

    subscriptions = await run_in_threadpool(db.get_subscriptions)
    lookup_urls = set(get_subscription_delete_url_candidates(target))
    lookup_urls.add(safe_target)
    subscription = next((sub for sub in subscriptions if sub.get("url") in lookup_urls), None)
    if not subscription:
        return {"success": False, "message": "subscription_not_found"}

    report = await run_in_threadpool(process_subscription, subscription, True)
    return {"success": True, "report": report}


@app.get("/api/subscriptions/stats")
async def get_subscription_stats(include_decorative_covers: bool = Query(False, description="是否附带卡片装饰封面（很慢，按需）")):
    return {
        "success": True,
        "data": await run_in_threadpool(
            get_subscription_stats_service().get_stats,
            include_decorative_covers,
        ),
    }


@app.get("/api/subscriptions/covers")
async def get_subscription_covers(url: str = Query(..., description="订阅 URL")):
    """单个订阅的装饰封面。用户展开卡片时才调，避免列表页每次加载都算一遍。"""
    try:
        covers = await run_in_threadpool(
            get_subscription_stats_service().covers_for_subscription, url
        )
        return {"success": True, "data": covers}
    except Exception as e:
        logger.error("Subscription covers failed: %s", safe_exception_label(e))
        return {"success": True, "data": []}


# MARK: - API: Movie Search & Management

@app.get("/api/search")
async def search_movies(
    q: Optional[str] = Query(None, max_length=200, description="search keyword"),
    limit: int = Query(50, ge=1, le=200, description="result limit"),
    offset: int = Query(0, ge=0, le=10000, description="result offset")
):
    # Search movies from the local database.
    try:
        movies = await run_in_threadpool(db.search_movies, q, limit, offset)
        # 搜索结果同样只展示处理完的数据：没翻好的这次不下发，并插队翻译
        movies, pending_translation_count = await run_in_threadpool(
            scraper.filter_processed_movies, movies
        )
        schedule_image_cache_warmup(movies, "search")
        return {
            "success": True,
            "data": movies,
            "count": len(movies),
            "pending_translation_count": pending_translation_count,
        }
    except Exception as e:
        logger.error("Search movies failed: %s", safe_exception_label(e))
        raise HTTPException(status_code=500, detail="search_movies_failed")


@app.get("/api/jellyfin/libraries")
async def get_jellyfin_libraries():
    # Return Jellyfin libraries.
    try:
        result = await get_jellyfin_service().get_libraries()
        return {"success": True, **result}
    except Exception as e:
        logger.error("Jellyfin libraries failed: %s", safe_exception_label(e))
        return safe_error_response("jellyfin_libraries_failed", e)


@app.get("/api/jellyfin/stats")
async def get_jellyfin_stats():
    # Return Jellyfin integration summary.
    try:
        return {"success": True, "data": await get_jellyfin_service().get_stats()}
    except Exception as e:
        logger.error("Jellyfin stats failed: %s", safe_exception_label(e))
        return safe_error_response("jellyfin_stats_failed", e)


@app.get("/api/library/category-distribution")
async def get_library_category_distribution(limit: int = Query(8, ge=1, le=20)):
    """Return local database genre distribution for the library insights chart."""
    try:
        data = await run_in_threadpool(db.get_movie_category_distribution, limit)
        return {"success": True, "data": data}
    except Exception as e:
        logger.error("Library category distribution failed: %s", safe_exception_label(e))
        return safe_error_response("library_category_distribution_failed", e)


LIBRARY_INDEX_TTL_SECONDS = get_float_env("BUS_TERMINAL_LIBRARY_INDEX_TTL", 300.0, minimum=10.0)
LIBRARY_INDEX_PAGE_SIZE = 500
_library_index_lock = threading.Lock()
_library_index_state: Dict[str, Any] = {"at": 0.0, "index": {}, "building": False}


def _library_index_payload(index: Dict[str, str], *, success: bool, cached: bool) -> Dict[str, Any]:
    return {"success": success, "cached": cached, "count": len(index), "index": index}


def _build_library_index() -> Dict[str, str]:
    """扫一遍 Jellyfin 影片，建立 番号 -> 条目 ID 的索引。

    条目名形如 "MBMH-097 标题" / "FHD_6M-SSNI-658A"，番号统一走 extract_number 解析；
    解析不出番号的条目无法和片库对齐，直接跳过（宁可漏，不可错指到别的片）。
    """
    index: Dict[str, str] = {}
    offset = 0
    while True:
        page = jellyfin_client.get_library_items(
            None, None, None, "DateCreated", "Descending", None,
            LIBRARY_INDEX_PAGE_SIZE, offset,
            fields="Name",  # 只要 Id + Name，别让 Jellyfin 去关联 People/MediaSources
        )
        items = page.get("items") or []
        for item in items:
            jellyfin_id = str(item.get("jellyfin_id") or "").strip()
            if not jellyfin_id:
                continue
            number = extract_number(str(item.get("title") or ""))
            if number:
                index[number.upper()] = jellyfin_id
        total = parse_int_value(page.get("total"), 0, minimum=0)
        offset += LIBRARY_INDEX_PAGE_SIZE
        if len(items) < LIBRARY_INDEX_PAGE_SIZE or offset >= total:
            break
    return index


@app.get("/api/library/index")
async def get_library_index(refresh: bool = Query(False)):
    """已入库影片索引（番号 -> Jellyfin 条目 ID）。

    前端一次拉回整份索引，卡片据此判断该显示"播放"还是"下载"，列表页不必逐条问后端。
    Jellyfin 不可达时退回旧索引；连旧索引都没有就返回空（等价于"未知"，卡片继续显示下载）。
    """
    now = time.time()
    with _library_index_lock:
        state = _library_index_state
        cached_index = state["index"]
        is_fresh = bool(cached_index) and now - state["at"] < LIBRARY_INDEX_TTL_SECONDS
        if state["building"] or (is_fresh and not refresh):
            return _library_index_payload(cached_index, success=True, cached=True)
        state["building"] = True
    try:
        index = await run_in_threadpool(_build_library_index)
    except Exception as e:
        logger.error("Library index build failed: %s", safe_exception_label(e))
        return _library_index_payload(cached_index, success=False, cached=True)
    finally:
        with _library_index_lock:
            _library_index_state["building"] = False
    with _library_index_lock:
        _library_index_state["index"] = index
        _library_index_state["at"] = time.time()
    return _library_index_payload(index, success=True, cached=False)


@app.get("/api/jellyfin/library")
async def get_jellyfin_library(
    library_id: Optional[str] = Query(None),
    watched: Optional[bool] = Query(None),
    liked: Optional[bool] = Query(None),
    sort_by: str = Query("DateCreated"),
    sort_order: str = Query("Descending"),
    query: Optional[str] = Query(None, max_length=200),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=10000),
):
    # Query movies directly from Jellyfin.
    try:
        safe_library_id = normalize_item_id(library_id) if library_id else None
        if library_id and not safe_library_id:
            raise HTTPException(status_code=400, detail="invalid_library_id")

        safe_sort_by = normalize_enum_value(sort_by, JELLYFIN_LIBRARY_SORT_FIELDS, "DateCreated")
        if not safe_sort_by:
            raise HTTPException(status_code=400, detail="invalid_sort_by")

        safe_sort_order = normalize_enum_value(sort_order, JELLYFIN_LIBRARY_SORT_ORDERS, "Descending")
        if not safe_sort_order:
            raise HTTPException(status_code=400, detail="invalid_sort_order")

        result = await get_jellyfin_service().get_library(
            safe_library_id,
            watched,
            liked,
            safe_sort_by,
            safe_sort_order,
            query,
            limit,
            offset,
        )
        return {"success": True, **result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Jellyfin library query failed: %s", safe_exception_label(e))
        return safe_error_response("jellyfin_library_query_failed", e)


@app.get("/api/jellyfin/behavior/insights")
async def get_jellyfin_behavior_insights(
    library_id: Optional[str] = Query(None),
    scan_limit: int = Query(1000, ge=1, le=5000),
    sample_limit: int = Query(8, ge=0, le=50),
    strong_preference_min_plays: int = Query(2, ge=1, le=100)
):
    # Read-only Jellyfin behavior buckets for lifecycle decisions.
    try:
        data = await get_jellyfin_service().get_behavior_insights(
            library_id,
            scan_limit,
            sample_limit,
            strong_preference_min_plays,
        )
        return {"success": True, "data": data}
    except Exception as e:
        logger.error("Jellyfin behavior insights failed: %s", safe_exception_label(e))
        return safe_error_response("jellyfin_behavior_insights_failed", e)


@app.get("/api/jellyfin/behavior/movies")
async def get_jellyfin_behavior_movies(
    bucket: str = Query("watched_not_favorite"),
    library_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    scan_limit: int = Query(1000, ge=1, le=5000),
    strong_preference_min_plays: int = Query(2, ge=1, le=100)
):
    # Read-only Jellyfin behavior movie list by bucket.
    if bucket not in JELLYFIN_BEHAVIOR_BUCKETS:
        raise HTTPException(status_code=400, detail="invalid_behavior_bucket")

    try:
        data = await get_jellyfin_service().get_behavior_movies(
            bucket,
            library_id,
            limit,
            offset,
            scan_limit,
            strong_preference_min_plays,
        )
        return {"success": True, **data}
    except Exception as e:
        logger.error("Jellyfin behavior movies failed: %s", safe_exception_label(e))
        return safe_error_response("jellyfin_behavior_movies_failed", e)


@app.post("/api/jellyfin/curation/scan")
async def scan_jellyfin_curation(
    library: str = Query("黄片"),
    only_gaps: bool = Query(True),
    limit: int = Query(200, ge=1, le=2000),
):
    """只读扫描 Jellyfin 库的元数据缺口，返回清洗计划。不写任何东西。"""
    try:
        result = await run_in_threadpool(
            get_jellyfin_curation_service().scan, library, only_gaps
        )
    except Exception as e:
        logger.error("Jellyfin curation scan failed: %s", safe_exception_label(e))
        return safe_error_response("jellyfin_curation_scan_failed", e)
    items = (result.get("items") or [])[:limit]
    return {**result, "items": items, "returned": len(items)}


@app.post("/api/jellyfin/curation/apply")
async def apply_jellyfin_curation(payload: Dict = Body(...)):
    """按清洗计划执行写入。默认 dry_run，受开关 + 确认码 + 单批上限保护。"""
    item_ids, item_ids_error = normalize_jellyfin_curation_item_ids(payload.get("item_ids", []))
    if item_ids_error:
        return {"success": False, "message": item_ids_error}
    guard = validate_jellyfin_curation_request(payload, len(item_ids))
    if not guard.get("success"):
        return guard

    if guard.get("dry_run"):
        # dry-run 只回报将要做什么，不发任何写请求
        plans = await run_in_threadpool(
            get_jellyfin_curation_service().scan, str(payload.get("library") or "黄片"), True
        )
        wanted = set(item_ids)
        return {
            "success": True,
            "dry_run": True,
            "planned": [p for p in (plans.get("items") or []) if p.get("jellyfin_id") in wanted],
        }

    actions_filter = payload.get("actions")
    try:
        return await run_in_threadpool(
            get_jellyfin_curation_service().execute,
            item_ids,
            actions_filter if isinstance(actions_filter, list) else None,
        )
    except Exception as e:
        logger.error("Jellyfin curation apply failed: %s", safe_exception_label(e))
        return safe_error_response("jellyfin_curation_apply_failed", e)


@app.post("/api/jellyfin/items/delete")
async def delete_jellyfin_items(payload: Dict = Body(...)):
    # Delete Jellyfin items synchronously.
    item_ids, item_ids_error = validate_jellyfin_item_ids_payload(payload.get("item_ids", []))
    if item_ids_error:
        return {"success": False, "message": item_ids_error}
    guard = validate_jellyfin_delete_request(payload, len(item_ids))
    if not guard.get("success"):
        return guard
    try:
        return await get_jellyfin_service().delete_items(item_ids, reason=guard.get("reason"))
    except Exception as e:
        logger.error("Jellyfin delete request failed: %s", safe_exception_label(e))
        return safe_error_response("jellyfin_delete_failed", e)

@app.post("/api/jellyfin/items/delete_async")
async def delete_jellyfin_items_async(payload: Dict = Body(...)):
    # Delete Jellyfin items asynchronously and return a task id.
    item_ids, item_ids_error = validate_jellyfin_item_ids_payload(payload.get("item_ids", []))
    if item_ids_error:
        return {"success": False, "message": item_ids_error}

    guard = validate_jellyfin_delete_request(payload, len(item_ids))
    if not guard.get("success"):
        return guard

    task_id = get_jellyfin_service().create_delete_task(item_ids, reason=guard.get("reason"))

    asyncio.create_task(delete_items_background(task_id, item_ids))

    logger.info("Created delete task %s, total %s items", display_value_for_log(task_id), len(item_ids))

    return {
        "success": True,
        "task_id": task_id,
        "total": len(item_ids),
        "message": f"delete_task_created:{len(item_ids)}",
    }

async def delete_items_background(task_id: str, item_ids: List[str]):
    return await get_jellyfin_service().delete_items_background(task_id, item_ids)

    # Run a Jellyfin delete task in the background.
    try:
        with deletion_tasks_lock:
            task = deletion_tasks[task_id]
            task["status"] = "running"
            task["started_at"] = time.time()

        jellyfin_client.reload_config()

        for idx, item_id in enumerate(item_ids):
            with deletion_tasks_lock:
                if deletion_tasks[task_id]["status"] == "cancelled":
                    logger.info("Delete task %s cancelled", display_value_for_log(task_id))
                    return

            try:
                # Call Jellyfin API to delete the item.
                success = await run_in_threadpool(
                    jellyfin_client.delete_item, item_id
                )

                with deletion_tasks_lock:
                    task = deletion_tasks[task_id]
                    task["results"].append({
                        "id": item_id,
                        "success": success,
                        "index": idx + 1,
                        "timestamp": time.time()
                    })

                    if success:
                        task["completed"] += 1
                    else:
                        task["failed"] += 1

                    # Update delete progress.
                    task["progress"] = round((idx + 1) / len(item_ids) * 100, 2)

                logger.debug(
                    "Delete task %s: %s/%s - %s - %s",
                    display_value_for_log(task_id),
                    idx + 1,
                    len(item_ids),
                    display_value_for_log(item_id),
                    "success" if success else "failed",
                )

            except Exception as e:
                error_label = safe_exception_label(e)
                with deletion_tasks_lock:
                    task = deletion_tasks[task_id]
                    task["failed"] += 1
                    task["results"].append({
                        "id": item_id,
                        "success": False,
                        "error": error_label,
                        "index": idx + 1,
                        "timestamp": time.time()
                    })
                    task["progress"] = round((idx + 1) / len(item_ids) * 100, 2)

                logger.error(
                    "Delete task %s item %s failed: %s",
                    display_value_for_log(task_id),
                    display_value_for_log(item_id),
                    error_label,
                )

            await asyncio.sleep(0.2)

        with deletion_tasks_lock:
            task = deletion_tasks[task_id]
            task["status"] = "completed"
            task["completed_at"] = time.time()
            task["progress"] = 100

        logger.info(
            "Delete task %s completed: success=%s, failed=%s",
            display_value_for_log(task_id),
            task["completed"],
            task["failed"],
        )

    except Exception as e:
        error_label = safe_exception_label(e)
        logger.error("Delete task %s failed: %s", display_value_for_log(task_id), error_label)
        with deletion_tasks_lock:
            task = deletion_tasks[task_id]
            task["status"] = "error"
            task["error"] = error_label
            task["completed_at"] = time.time()

@app.get("/api/jellyfin/delete_progress/{task_id}")
async def get_delete_progress(task_id: str):
    # Return delete task progress.
    task_id = normalize_task_id(task_id)
    if not task_id:
        raise HTTPException(status_code=400, detail="invalid_task_id")

    progress = get_jellyfin_service().get_delete_progress(task_id)
    if not progress:
        raise HTTPException(status_code=404, detail="task_not_found")
    return {"success": True, "task_id": task_id, **progress}

@app.delete("/api/jellyfin/delete_task/{task_id}")
async def cancel_delete_task(task_id: str):
    # Cancel a delete task.
    task_id = normalize_task_id(task_id)
    if not task_id:
        raise HTTPException(status_code=400, detail="invalid_task_id")

    result = get_jellyfin_service().cancel_delete_task(task_id)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail="task_not_found")
    return {"success": bool(result.get("cancelled")), "message": result.get("message")}

@app.get("/api/jellyfin/delete_tasks")
async def list_delete_tasks(limit: int = Query(10, ge=1, le=50)):
    # List recent delete tasks.
    return {"success": True, "tasks": get_jellyfin_service().list_delete_tasks(limit)}


@app.post("/api/jellyfin/sync_status")
async def sync_jellyfin_status_endpoint():
    # Sync Jellyfin favorite/watched status and return a report.
    try:
        report = await run_in_threadpool(sync_jellyfin_status_report)
        return {
            "success": True,
            "message": "jellyfin_delete_disabled",
            "data": report,
        }
    except Exception as e:
        logger.error("Sync Jellyfin status failed: %s", safe_exception_label(e))
        return safe_error_response("jellyfin_sync_failed", e)


async def sync_jellyfin_status():
    result = await sync_jellyfin_status_endpoint()
    if result.get("success") and "report" not in result:
        result = {**result, "report": result.get("data")}
    return result


@app.post("/api/scrape/jellyfin-refresh")
async def refresh_jellyfin_scrape():
    return await sync_jellyfin_status_endpoint()


@app.post("/api/jellyfin/favorite_actor")
async def favorite_actor_sync(payload: Dict = Body(...)):
    try:
        return await get_jellyfin_service().favorite_actor_sync(payload or {})
    except Exception as e:
        logger.error("Jellyfin favorite actor sync failed: %s", safe_exception_label(e))
        return safe_error_response("jellyfin_favorite_actor_sync_failed", e)


# MARK: - SPA Fallback

def resolve_static_file_path(full_path: str) -> Optional[str]:
    from pathlib import Path

    static_root = Path(STATIC_DIR).resolve()
    requested_path = (static_root / full_path).resolve()
    try:
        requested_path.relative_to(static_root)
    except ValueError:
        return None
    if requested_path.is_file():
        return str(requested_path)
    return None


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    # Keep unknown API paths as API 404 instead of serving the frontend shell.
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API endpoint not found")

    requested_path = resolve_static_file_path(full_path)
    if requested_path:
        return FileResponse(requested_path)

    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)

    raise HTTPException(status_code=404, detail="Resource not found")
