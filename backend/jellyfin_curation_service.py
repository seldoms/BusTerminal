"""Jellyfin 现有资源清洗：扫描出缺口 + 按需执行。

设计要点（都来自生产实测，见 docs/JELLYFIN_CURATION_PLAN.md）：
1. 扫描只读，永远不写；执行必须显式传 item_ids，且受 jellyfin_curation_enabled 开关保护。
2. 写条目前必须先拉完整 DTO、整体回写。Jellyfin 10.10.7 的 POST /Items/{id} 是整对象
   替换，局部 payload 会 500 并把 Genres 写成含 null 的数组，之后单条详情端点永久 500。
3. 封面走本地 cover_cache 字节 → base64 上传。Jellyfin 的 RemoteImages 对番号恒空，
   远程下载那条路（POST /Items/{id}/RemoteImages/...）拿不到图。
"""
import re
from typing import Any, Dict, List, Optional, Tuple

from database import db, strip_translation_control_tokens, is_bad_translation_text
from jellyfin_api import jellyfin_client
from image_cache_service import image_cache_service
from movie_code import normalize_movie_code
from number_parsing import extract_number

# 平/片假名（含长音符 ー U+30FC 也会落进这个区间）
KANA_PATTERN = re.compile(r"[\u3040-\u30ff]")
CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")
# 片假名单独判：厂牌名（まあや/ソ/RS 等）多是片假名，直译出来常是「出色地」这种无意义短语
KATAKANA_PATTERN = re.compile(r"[\u30a0-\u30ff]")

# 译文质量红线：太短的一律不用（实测 translations 表有 4,978 条长度 < 8，多为厂牌名直译）
MIN_TRANSLATION_LENGTH = 8

# Jellyfin 标题里常见的脏尾巴：amane 写 NFO 时把演员名拼在标题后
_TRAILING_ACTORS_PATTERN = re.compile(r"\s*[,，]\s*(?:未知演员|.+)$")

JELLYFIN_CURATION_CONFIRMATION = "CURATE_JELLYFIN_ITEMS"
JELLYFIN_CURATION_MAX_ITEMS = 100


def normalize_code(value: Any) -> str:
    """番号归一化。必须走 movie_code.normalize_movie_code —— 它是唯一事实来源。

    自己另写一套会踩坑：只做「去分隔符+大写」的话 `MP-BD0006` 会变成 `MPBD0006`，
    而库里存的是 `MPBD6`（数字段还要去前导零），于是永远匹配不上。
    """
    return normalize_movie_code(value)


def extract_item_number(item: Dict) -> Optional[str]:
    """从条目的文件路径/文件名/标题里提番号。

    路径优先：整理后的目录名和文件名常被中文标题覆盖，标题里反而残留日文，
    路径更接近番号本身。任一来源命中即返回。
    """
    media_sources = item.get("MediaSources") or []
    first = media_sources[0] if media_sources and isinstance(media_sources[0], dict) else {}
    for candidate in (first.get("Path"), first.get("Name"), item.get("Path"), item.get("Name")):
        number = extract_number(candidate or "")
        if number:
            return number
    return None


def has_kana(value: Any) -> bool:
    return bool(KANA_PATTERN.search(str(value or "")))


def has_cjk(value: Any) -> bool:
    return bool(CJK_PATTERN.search(str(value or "")))


def load_movies_by_codes(codes: List[str]) -> Dict[str, Dict]:
    """按归一化番号批量取 bus 库记录，返回 {归一化番号: 记录}。"""
    wanted = sorted({c for c in (normalize_code(x) for x in codes) if c})
    if not wanted:
        return {}
    result: Dict[str, Dict] = {}
    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        # 分块避免超长 IN 列表
        for i in range(0, len(wanted), 300):
            chunk = [c.upper() for c in wanted[i:i + 300]]
            if db.is_postgres:
                sql = (
                    "SELECT code_norm, id, title, title_cn, original_title, cover, "
                    "release_date FROM movies WHERE UPPER(code_norm) = ANY(%s)"
                )
                cursor.execute(sql, (chunk,))
            else:
                placeholders = ",".join("?" * len(chunk))
                sql = (
                    "SELECT code_norm, id, title, title_cn, original_title, cover, "
                    f"release_date FROM movies WHERE UPPER(code_norm) IN ({placeholders})"
                )
                cursor.execute(sql, chunk)
            for row in cursor.fetchall():
                key = normalize_code(row["code_norm"])
                result[key] = {
                    "id": row["id"],
                    "title": row["title"],
                    "title_cn": row["title_cn"],
                    "original_title": row["original_title"],
                    "cover": row["cover"],
                    "release_date": row["release_date"],
                }
    finally:
        db._release_connection(conn)
    return result


def load_translations_by_titles(titles: List[str]) -> Dict[str, str]:
    """按原始标题批量取译文缓存，返回 {原文: 译文}。"""
    keys = [t for t in {str(x or "").strip() for x in titles} if t]
    if not keys:
        return {}
    out: Dict[str, str] = {}
    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        for i in range(0, len(keys), 300):
            chunk = keys[i:i + 300]
            if db.is_postgres:
                cursor.execute(
                    "SELECT original_text, translated_text FROM translations "
                    "WHERE original_text = ANY(%s)",
                    (chunk,),
                )
            else:
                placeholders = ",".join("?" * len(chunk))
                cursor.execute(
                    f"SELECT original_text, translated_text FROM translations "
                    f"WHERE original_text IN ({placeholders})",
                    chunk,
                )
            for row in cursor.fetchall():
                translated = strip_translation_control_tokens(row["translated_text"])
                if translated and not is_bad_translation_text(translated):
                    out[row["original_text"]] = translated
    finally:
        db._release_connection(conn)
    return out


class JellyfinCurationService:
    """扫描 + 执行 Jellyfin 资源清洗。"""

    def __init__(self, client=None):
        self.client = client or jellyfin_client

    # ---------- 扫描（只读） ----------

    def scan(self, library_name: str = "黄片", only_gaps: bool = True) -> Dict:
        """扫出库内条目的元数据缺口，返回清洗计划。**不写任何东西。**"""
        self.client.reload_config()
        if not self.client.host or not self.client.api_key:
            return {"success": False, "message": "jellyfin_not_configured", "items": []}
        libraries = self.client.get_libraries()
        library_id = next(
            (lib["id"] for lib in libraries if str(lib.get("name") or "").strip() == library_name),
            None,
        )
        if not library_id:
            return {"success": False, "message": f"library_not_found:{library_name}", "items": []}

        raw_items = self.client.list_library_items_raw(library_id=library_id)
        if not raw_items:
            return {"success": False, "message": "no_items_or_degraded", "items": []}

        numbers = [extract_item_number(it) for it in raw_items]
        movies = load_movies_by_codes([n for n in numbers if n])
        originals = [
            movies.get(normalize_code(n), {}).get("original_title")
            for n in numbers if n
        ]
        translations = load_translations_by_titles([o for o in originals if o])

        plans: List[Dict] = []
        stats = {
            "total": len(raw_items),
            "missing_cover": 0,
            "cover_source_available": 0,
            "name_has_kana": 0,
            "translatable": 0,
            "missing_overview": 0,
            "overview_available": 0,
            "missing_provider_ids": 0,
            "unmatched_number": 0,
        }
        for item in raw_items:
            plan = self._plan_item(item, movies, translations)
            if plan is None:
                continue
            if not only_gaps or plan["actions"]:
                plans.append(plan)
            for key in stats:
                if plan["stats"].get(key):
                    stats[key] += 1

        return {
            "success": True,
            "library": library_name,
            "dry_run": True,
            "stats": stats,
            "total": len(plans),
            "items": plans,
        }

    def _plan_item(self, item: Dict, movies: Dict, translations: Dict) -> Optional[Dict]:
        item_id = str(item.get("Id") or "").strip()
        if not item_id:
            return None
        number = extract_item_number(item)
        code = normalize_code(number) if number else ""
        movie = movies.get(code) or {}
        image_tags = item.get("ImageTags") or {}
        current_name = str(item.get("Name") or "")

        actions: List[Dict] = []

        # 1) 封面
        has_cover = bool(image_tags.get("Primary"))
        cover_url = str(movie.get("cover") or "").strip()
        cover_cached = False
        if cover_url:
            try:
                cover_cached = image_cache_service.find_cached_original(cover_url) is not None
            except Exception:
                cover_cached = False
        if not has_cover and cover_url:
            actions.append({
                "action": "upload_cover",
                "cover_url": cover_url,
                "cached": cover_cached,
            })

        # 2) 片名译文
        # 只有「当前名确实不合格」时才改名：已经成中文的条目重写没有收益，
        # 只会因为库内译文与 amane 写入的 NFO 用词不同而把好名字改坏。
        translated = self._pick_translation(movie, translations)
        needs_rename = has_kana(current_name) or not has_cjk(current_name)
        target_name = (
            self._build_target_name(current_name, number, translated) if needs_rename else None
        )
        if target_name and target_name != current_name:
            actions.append({
                "action": "rename",
                "from": current_name,
                "to": target_name,
                "source": "movies.title_cn" if movie.get("title_cn") else "translations",
            })

        # 3) 简介
        overview = str(item.get("Overview") or "").strip()
        if not overview:
            candidate = self._overview_candidate(movie)
            if candidate:
                actions.append({
                    "action": "set_overview",
                    "to": candidate,
                    "source": "movies.detail",
                })

        # 4) ProviderIds
        provider_ids = item.get("ProviderIds") or {}
        if number and not provider_ids:
            actions.append({
                "action": "set_provider_ids",
                "to": {"JavBus": number},
            })

        return {
            "jellyfin_id": item_id,
            "number": number,
            "current_name": current_name,
            "target_name": target_name,
            "has_cover": has_cover,
            "has_overview": bool(overview),
            "provider_ids": provider_ids,
            "matched_in_db": bool(movie),
            "actions": actions,
            "stats": {
                "missing_cover": not has_cover,
                "cover_source_available": bool(cover_url),
                "name_has_kana": has_kana(current_name),
                "translatable": bool(translated),
                "missing_overview": not overview,
                "overview_available": bool(self._overview_candidate(movie)),
                "missing_provider_ids": not provider_ids and bool(number),
                "unmatched_number": bool(number) and not movie,
            },
        }

    @staticmethod
    def _pick_translation(movie: Dict, translations: Dict) -> Optional[str]:
        """译文的取值优先级：movies.title_cn > translations[original_title]。

        质量口径（用户拍板）：作品名意译、艺人姓名保留日文假名，所以演员名不动。

        三道质量闸（都是生产实测踩出来的）：
        1. 不能含假名 —— 否则等于没翻；
        2. 不能过短 —— 「まあや」这种厂牌名直译成「出色地」，写进去比原文更糟；
        3. 原题是纯片假名的（厂牌/系列名，不是作品名）一律不译。
        """
        def usable(text: Any) -> Optional[str]:
            value = strip_translation_control_tokens(text or "").strip()
            if not value or is_bad_translation_text(value):
                return None
            if has_kana(value) or len(value) < MIN_TRANSLATION_LENGTH:
                return None
            return value

        title_cn = usable(movie.get("title_cn"))
        if title_cn:
            return title_cn

        original = str(movie.get("original_title") or "").strip()
        if not original:
            return None
        # 原题本身就是片假名 → 多半是厂牌/系列名，翻译它没有意义
        if KATAKANA_PATTERN.search(original) and not CJK_PATTERN.search(original):
            return None
        return usable(translations.get(original))

    @staticmethod
    def _build_target_name(current_name: str, number: Optional[str], translated: Optional[str]) -> Optional[str]:
        """拼目标片名：`<番号> <中文标题>`，番号缺失时保留原样。

        库内译文的脏尾巴比想象中多（实测 samples）：
          `…樱空桃高清字幕IPZZ-912/2026-09-25`   ← 高清字幕 + 番号 + 日期
          `…女Takakiyo今日新物种STAR-297`          ← 番号在尾巴上，不剥会变成 `STAR-297 …STAR-297`
          `…昨天新物种300MIUM-1306/2026-01-13`    ← 同上 + 日期
        所以要按「剥尾巴 → 剥编号 → 剥日期」的顺序来，且剥完再判空。
        """
        if not translated:
            return None
        base = strip_translation_control_tokens(translated or "").strip()
        if number:
            # 尾巴上的番号（含带不带连字符的变体）先去掉，否则会跟前缀重复
            base = re.sub(re.escape(number), " ", base, flags=re.IGNORECASE)
        base = re.sub(r"\s*(?:高清?字幕|uncensored|无码破解).*$", "", base, flags=re.IGNORECASE)
        # 「今日新物种 / 3天前新種 / 昨天新種」这类抓取站点水印尾巴
        base = re.sub(r"(?:今日|昨天|今日新種|新種|新物种).*$", "", base)
        base = re.sub(r"\d+\s*天前.*$", "", base)
        base = re.sub(r"[/／]\s*\d{4}-\d{2}-\d{2}\s*$", "", base)
        # 兜底：任何残留的「字母+数字」番号形态（可能是别的番号变体）
        base = re.sub(r"\b[A-Z]{2,}\s*-?\s*\d{2,6}\b", " ", base)
        base = re.sub(r"\s+", " ", base).strip(" -/／,，、")
        if len(base) < MIN_TRANSLATION_LENGTH:
            return None
        if number:
            return f"{number} {base}"
        return base

    @staticmethod
    def _overview_candidate(movie: Dict) -> Optional[str]:
        """简介候选：优先用库标题拼一条最简信息，不去联网抓。"""
        title = strip_translation_control_tokens(movie.get("title_cn") or movie.get("title") or "")
        if not title:
            return None
        if has_kana(title):
            return None
        code = str(movie.get("original_title") or "").strip()
        return title

    # ---------- 执行（写入） ----------

    def validate_execute_request(
        self,
        item_ids: Any,
        *,
        confirmation: str,
        enabled: bool,
        dry_run: bool,
        max_items: int = JELLYFIN_CURATION_MAX_ITEMS,
    ) -> Dict:
        ids, error = normalize_item_ids(item_ids)
        if error:
            return {"success": False, "message": error}
        if not dry_run:
            if not enabled:
                return {"success": False, "message": "jellyfin_curation_disabled"}
            if confirmation != JELLYFIN_CURATION_CONFIRMATION:
                return {
                    "success": False,
                    "message": f"missing_confirmation_phrase:{JELLYFIN_CURATION_CONFIRMATION}",
                    "requires_confirmation": True,
                }
            if len(ids) > max_items:
                return {
                    "success": False,
                    "message": f"curation_item_limit_exceeded:{max_items}",
                    "max_items": max_items,
                }
        return {"success": True, "item_ids": ids}

    def execute(self, item_ids: List[str], actions_filter: Optional[List[str]] = None) -> Dict:
        """按清洗计划执行。逐条独立处理，单条失败不影响其余。"""
        self.client.reload_config()
        self.client.load_items_for_ids(item_ids)
        results = []
        for item_id in item_ids:
            item = self.client.item_snapshot(item_id)
            if not item:
                results.append({
                    "jellyfin_id": item_id,
                    "success": False,
                    "message": "item_not_found_or_unreadable",
                })
                continue
            try:
                plan = self._plan_item(item, *self._lookup_for_item(item))
                if not plan:
                    results.append({"jellyfin_id": item_id, "success": False, "message": "no_plan"})
                    continue
                actions = plan["actions"]
                if actions_filter:
                    actions = [a for a in actions if a.get("action") in set(actions_filter)]
                results.append(self._apply_plan(item_id, item, actions))
            except Exception as exc:  # 单条失败不中断整批
                results.append({
                    "jellyfin_id": item_id,
                    "success": False,
                    "message": type(exc).__name__,
                })
        succeeded = sum(1 for r in results if r.get("success"))
        return {
            "success": succeeded == len(results) and bool(results),
            "total": len(results),
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "results": results,
        }

    def _lookup_for_item(self, item: Dict) -> Tuple[Dict, Dict]:
        number = extract_item_number(item)
        code = normalize_code(number) if number else ""
        movies = load_movies_by_codes([code]) if code else {}
        movie = movies.get(code) or {}
        originals = [movie.get("original_title")] if movie.get("original_title") else []
        return movies, load_translations_by_titles(originals)

    def _apply_plan(self, item_id: str, item: Dict, actions: List[Dict]) -> Dict:
        metadata_actions = [a for a in actions if a.get("action") != "upload_cover"]
        cover_actions = [a for a in actions if a.get("action") == "upload_cover"]

        applied: List[str] = []
        errors: List[str] = []

        if metadata_actions:
            updated = dict(item)
            for action in metadata_actions:
                kind = action.get("action")
                if kind == "rename":
                    updated["Name"] = action["to"]
                    updated["SortName"] = action["to"]
                elif kind == "set_overview":
                    updated["Overview"] = action["to"]
                elif kind == "set_provider_ids":
                    merged = dict(item.get("ProviderIds") or {})
                    merged.update(action["to"])
                    updated["ProviderIds"] = merged
                applied.append(kind)
            # ⚠️ 刻意**不**动 LockedFields：它要的是 MetadataFieldType 枚举名，
            # 传不对整个 POST 直接 400（实测 `$.LockedFields[1]` 校验失败），
            # 连带把同请求里的改名/简介/ProviderIds 全部回滚。
            # 字段锁是「锦上添花」，为它冒整批写入失败的风险不值得 —— 真要防覆盖，
            # 应该用 amane 侧 NFO（那才是 Jellyfin 重新刮削时的数据源）。
            updated["LockedFields"] = list(item.get("LockedFields") or [])
            result = self.client.update_item_metadata(item_id, updated)
            if not result.get("success"):
                errors.append(f"metadata:{result.get('message')}")
            # 复核：写坏时能立刻发现（Jellyfin 会把单条 GET 打成 500）
            self.client.forget_items([item_id])
            if self.client.item_snapshot(item_id) is None:
                errors.append("verify_failed_after_write")

        for action in cover_actions:
            cover_bytes = self._load_cover_bytes(action.get("cover_url") or "")
            if not cover_bytes:
                errors.append("cover_source_unavailable")
                continue
            result = self.client.upload_item_image(item_id, cover_bytes)
            if result.get("success"):
                applied.append("upload_cover")
            else:
                errors.append(f"cover:{result.get('message')}")

        return {
            "jellyfin_id": item_id,
            "success": not errors,
            "applied": applied,
            "errors": errors,
        }

    @staticmethod
    def _load_cover_bytes(cover_url: str) -> Optional[bytes]:
        """取封面字节。get_proxy_image 已内含「查本地缓存 → 未命中现抓」全链路。"""
        if not cover_url:
            return None
        try:
            cached = image_cache_service.get_proxy_image(cover_url)
        except Exception:
            return None
        if not cached:
            return None
        path = getattr(cached, "path", None) if not isinstance(cached, (str, bytes)) else cached
        if not path:
            return None
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError:
            return None


def normalize_item_ids(raw: Any) -> Tuple[List[str], Optional[str]]:
    if not isinstance(raw, list):
        return [], "missing_or_invalid_item_ids"
    normalized: List[str] = []
    invalid = 0
    for value in raw:
        item_id = str(value or "").strip()
        if not item_id:
            invalid += 1
            continue
        if item_id not in normalized:
            normalized.append(item_id)
    if invalid:
        return [], "invalid_item_ids"
    if not normalized:
        return [], "missing_or_invalid_item_ids"
    return normalized, None