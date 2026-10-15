# BusTerminal API

> Generated from OpenAPI `0.1.0`. Do not edit the endpoint table manually.

Runtime documentation: `/docs`; machine-readable schema: `/openapi.json`.

The service currently has no built-in authentication. Keep it on a trusted LAN or behind an authenticated reverse proxy/VPN.

## Verification levels

- `read-only`: safe for Docker smoke checks, although some GET requests may refresh local caches or contact upstream sources.
- `local-write`: changes BusTerminal's local database/cache/configuration; verify in an isolated test database.
- `external-write`: may control qBittorrent/Jellyfin or enqueue real downloads; automated tests must mock the external service.

Operations: 126 total; 69 read-only; 44 local-write; 13 external-write.

## Endpoints

| Method | Path | Risk | Tags | Parameters | Request body | Summary |
| --- | --- | --- | --- | --- | --- | --- |
| `GET` | `/` | `read-only` | - | - | - | Read Root |
| `GET` | `/api/actress/{actress_id}` | `read-only` | - | path:actress_id* | - | Get Actress Detail |
| `GET` | `/api/actresses` | `read-only` | - | query:page, query:limit, query:query, query:followed_only, query:subscribed_only, query:subscription_state, query:sort_by_new, query:sort_by_cup | - | Get Actresses |
| `POST` | `/api/actresses/avatar/sync` | `local-write` | - | query:dry_run, query:limit | - | Sync Actress Avatars |
| `POST` | `/api/actresses/mark_viewed` | `local-write` | - | - | - | Mark Actresses Viewed |
| `POST` | `/api/actresses/subscribe` | `local-write` | - | - | required | Toggle Actress Subscription |
| `POST` | `/api/actresses/sync_all_jellyfin` | `external-write` | - | - | - | Sync All Actresses Jellyfin |
| `POST` | `/api/actresses/{actress_id}/sync_jellyfin` | `external-write` | - | path:actress_id* | - | Sync Actress Jellyfin |
| `GET` | `/api/amane/config` | `read-only` | - | - | - | Get Amane Config |
| `POST` | `/api/amane/config` | `local-write` | - | - | required | Update Amane Config |
| `POST` | `/api/amane/heal` | `local-write` | - | query:limit, query:older_than_seconds, query:dry_run | - | Heal Stuck Amane Movies |
| `POST` | `/api/amane/heal/{movie_id}` | `local-write` | - | path:movie_id*, query:number | - | Heal One Amane Movie |
| `POST` | `/api/amane/ingest` | `local-write` | - | query:wait | required | Run Amane Ingest Endpoint |
| `POST` | `/api/amane/ingest/batch` | `local-write` | - | query:wait | required | Run Amane Ingest Batch |
| `GET` | `/api/amane/ingest/config` | `read-only` | - | - | - | Get Amane Ingest Config |
| `POST` | `/api/amane/ingest/config` | `local-write` | - | - | required | Update Amane Ingest Config |
| `POST` | `/api/amane/ingest/visibility` | `local-write` | - | query:refresh | required | Confirm Amane Ingest Visibility |
| `GET` | `/api/amane/inventory` | `read-only` | - | query:force | - | Get Amane Inventory |
| `GET` | `/api/amane/libraries` | `read-only` | - | query:force | - | List Amane Libraries |
| `GET` | `/api/amane/media` | `read-only` | - | query:number, query:limit | - | Search Amane Media |
| `GET` | `/api/amane/presence` | `read-only` | - | query:movie_id*, query:number | - | Check Amane Library Presence |
| `GET` | `/api/amane/selfcheck` | `read-only` | - | - | - | Get Amane Selfcheck |
| `GET` | `/api/amane/status` | `read-only` | - | - | - | Get Amane Status |
| `GET` | `/api/amane/stuck` | `read-only` | - | query:limit, query:older_than_seconds | - | List Stuck Amane Movies |
| `GET` | `/api/amane/tasks` | `read-only` | - | query:limit, query:status | - | List Amane Tasks |
| `POST` | `/api/amane/tasks/organize` | `local-write` | - | - | required | Submit Amane Organize |
| `POST` | `/api/amane/tasks/refresh` | `local-write` | - | - | required | Submit Amane Refresh |
| `POST` | `/api/amane/tasks/scrape` | `local-write` | - | - | required | Submit Amane Scrape |
| `GET` | `/api/amane/tasks/{task_id}` | `read-only` | - | path:task_id* | - | Get Amane Task |
| `POST` | `/api/auth/login` | `local-write` | - | - | required | Auth Login |
| `POST` | `/api/auth/setup` | `local-write` | - | - | required | Auth Setup |
| `GET` | `/api/auth/status` | `read-only` | - | - | - | Auth Status |
| `GET` | `/api/automation/candidates` | `read-only` | - | query:status, query:candidate_type, query:limit, query:offset | - | Get Automation Candidates |
| `POST` | `/api/automation/candidates/approve_all` | `local-write` | - | - | optional | Approve All Automation Candidates |
| `POST` | `/api/automation/candidates/{candidate_id}/approve` | `local-write` | - | path:candidate_id* | required | Approve Automation Candidate |
| `POST` | `/api/automation/candidates/{candidate_id}/reject` | `local-write` | - | path:candidate_id* | required | Reject Automation Candidate |
| `GET` | `/api/automation/settings` | `read-only` | - | - | - | Get Automation Settings |
| `POST` | `/api/automation/settings` | `local-write` | - | - | required | Update Automation Settings |
| `GET` | `/api/bi/overview` | `read-only` | - | query:refresh | - | Get Bi Overview |
| `GET` | `/api/bi/settings` | `read-only` | - | - | - | Get Bi Settings |
| `POST` | `/api/bi/settings` | `local-write` | - | - | required | Update Bi Settings |
| `POST` | `/api/cache/cleanup` | `local-write` | - | - | - | Cleanup Cache To Safe Watermark |
| `GET` | `/api/cache/images/stats` | `read-only` | - | - | - | Get Image Cache Stats |
| `POST` | `/api/cache/invalidate` | `local-write` | - | - | required | Invalidate Cache |
| `GET` | `/api/cache/stats` | `read-only` | - | - | - | Get Cache Stats |
| `POST` | `/api/cache/warmup` | `local-write` | - | - | required | Warmup Cache |
| `POST` | `/api/category/refresh` | `local-write` | - | query:url* | - | Refresh Category |
| `GET` | `/api/cleanup/candidates` | `read-only` | - | query:status, query:limit, query:offset | - | Get Cleanup Candidates |
| `POST` | `/api/cleanup/candidates/{candidate_id}/approve` | `local-write` | - | path:candidate_id* | required | Approve Cleanup Candidate |
| `POST` | `/api/cleanup/candidates/{candidate_id}/reject` | `local-write` | - | path:candidate_id* | required | Reject Cleanup Candidate |
| `GET` | `/api/config/system` | `read-only` | - | - | - | Get System Config |
| `POST` | `/api/config/system` | `local-write` | - | - | required | Update System Config |
| `GET` | `/api/detail` | `read-only` | - | query:url*, query:movie_id, query:title | - | Get Movie Detail |
| `GET` | `/api/discovery/movies` | `read-only` | - | query:source, query:page, query:rank_type, query:force_refresh | - | Get Discovery Movies Cached |
| `GET` | `/api/discovery/movies/raw` | `read-only` | - | query:source, query:page, query:rank_type | - | Get Discovery Movies |
| `POST` | `/api/discovery/preload` | `local-write` | - | query:pages | - | Preload Discovery Movies |
| `GET` | `/api/discovery/sources` | `read-only` | - | - | - | Get Discovery Sources |
| `POST` | `/api/download` | `external-write` | - | - | required | Download Movie |
| `POST` | `/api/download/magnet` | `external-write` | - | - | required | Download Magnet |
| `GET` | `/api/download/queue` | `read-only` | download-queue | query:status, query:limit, query:offset | - | List Download Queue |
| `POST` | `/api/download/queue/{task_id}/cancel` | `local-write` | download-queue | path:task_id* | - | Cancel Download Task |
| `POST` | `/api/download/queue/{task_id}/recover` | `local-write` | download-queue | path:task_id* | - | Recover Download Task |
| `POST` | `/api/download/queue/{task_id}/retry` | `local-write` | download-queue | path:task_id* | - | Retry Download Task |
| `GET` | `/api/downloads/pending/stats` | `read-only` | - | query:limit | - | Get Pending Download Stats |
| `GET` | `/api/genres` | `read-only` | - | query:type, query:force_refresh | - | Get Genres |
| `GET` | `/api/health/check` | `read-only` | - | - | - | Health Check |
| `GET` | `/api/health/deps` | `read-only` | - | - | - | Health Dependencies |
| `GET` | `/api/health/live` | `read-only` | - | - | - | Health Live |
| `POST` | `/api/homepage/refresh` | `local-write` | - | - | - | Refresh Homepage |
| `GET` | `/api/jable/hot` | `read-only` | - | query:page, query:limit, query:sort_by, query:force_refresh, query:translate | - | Get Jable Hot Movies |
| `GET` | `/api/jable/search_magnet` | `read-only` | - | query:video_id* | - | Search Jable Magnet |
| `GET` | `/api/jellyfin/behavior/insights` | `read-only` | - | query:library_id, query:scan_limit, query:sample_limit, query:strong_preference_min_plays | - | Get Jellyfin Behavior Insights |
| `GET` | `/api/jellyfin/behavior/movies` | `read-only` | - | query:bucket, query:library_id, query:limit, query:offset, query:scan_limit, query:strong_preference_min_plays | - | Get Jellyfin Behavior Movies |
| `POST` | `/api/jellyfin/curation/apply` | `external-write` | - | - | required | Apply Jellyfin Curation |
| `POST` | `/api/jellyfin/curation/scan` | `read-only` | - | query:library, query:only_gaps, query:limit | - | Scan Jellyfin Curation |
| `GET` | `/api/jellyfin/delete_progress/{task_id}` | `read-only` | - | path:task_id* | - | Get Delete Progress |
| `DELETE` | `/api/jellyfin/delete_task/{task_id}` | `external-write` | - | path:task_id* | - | Cancel Delete Task |
| `GET` | `/api/jellyfin/delete_tasks` | `read-only` | - | query:limit | - | List Delete Tasks |
| `POST` | `/api/jellyfin/favorite_actor` | `external-write` | - | - | required | Favorite Actor Sync |
| `GET` | `/api/jellyfin/image/{item_id}` | `read-only` | - | path:item_id*, query:tag, query:max_width, query:quality | - | Proxy Jellyfin Image |
| `POST` | `/api/jellyfin/items/delete` | `external-write` | - | - | required | Delete Jellyfin Items |
| `POST` | `/api/jellyfin/items/delete_async` | `external-write` | - | - | required | Delete Jellyfin Items Async |
| `GET` | `/api/jellyfin/libraries` | `read-only` | - | - | - | Get Jellyfin Libraries |
| `GET` | `/api/jellyfin/library` | `read-only` | - | query:library_id, query:watched, query:liked, query:sort_by, query:sort_order, query:query, query:limit, query:offset | - | Get Jellyfin Library |
| `POST` | `/api/jellyfin/play/{item_id}` | `local-write` | - | path:item_id*, query:player | - | Play Jellyfin Item Mpv |
| `GET` | `/api/jellyfin/sessions` | `read-only` | - | - | - | List Jellyfin Sessions |
| `GET` | `/api/jellyfin/stats` | `read-only` | - | - | - | Get Jellyfin Stats |
| `GET` | `/api/jellyfin/stream/{item_id}` | `read-only` | - | path:item_id* | - | Proxy Jellyfin Stream |
| `POST` | `/api/jellyfin/sync_status` | `external-write` | - | - | - | Sync Jellyfin Status Endpoint |
| `GET` | `/api/library/category-distribution` | `read-only` | - | query:limit | - | Get Library Category Distribution |
| `GET` | `/api/library/index` | `read-only` | - | query:refresh | - | Get Library Index |
| `GET` | `/api/lifecycle/movies` | `read-only` | - | query:movie_id, query:discovery_status, query:decision_status, query:download_status, query:organize_status, query:jellyfin_status, query:cleanup_status, query:limit, query:offset | - | Get Lifecycle Movies |
| `GET` | `/api/lifecycle/movies/{movie_id}/events` | `read-only` | - | path:movie_id*, query:limit | - | Get Lifecycle Events |
| `GET` | `/api/logs` | `read-only` | - | - | - | Get Logs |
| `GET` | `/api/magnets/select` | `read-only` | - | query:url*, query:preference, query:movie_id, query:title | - | Select Magnet |
| `GET` | `/api/movies` | `read-only` | - | query:url*, query:page, query:limit, query:force_refresh, query:translate_mode | - | Get Movies |
| `GET` | `/api/navigation` | `read-only` | - | query:force_refresh | - | Get Navigation |
| `POST` | `/api/onboarding/apply` | `local-write` | - | - | required | Apply Onboarding |
| `POST` | `/api/onboarding/block-keywords/preview` | `local-write` | - | - | optional | Preview Block Keywords |
| `POST` | `/api/onboarding/database/check` | `local-write` | - | - | optional | Check Onboarding Database |
| `POST` | `/api/onboarding/reset` | `local-write` | - | - | - | Reset Onboarding |
| `GET` | `/api/onboarding/status` | `read-only` | - | query:include_settings | - | Get Onboarding Status |
| `POST` | `/api/onboarding/test/{target}` | `local-write` | - | path:target* | optional | Test Onboarding Target |
| `GET` | `/api/proxy/image` | `read-only` | - | query:url*, query:w | - | Proxy Remote Image |
| `GET` | `/api/qb/config` | `read-only` | - | - | - | Get Qb Config |
| `POST` | `/api/qb/config` | `local-write` | - | - | required | Update Qb Config |
| `GET` | `/api/qb/dashboard` | `read-only` | - | - | - | Get Qb Dashboard |
| `GET` | `/api/qb/status` | `read-only` | - | - | - | Get Qb Status |
| `GET` | `/api/qb/torrents` | `read-only` | - | query:category, query:limit, query:offset | - | Get Qb Torrents |
| `POST` | `/api/qb/torrents/control` | `external-write` | - | - | required | Control Qb Torrents |
| `POST` | `/api/qb/torrents/filter-jav-files` | `external-write` | - | - | - | Filter Qb Jav Files |
| `POST` | `/api/scrape/jellyfin-refresh` | `external-write` | - | - | - | Refresh Jellyfin Scrape |
| `GET` | `/api/search` | `read-only` | - | query:q, query:limit, query:offset | - | Search Movies |
| `GET` | `/api/stats/dashboard` | `read-only` | - | - | - | Get Dashboard Stats |
| `GET` | `/api/subscriptions` | `read-only` | - | query:sync | - | Get Subscriptions |
| `POST` | `/api/subscriptions` | `local-write` | - | - | required | Add Subscription |
| `DELETE` | `/api/subscriptions` | `local-write` | - | query:url* | - | Delete Subscription |
| `GET` | `/api/subscriptions/covers` | `read-only` | - | query:url* | - | Get Subscription Covers |
| `POST` | `/api/subscriptions/refresh` | `local-write` | - | query:url* | - | Refresh Subscription |
| `GET` | `/api/subscriptions/stats` | `read-only` | - | query:include_decorative_covers | - | Get Subscription Stats |
| `GET` | `/api/tasks/external` | `read-only` | - | query:status, query:task_type, query:limit, query:offset | - | Get External Tasks |
| `GET` | `/api/tasks/status` | `read-only` | - | - | - | Get Task Status |
| `POST` | `/api/translate/batch` | `local-write` | - | - | required | Batch Translate |
| `GET` | `/api/translation/status` | `read-only` | - | - | - | Get Translation Status |
| `POST` | `/api/webhooks/jellyfin` | `local-write` | - | - | required | Jellyfin Webhook Route |
| `GET` | `/{full_path}` | `read-only` | - | path:full_path* | - | Spa Fallback |

`*` marks required path/query/header parameters. Request and response schemas remain authoritative in `/docs` and `/openapi.json`.

## Test commands

```powershell
.\.venv\Scripts\python.exe -m pytest backend/test_api_contract.py -q
.\.venv\Scripts\python.exe -m pytest backend/test_lifecycle_scenario.py -q
```

Docker verification must run read-only endpoints first. External-write operations are covered with mocks and are not replayed against real qBittorrent, Jellyfin, NAS paths, or production data.
