"""Shared read-only API smoke cases grouped by business module."""

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple


@dataclass(frozen=True)
class SmokeCase:
    name: str
    path: str
    params: Mapping[str, Any] = field(default_factory=dict)
    required_keys: Tuple[str, ...] = ("success",)
    require_success: bool = True
    timeout_seconds: Optional[float] = None


MODULE_CASES: Dict[str, Tuple[SmokeCase, ...]] = {
    "system": (
        SmokeCase("liveness", "/api/health/live", required_keys=("success", "status")),
        SmokeCase("readiness", "/api/health/check", required_keys=("success", "status", "database")),
        SmokeCase("system_config", "/api/config/system", required_keys=("success", "data")),
        SmokeCase("dashboard", "/api/stats/dashboard", required_keys=("success", "data")),
        SmokeCase("task_status", "/api/tasks/status", required_keys=("success", "data")),
        SmokeCase("external_tasks", "/api/tasks/external", {"limit": 20}, ("success", "data", "count")),
        SmokeCase("cache_stats", "/api/cache/stats", required_keys=("success", "data"), timeout_seconds=45),
        SmokeCase("image_cache_stats", "/api/cache/images/stats", required_keys=("success", "data"), timeout_seconds=45),
    ),
    "content": (
        SmokeCase("navigation", "/api/navigation", required_keys=("success", "data")),
        SmokeCase("genres", "/api/genres", {"type": "censored"}, ("success", "data")),
        SmokeCase(
            "homepage_movies",
            "/api/movies",
            {
                "url": "https://www.javbus.com",
                "page": 1,
                "limit": 30,
                "translate_mode": "cache_only",
            },
            ("success", "data", "count"),
        ),
        SmokeCase("discovery_sources", "/api/discovery/sources", required_keys=("success", "data")),
        SmokeCase(
            "discovery_movies",
            "/api/discovery/movies",
            {"source": "javbus", "page": 1, "rank_type": "daily"},
            ("success", "data"),
        ),
        SmokeCase("local_search", "/api/search", {"limit": 20}, ("success", "data", "count")),
    ),
    "subscriptions": (
        SmokeCase("actresses", "/api/actresses", {"page": 1, "limit": 30}, ("success", "data")),
        SmokeCase("subscriptions", "/api/subscriptions", {"sync": "false"}, ("success", "data")),
        SmokeCase("subscription_stats", "/api/subscriptions/stats", required_keys=("success", "data"), timeout_seconds=45),
    ),
    "downloads": (
        SmokeCase("qb_config", "/api/qb/config", required_keys=("success", "data")),
        SmokeCase("qb_status", "/api/qb/status", required_keys=("success",)),
        SmokeCase("qb_dashboard", "/api/qb/dashboard", required_keys=("success",), timeout_seconds=45),
        SmokeCase("qb_torrents", "/api/qb/torrents", {"limit": 20, "offset": 0}, ("success", "data"), timeout_seconds=45),
        SmokeCase("download_queue", "/api/download/queue", {"limit": 20}, ("success", "data")),
        SmokeCase("pending_downloads", "/api/downloads/pending/stats", {"limit": 20}, ("success", "data")),
    ),
    "lifecycle": (
        SmokeCase("movie_lifecycle", "/api/lifecycle/movies", {"limit": 20}, ("success", "data")),
        SmokeCase("cleanup_candidates", "/api/cleanup/candidates", {"limit": 20}, ("success", "data", "count")),
        SmokeCase("automation_candidates", "/api/automation/candidates", {"limit": 20}, ("success", "data", "count")),
        SmokeCase("automation_settings", "/api/automation/settings", required_keys=("success", "data")),
    ),
    "jellyfin": (
        SmokeCase("jellyfin_libraries", "/api/jellyfin/libraries", required_keys=("success",), timeout_seconds=45),
        SmokeCase("jellyfin_stats", "/api/jellyfin/stats", required_keys=("success", "data"), timeout_seconds=45),
        SmokeCase("jellyfin_library", "/api/jellyfin/library", {"limit": 20}, ("success",), timeout_seconds=45),
        SmokeCase(
            "jellyfin_insights",
            "/api/jellyfin/behavior/insights",
            {"scan_limit": 1000, "sample_limit": 5},
            ("success", "data"),
            timeout_seconds=60,
        ),
        SmokeCase(
            "jellyfin_behavior_movies",
            "/api/jellyfin/behavior/movies",
            {"bucket": "watched_not_favorite", "limit": 20, "scan_limit": 1000},
            ("success",),
            timeout_seconds=60,
        ),
    ),
}


def selected_cases(modules: Tuple[str, ...]) -> Tuple[Tuple[str, SmokeCase], ...]:
    selected = []
    for module_name in modules:
        selected.extend((module_name, case) for case in MODULE_CASES[module_name])
    return tuple(selected)
