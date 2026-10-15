from typing import Dict, Tuple


RISK_READ_ONLY = "read-only"
RISK_LOCAL_WRITE = "local-write"
RISK_EXTERNAL_WRITE = "external-write"

# 虽是 POST 但纯读：不发任何写请求，返回清洗计划。
_READ_ONLY_POST_OPERATIONS = {
    ("POST", "/api/jellyfin/curation/scan"),
}

_EXTERNAL_WRITE_OPERATIONS = {
    ("POST", "/api/download"),
    ("POST", "/api/download/magnet"),
    ("POST", "/api/qb/torrents/control"),
    ("POST", "/api/qb/torrents/filter-jav-files"),
    ("POST", "/api/actresses/sync_all_jellyfin"),
    ("POST", "/api/actresses/{actress_id}/sync_jellyfin"),
    ("POST", "/api/jellyfin/items/delete"),
    ("POST", "/api/jellyfin/items/delete_async"),
    ("DELETE", "/api/jellyfin/delete_task/{task_id}"),
    ("POST", "/api/jellyfin/sync_status"),
    ("POST", "/api/scrape/jellyfin-refresh"),
    ("POST", "/api/jellyfin/favorite_actor"),
    # 清洗会改写 Jellyfin 条目元数据并上传封面，与删除同属外部写。
    ("POST", "/api/jellyfin/curation/apply"),
}


def classify_operation(method: str, path: str) -> str:
    operation = (str(method or "").upper(), str(path or ""))
    if operation in _EXTERNAL_WRITE_OPERATIONS:
        return RISK_EXTERNAL_WRITE
    if operation in _READ_ONLY_POST_OPERATIONS:
        return RISK_READ_ONLY
    if operation[0] in {"POST", "PUT", "PATCH", "DELETE"}:
        return RISK_LOCAL_WRITE
    return RISK_READ_ONLY


def operation_counts(schema: Dict) -> Dict[str, int]:
    counts = {RISK_READ_ONLY: 0, RISK_LOCAL_WRITE: 0, RISK_EXTERNAL_WRITE: 0}
    for path, path_item in schema.get("paths", {}).items():
        for method in path_item:
            normalized = method.upper()
            if normalized not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            counts[classify_operation(normalized, path)] += 1
    return counts

