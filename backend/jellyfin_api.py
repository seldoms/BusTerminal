
import base64
import requests
import logging
import re
from typing import Any, Optional, Dict, List
from database import db, display_value_for_log, parse_bool_value, parse_int_value
from net_policy import internal_session

logger = logging.getLogger(__name__)
JELLYFIN_PROBE_TIMEOUT_SECONDS = 3

# 清洗扫描要用的字段。刻意不含 People / MediaSources：带上它们 Jellyfin 每次要多做
# 几次关联查询，200 条一页要 3~4 秒，全库扫描扛不住。
CURATION_ITEM_FIELDS = (
    "Name,OriginalTitle,Overview,Taglines,ProviderIds,Genres,Tags,Studios,"
    "ProductionYear,PremiereDate,LockedFields,ImageTags,Path,MediaSources"
)

# POST /Items/{id} 是整对象替换：这些字段缺一个就会 500 并写坏数据，必须全部显式带上。
FULL_ITEM_UPDATE_FIELDS = (
    "Name", "OriginalTitle", "Overview", "Taglines", "ProviderIds", "Genres", "Tags",
    "Studios", "ProductionYear", "PremiereDate", "OfficialRating", "CommunityRating",
    "CriticRating", "Status", "LockedFields", "SortName",
)

# Jellyfin 是内网服务：所有请求都走 net_policy.internal_session()，它在**
# 默认情况**下不跟随系统代理（trust_env=False）。本机若配了代理而不绕开，
# 请求会被发到代理并在随机高端口上超时。个别环境确实需要代理时，
# 把 net_policy 的开关打开即可，无需改本文件的代码。

# 并改为只从数据库或环境变量读取 JELLYFIN_HOST/JELLYFIN_API_KEY。

def is_valid_jellyfin_item_id(item_id) -> bool:
    value = str(item_id or "").strip()
    return bool(value and len(value) <= 128 and re.fullmatch(r"[A-Za-z0-9_-]+", value))


def _clean_str_list(value: Any) -> List[str]:
    """列表字段兜底：非列表一律当空，列表里的 None/非字符串剔掉。

    ⚠️ 剔 None 不是洁癖：条目里残留 `Genres: ["a", null]` 会让 Jellyfin 的
    `DtoService.AttachGenreItems` 抛 ArgumentNullException，`GET /Users/{uid}/Items/{id}`
    从此永久 500（生产 2026-10-13 实际发生过）。
    """
    return [x for x in _list_value(value) if isinstance(x, str) and x.strip()]


def build_full_item_update_payload(metadata: Dict) -> Dict:
    """把读回来的条目 DTO 补成一份可安全整体回写的 payload。

    Jellyfin 10.10.7 的 `POST /Items/{id}` 是整对象替换语义，缺字段即 500，
    所以这里对每个字段做显式兜底（list → []、None → {} / "" / None）。
    """
    source = _dict_value(metadata)
    payload: Dict[str, Any] = {}
    for field in FULL_ITEM_UPDATE_FIELDS:
        value = source.get(field)
        if field in ("Taglines", "Genres", "Tags", "Studios"):
            payload[field] = _clean_str_list(value)
        elif field == "LockedFields":
            payload[field] = _clean_str_list(value)
        elif field == "ProviderIds":
            payload[field] = _dict_value(value)
        elif isinstance(value, str):
            payload[field] = value
        else:
            payload[field] = value
    return payload


def exception_type(exc: Exception) -> str:
    return type(exc).__name__


def _dict_value(value) -> Dict:
    return value if isinstance(value, dict) else {}


def _list_value(value) -> List:
    return value if isinstance(value, list) else []


def _dict_items(value) -> List[Dict]:
    return [item for item in _list_value(value) if isinstance(item, dict)]


def _is_favorite_item(item: Dict) -> bool:
    item = _dict_value(item)
    user_data = _dict_value(item.get("UserData"))
    return parse_bool_value(
        user_data.get("IsFavorite", item.get("IsFavorite")),
        False,
    )


def _merge_items_by_id_or_name(items: List[Dict]) -> List[Dict]:
    merged = []
    seen = set()
    for item in items:
        item = _dict_value(item)
        key = str(item.get("Id") or item.get("Name") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged

class JellyfinClient:
    """
    Jellyfin API 客户端，用于补全人员(女优)元数据。
    """
    def __init__(self):
        self._item_cache: Dict[str, Dict] = {}
        self.reload_config()

    def reload_config(self):
        """从数据库或环境变量重新加载 Jellyfin 配置，测试环境使用本地兜底值。"""
        import os

        self.host = db.get_config('jellyfin_host') or os.getenv('JELLYFIN_HOST', '')
        self.api_key = db.get_config('jellyfin_api_key') or os.getenv('JELLYFIN_API_KEY', '')
        self.user_id = db.get_config('jellyfin_user_id') or os.getenv('JELLYFIN_USER_ID', '')
        self.host = self.host.rstrip('/') if self.host else ''
        logger.info("JellyfinClient config reloaded. Configured: %s", bool(self.host and self.api_key))

    def _get_headers(self):
        return {
            "X-Emby-Token": self.api_key,
            "Accept": "application/json"
        }

    def _get_auth_headers(self):
        # [NEW] Some endpoints might prefer this format
        return {
            "X-Emby-Token": self.api_key,
            "X-MediaBrowser-Token": self.api_key,
            "Accept": "application/json"
        }

    def get_server_info(self) -> Optional[Dict]:
        """获取 Jellyfin 服务基础信息。"""
        if not self.host or not self.api_key:
            return None

        try:
            url = f"{self.host.rstrip('/')}/System/Info"
            resp = internal_session().get(
                url,
                headers=self._get_auth_headers(),
                timeout=JELLYFIN_PROBE_TIMEOUT_SECONDS,
            )
            if resp.status_code == 200:
                return resp.json()
            return None
        except (requests.exceptions.RequestException, ConnectionError, TimeoutError, OSError) as e:
            logger.error("Jellyfin API Connection Error (server_info): %s", exception_type(e))
            raise
        except Exception as e:
            logger.error("Jellyfin API Error (server_info): %s", exception_type(e))
            return None

    def get_person_info(self, name: str) -> Optional[Dict]:
        """通过姓名从 Jellyfin 获取人员详情"""
        if not self.host or not self.api_key:
            return None
        
        try:
            import urllib.parse
            safe_name = urllib.parse.quote(name)
            url = f"{self.host.rstrip('/')}/Persons/{safe_name}"
            resp = internal_session().get(url, headers=self._get_headers(), timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                # 构造标准返回
                person = {
                    "name": data.get("Name"),
                    "id": data.get("Id"),
                    "bio": data.get("Overview"),
                    # [FIX] 使用后端代理加载图片，避免内网隔离导致前端无法加载
                    "avatar": f"/api/jellyfin/image/{data.get('Id')}" if data.get("HasPrimaryImage") else None,
                    "premiere_date": data.get("PremiereDate"), 
                }
                return person
            return None
        except Exception as e:
            logger.error("Jellyfin API Error (get_person): %s", exception_type(e))
            return None

    def search_people(self, query: str) -> List[Dict]:
        """搜索人员"""
        if not self.host or not self.api_key:
            return []
        
        try:
            url = f"{self.host.rstrip('/')}/Persons"
            params = {"SearchTerm": query}
            resp = internal_session().get(url, headers=self._get_headers(), params=params, timeout=10)
            if resp.status_code == 200:
                return _dict_items(resp.json().get("Items"))
            return []
        except Exception as e:
            logger.error("Jellyfin API Error (search_people): %s", exception_type(e))
            return []

    def _discover_user_id(self) -> Optional[str]:
        """尝试自动发现用户 ID (取第一个有权限的用户)"""
        if self.user_id:
            return self.user_id
        
        try:
            url = f"{self.host.rstrip('/')}/Users"
            resp = internal_session().get(url, headers=self._get_headers(), timeout=10)
            if resp.status_code == 200:
                users = resp.json()
                if users:
                    # 获取第一个用户，通常是管理员
                    uid = users[0]['Id']
                    logger.info(
                        "Auto-discovered Jellyfin user: name=%s id=%s",
                        display_value_for_log(users[0].get('Name')),
                        display_value_for_log(uid),
                    )
                    # 保存到配置以备后用
                    db.set_config('jellyfin_user_id', uid)
                    self.user_id = uid
                    return uid
            return None
        except Exception as e:
            logger.error("Jellyfin API Error (discover_user): %s", exception_type(e))
            return None

    def get_libraries(self) -> List[Dict]:
        """获取所有媒体库列表"""
        uid = self._discover_user_id()
        if not self.host or not self.api_key or not uid:
            return []
        try:
            url = f"{self.host.rstrip('/')}/Users/{uid}/Views"
            resp = internal_session().get(url, headers=self._get_headers(), timeout=10)
            if resp.status_code == 200:
                items = _list_value(resp.json().get("Items", []))
                libraries = []
                for it in items:
                    item = _dict_value(it)
                    item_id = str(item.get("Id") or "").strip()
                    if not item_id:
                        continue
                    libraries.append({
                        "id": item_id,
                        "name": item.get("Name") or item_id,
                        "type": item.get("CollectionType") or "unknown",
                    })
                return libraries
            return []
        except Exception as e:
            logger.error("Jellyfin API Error (get_libraries): %s", exception_type(e))
            return []

    def get_library_items(
        self,
        library_id: Optional[str] = None,
        is_played: Optional[bool] = None,
        is_favorite: Optional[bool] = None,
        sort_by: str = "DateCreated",
        sort_order: str = "Descending",
        search: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        fields: Optional[str] = None,
    ) -> Dict:
        """按条件查询媒体库中的影片，直接返回 Jellyfin 数据（含封面 URL）。

        fields 用于缩小返回字段：只要 Id/Name 的批量扫描（如已入库索引）传 "Name" 即可，
        带上 People/MediaSources 会让 Jellyfin 每次多做好几次关联查询，500 条一页要 3~4 秒。
        """
        uid = self._discover_user_id()
        if not self.host or not self.api_key or not uid:
            return {"items": [], "total": 0}
        try:
            url = f"{self.host.rstrip('/')}/Users/{uid}/Items"
            params = {
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "Fields": fields or "Name,Overview,PremiereDate,DateCreated,CommunityRating,UserData,ImageTags,People,MediaSources,RunTimeTicks,Genres",
                "SortBy": sort_by,
                "SortOrder": sort_order,
            }
            needs_local_favorite_filter = is_favorite is False
            if needs_local_favorite_filter:
                params["Limit"] = max(offset + limit, 5000)
                params["StartIndex"] = 0
            else:
                params["Limit"] = limit
                params["StartIndex"] = offset
            if library_id:
                params["ParentId"] = library_id
            # 过滤条件
            filters = []
            if is_played is True:
                filters.append("IsPlayed")
            elif is_played is False:
                filters.append("IsUnplayed")
            if is_favorite is True:
                filters.append("IsFavorite")
            if filters:
                params["Filters"] = ",".join(filters)
            if search:
                params["SearchTerm"] = search
            resp = internal_session().get(url, headers=self._get_headers(), params=params, timeout=15)
            if resp.status_code != 200:
                return {"items": [], "total": 0, "degraded": True, "message": "jellyfin_unavailable"}
            data = resp.json()
            raw_items = _list_value(data.get("Items", []))
            if needs_local_favorite_filter:
                raw_items = [
                    item for item in raw_items
                    if not parse_bool_value(_dict_value(_dict_value(item).get("UserData")).get("IsFavorite"), False)
                ]
                total = len(raw_items)
                raw_items = raw_items[offset:offset + limit]
            else:
                total = parse_int_value(data.get("TotalRecordCount"), len(raw_items), minimum=0)
            items = []
            for it in raw_items:
                it = _dict_value(it)
                jf_id = str(it.get("Id") or "").strip()
                if not jf_id:
                    continue
                image_tags = _dict_value(it.get("ImageTags"))
                primary_tag = image_tags.get("Primary", "")
                cover = (
                    f"/api/jellyfin/image/{jf_id}?tag={primary_tag}"
                    if primary_tag else
                    f"/api/jellyfin/image/{jf_id}"
                )
                user_data = _dict_value(it.get("UserData"))
                media_sources = _list_value(it.get("MediaSources"))
                file_size = parse_int_value(it.get("Size"), 0, minimum=0)
                for source in media_sources:
                    source_size = parse_int_value(_dict_value(source).get("Size"), 0, minimum=0)
                    if source_size > file_size:
                        file_size = source_size
                title = str(it.get("Name") or jf_id)
                premiere_date = it.get("PremiereDate")
                release_date = premiere_date[:10] if isinstance(premiere_date, str) else ""
                items.append({
                    "jellyfin_id": jf_id,
                    "id": title,  # 用片名作为展示 ID
                    "title": title,
                    "cover": cover,
                    "year": it.get("ProductionYear"),
                    "rating": it.get("CommunityRating"),
                    "added_at": it.get("DateCreated"),
                    "release_date": release_date,
                    "is_watched": parse_bool_value(user_data.get("Played"), False),
                    "is_favorite": parse_bool_value(user_data.get("IsFavorite"), False),
                    "play_count": parse_int_value(user_data.get("PlayCount"), 0, minimum=0),
                    "last_played": user_data.get("LastPlayedDate"),
                    "file_size": file_size,
                    # 时长与类型：BI 统计要按类型/时长出图，必须透出。
                    # RunTimeTicks 是 Jellyfin 的标准时长字段（1 tick = 100ns）。
                    "runtime_ticks": parse_int_value(it.get("RunTimeTicks"), 0, minimum=0),
                    "runtime_minutes": round(
                        parse_int_value(it.get("RunTimeTicks"), 0, minimum=0) / 600_000_000, 1
                    ),
                    "genres": [str(g) for g in _list_value(it.get("Genres")) if str(g).strip()],
                })
            return {"items": items, "total": total}
        except Exception as e:
            logger.error("Jellyfin API Error (get_library_items): %s", exception_type(e))
            return {"items": [], "total": 0, "degraded": True, "message": "jellyfin_unavailable"}

    def delete_items(self, item_ids: List[str]) -> Dict:
        """批量从 Jellyfin 删除项目"""
        if not self.host or not self.api_key:
            return {"success": False, "results": []}
        results = []
        for item_id in item_ids:
            if not is_valid_jellyfin_item_id(item_id):
                logger.warning("Jellyfin: skipped delete for invalid item id")
                results.append({"id": item_id, "success": False, "error": "invalid_item_id"})
                continue
            try:
                url = f"{self.host.rstrip('/')}/Items/{item_id}"
                resp = internal_session().delete(url, headers=self._get_headers(), timeout=10)
                ok = resp.status_code in [200, 204]
                results.append({"id": item_id, "success": ok})
                if not ok:
                    logger.error(
                        "Jellyfin: failed to delete item=%s status=%s",
                        display_value_for_log(item_id),
                        resp.status_code,
                    )
            except Exception as e:
                logger.error(
                    "Jellyfin delete error for item=%s: %s",
                    display_value_for_log(item_id),
                    exception_type(e),
                )
                results.append({"id": item_id, "success": False, "error": exception_type(e)})
        return {"success": True, "results": results}

    def get_favorite_movies(self) -> List[Dict]:
        """获取用户收藏的电影"""
        uid = self._discover_user_id()
        if not self.host or not self.api_key or not uid:
            return []
            
        try:
            url = f"{self.host.rstrip('/')}/Users/{uid}/Items"
            params = {
                "Filters": "IsFavorite",
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "Fields": "ProviderIds,People"
            }
            resp = internal_session().get(url, headers=self._get_headers(), params=params, timeout=15)
            if resp.status_code == 200:
                return _dict_items(resp.json().get("Items"))
            return []
        except Exception as e:
            logger.error("Jellyfin API Error (favorites): %s", exception_type(e))
            return []

    def get_item_detail(self, item_id: str, fields: str = "ProviderIds,People") -> Optional[Dict]:
        """获取单个 Jellyfin 项目详情，用于列表接口字段缺失时兜底。"""
        uid = self._discover_user_id()
        item_id = str(item_id or "").strip()
        if not self.host or not self.api_key or not uid or not is_valid_jellyfin_item_id(item_id):
            return None

        params = {"Fields": fields} if fields else {}
        endpoints = [
            f"{self.host.rstrip('/')}/Users/{uid}/Items/{item_id}",
            f"{self.host.rstrip('/')}/Items/{item_id}",
        ]
        for url in endpoints:
            try:
                resp = internal_session().get(url, headers=self._get_headers(), params=params, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    return data if isinstance(data, dict) else None
            except Exception as e:
                logger.error("Jellyfin API Error (item_detail): %s", exception_type(e))
                return None
        return None

    def get_favorite_people(self) -> List[Dict]:
        """Return Jellyfin people marked as favorite by the configured user."""
        uid = self._discover_user_id()
        if not self.host or not self.api_key or not uid:
            return []

        people = []
        try:
            url = f"{self.host.rstrip('/')}/Users/{uid}/Items"
            params = {
                "Filters": "IsFavorite",
                "IncludeItemTypes": "Person",
                "Recursive": "true",
                "Fields": "Overview,ImageTags,UserData",
            }
            resp = internal_session().get(url, headers=self._get_headers(), params=params, timeout=15)
            if resp.status_code == 200:
                people.extend(_dict_items(resp.json().get("Items")))
            if people:
                return _merge_items_by_id_or_name(people)

            fallback_url = f"{self.host.rstrip('/')}/Persons"
            fallback_params = {
                "UserId": uid,
                "IsFavorite": "true",
                "Fields": "Overview,ImageTags,UserData",
                "Limit": 1000,
            }
            fallback_resp = internal_session().get(
                fallback_url,
                headers=self._get_headers(),
                params=fallback_params,
                timeout=15,
            )
            if fallback_resp.status_code == 200:
                people.extend(
                    item for item in _dict_items(fallback_resp.json().get("Items"))
                    if _is_favorite_item(item)
                )
            return _merge_items_by_id_or_name(people)
        except Exception as e:
            logger.error("Jellyfin API Error (favorite_people): %s", exception_type(e))
            return []

    def get_watched_movies(self) -> List[Dict]:
        """获取用户已看的电影"""
        uid = self._discover_user_id()
        if not self.host or not self.api_key or not uid:
            return []
            
        try:
            # Filters=IsPlayed & IncludeItemTypes=Movie
            url = f"{self.host.rstrip('/')}/Users/{uid}/Items"
            params = {
                "Filters": "IsPlayed",
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "Fields": "ProviderIds"
            }
            resp = internal_session().get(url, headers=self._get_headers(), params=params, timeout=15)
            if resp.status_code == 200:
                return _dict_items(resp.json().get("Items"))
            return []
        except Exception as e:
            logger.error("Jellyfin API Error (watched): %s", exception_type(e))
            return []

    def delete_item(self, item_id: str) -> bool:
        """从 Jellyfin 删除项目"""
        if not self.host or not self.api_key:
            return False
        if not is_valid_jellyfin_item_id(item_id):
            logger.warning("Jellyfin: skipped delete for invalid item id")
            return False
            
        try:
            url = f"{self.host.rstrip('/')}/Items/{item_id}"
            resp = internal_session().delete(url, headers=self._get_headers(), timeout=10)
            if resp.status_code in [200, 204]:
                logger.info("Jellyfin: item deleted successfully: %s", display_value_for_log(item_id))
                return True
            logger.error(
                "Jellyfin: failed to delete item=%s status=%s",
                display_value_for_log(item_id),
                resp.status_code,
            )
            return False
        except Exception as e:
            logger.error("Jellyfin API Error (delete_item): %s", exception_type(e))
            return False

    def toggle_favorite_item(self, item_id: str, is_favorite: bool) -> bool:
        """切换项目的收藏状态"""
        uid = self._discover_user_id()
        if not self.host or not self.api_key or not uid:
            return False
        if not is_valid_jellyfin_item_id(item_id):
            logger.warning("Jellyfin: skipped favorite toggle for invalid item id")
            return False
            
        try:
            method = "POST" if is_favorite else "DELETE"
            url = f"{self.host.rstrip('/')}/Users/{uid}/FavoriteItems/{item_id}"
            resp = internal_session().request(method, url, headers=self._get_headers(), timeout=10)
            if resp.status_code in [200, 204]:
                logger.info(
                    "Jellyfin: favorite status updated item=%s favorite=%s",
                    display_value_for_log(item_id),
                    is_favorite,
                )
                return True
            return False
        except Exception as e:
            logger.error("Jellyfin API Error (toggle_favorite): %s", exception_type(e))
            return False

    def get_item_by_name(self, name: str, item_types: List[str] = ["Movie"]) -> Optional[Dict]:
        """按名称查找项目 (通常用于根据番号找电影或按姓名找演员)"""
        if not self.host or not self.api_key:
            return None
            
        try:
            url = f"{self.host.rstrip('/')}/Items"
            params = {
                "SearchTerm": name,
                "IncludeItemTypes": ",".join(item_types),
                "Recursive": "true"
            }
            resp = internal_session().get(url, headers=self._get_headers(), params=params, timeout=10)
            if resp.status_code == 200:
                items = _dict_items(resp.json().get("Items"))
                if items:
                    # 返回第一个匹配项
                    return items[0]
            return None
        except Exception as e:
            logger.error("Jellyfin API Error (get_item_by_name): %s", exception_type(e))
            return None

    def refresh_library(self) -> bool:
        """触发一次全库扫描。新文件入库后必须靠它让 Jellyfin 发现。"""
        if not self.host or not self.api_key:
            return False
        try:
            url = f"{self.host.rstrip('/')}/Library/Refresh"
            resp = internal_session().post(url, headers=self._get_headers(), timeout=30)
            if resp.status_code in (200, 204):
                return True
            logger.warning("Jellyfin refresh failed: %s", resp.status_code)
            return False
        except Exception as e:
            logger.error("Jellyfin API Error (refresh_library): %s", exception_type(e))
            return False

    def list_library_items_raw(
        self,
        library_id: Optional[str] = None,
        fields: str = CURATION_ITEM_FIELDS,
        page_size: int = 200,
    ) -> List[Dict]:
        """翻页拉库内全部影片的原始字段。

        清洗扫描必须走这里而不是 get_item_detail()：单条 `GET /Users/{uid}/Items/{id}`
        在部分条目上会整体 500（Genres 含 null 的历史脏数据），列表接口不受影响。
        """
        uid = self._discover_user_id()
        if not self.host or not self.api_key or not uid:
            return []
        url = f"{self.host.rstrip('/')}/Users/{uid}/Items"
        collected: List[Dict] = []
        start = 0
        try:
            while True:
                params = {
                    "IncludeItemTypes": "Movie",
                    "Recursive": "true",
                    "Fields": fields,
                    "SortBy": "SortName",
                    "SortOrder": "Ascending",
                    "Limit": max(page_size, 1),
                    "StartIndex": start,
                }
                if library_id:
                    params["ParentId"] = library_id
                resp = internal_session().get(
                    url, headers=self._get_headers(), params=params, timeout=30
                )
                if resp.status_code != 200:
                    logger.error("Jellyfin API Error (list_library_items_raw): %s", resp.status_code)
                    return collected
                data = resp.json()
                batch = _dict_items(data.get("Items"))
                collected.extend(batch)
                start += len(batch)
                total = parse_int_value(data.get("TotalRecordCount"), len(collected), minimum=0)
                if not batch or start >= total:
                    break
        except Exception as e:
            logger.error("Jellyfin API Error (list_library_items_raw): %s", exception_type(e))
        return collected

    def load_items_for_ids(self, item_ids: List[str], fields: str = CURATION_ITEM_FIELDS) -> None:
        """按 id 批量读条目进内存快照。

        写入前必须有完整 DTO：POST /Items/{id} 是整对象替换，凭空构造 payload 会写坏数据。
        """
        ids = [str(i).strip() for i in item_ids if is_valid_jellyfin_item_id(i)]
        if not ids or not self.host or not self.api_key:
            return
        uid = self._discover_user_id()
        if not uid:
            return
        url = f"{self.host.rstrip('/')}/Users/{uid}/Items"
        try:
            resp = internal_session().get(
                url,
                headers=self._get_headers(),
                params={
                    "Fields": fields,
                    "IncludeItemTypes": "Movie",
                    "Recursive": "true",
                    "Ids": ",".join(ids),
                    "Limit": max(len(ids), 1),
                },
                timeout=30,
            )
            if resp.status_code != 200:
                logger.warning("Jellyfin load_items_for_ids failed: %s", resp.status_code)
                return
            for item in _dict_items(resp.json().get("Items")):
                item_id = str(item.get("Id") or "").strip()
                if item_id:
                    self._item_cache[item_id] = item
        except Exception as e:
            logger.error("Jellyfin API Error (load_items_for_ids): %s", exception_type(e))

    def item_snapshot(self, item_id: str) -> Optional[Dict]:
        """取条目完整 DTO：先看内存快照，再按需单查。"""
        item_id = str(item_id or "").strip()
        if not item_id:
            return None
        cached = self._item_cache.get(item_id)
        if cached:
            return cached
        if not is_valid_jellyfin_item_id(item_id) or not self.host or not self.api_key:
            return None
        uid = self._discover_user_id()
        if not uid:
            return None
        for url in (
            f"{self.host.rstrip('/')}/Users/{uid}/Items/{item_id}",
            f"{self.host.rstrip('/')}/Items/{item_id}",
        ):
            try:
                resp = internal_session().get(
                    url, headers=self._get_headers(),
                    params={"Fields": CURATION_ITEM_FIELDS}, timeout=15,
                )
                if resp.status_code == 200:
                    item = resp.json()
                    if isinstance(item, dict) and item.get("Id"):
                        self._item_cache[item_id] = item
                        return item
            except Exception as e:
                logger.debug("Jellyfin item_snapshot probe failed: %s", exception_type(e))
        return None

    def forget_items(self, item_ids: List[str]) -> None:
        for item_id in item_ids or []:
            self._item_cache.pop(str(item_id or "").strip(), None)

    def update_item_metadata(self, item_id: str, metadata: Dict) -> Dict:
        """整体回写条目元数据。

        ⚠️ Jellyfin 10.10.7 的 `POST /Items/{id}` 走的是「整对象替换」语义：
        少传 Genres/Tags/Studios/ProviderIds/Taglines/LockedFields 里的任何一个，
        都会因 `Enumerable.Except(first, second)` 收到 null 而 500，**并且**把字段写成
        含 null 的数组，之后 `GET /Users/{uid}/Items/{id}` 会永久 500。
        所以调用方必须把读回来的完整 DTO 交给本方法，只在上面改要改的字段。

        返回 {"success": bool, "status_code": int, "message": str}。
        """
        if not is_valid_jellyfin_item_id(item_id):
            return {"success": False, "status_code": 0, "message": "invalid_item_id"}
        if not self.host or not self.api_key:
            return {"success": False, "status_code": 0, "message": "jellyfin_not_configured"}
        try:
            payload = build_full_item_update_payload(metadata)
            url = f"{self.host.rstrip('/')}/Items/{item_id}"
            resp = internal_session().post(
                url,
                headers={**self._get_headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=30,
            )
            if resp.status_code in (200, 204):
                return {"success": True, "status_code": resp.status_code, "message": ""}
            logger.warning(
                "Jellyfin update_item_metadata failed: status=%s item=%s",
                resp.status_code,
                display_value_for_log(item_id),
            )
            return {
                "success": False,
                "status_code": resp.status_code,
                "message": (resp.text or "")[:200],
            }
        except Exception as e:
            logger.error("Jellyfin API Error (update_item_metadata): %s", exception_type(e))
            return {"success": False, "status_code": 0, "message": exception_type(e)}

    def upload_item_image(self, item_id: str, image_bytes: bytes, image_index: int = 0) -> Dict:
        """上传条目封面。

        ⚠️ body 必须是 **base64 文本**：multipart 会 400 `Incorrect ContentType.`，
        裸二进制会 500（服务端 ImageSaver 内部按 base64 解 → FormatException）。
        上传后只生成 ImageTags.Primary，Thumb 要靠 Refresh 或库扫描补。
        """
        if not is_valid_jellyfin_item_id(item_id):
            return {"success": False, "status_code": 0, "message": "invalid_item_id"}
        if not self.host or not self.api_key:
            return {"success": False, "status_code": 0, "message": "jellyfin_not_configured"}
        if not image_bytes:
            return {"success": False, "status_code": 0, "message": "empty_image"}
        try:
            body = base64.b64encode(image_bytes).decode("ascii")
            url = f"{self.host.rstrip('/')}/Items/{item_id}/Images/Primary"
            resp = internal_session().post(
                url,
                headers={**self._get_headers(), "Content-Type": "image/jpeg"},
                params={"index": max(image_index, 0)},
                data=body,
                timeout=120,
            )
            if resp.status_code in (200, 204):
                return {"success": True, "status_code": resp.status_code, "message": ""}
            logger.warning(
                "Jellyfin upload_item_image failed: status=%s item=%s",
                resp.status_code,
                display_value_for_log(item_id),
            )
            return {
                "success": False,
                "status_code": resp.status_code,
                "message": (resp.text or "")[:200],
            }
        except Exception as e:
            logger.error("Jellyfin API Error (upload_item_image): %s", exception_type(e))
            return {"success": False, "status_code": 0, "message": exception_type(e)}

    def search_items(self, term: str, item_types: Optional[List[str]] = None, limit: int = 10) -> List[Dict]:
        """按关键词搜索条目。可见性确认用它判断新片是否已被 Jellyfin 收录。"""
        if not self.host or not self.api_key or not str(term or "").strip():
            return []
        try:
            url = f"{self.host.rstrip('/')}/Items"
            params = {
                "SearchTerm": term,
                "IncludeItemTypes": ",".join(item_types or ["Movie"]),
                "Recursive": "true",
                "Limit": limit,
            }
            resp = internal_session().get(url, headers=self._get_headers(), params=params, timeout=10)
            if resp.status_code == 200:
                return _dict_items(resp.json().get("Items"))
            return []
        except Exception as e:
            logger.error("Jellyfin API Error (search_items): %s", exception_type(e))
            return []

jellyfin_client = JellyfinClient()
