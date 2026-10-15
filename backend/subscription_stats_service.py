import re
from typing import Any, Callable, Dict, List, Optional

# The subscription cards only render the first 5 covers, so anything beyond
# that is dead weight in a response that is fetched on every page load.
DECORATIVE_COVER_LIMIT = 5


class SubscriptionStatsService:
    def __init__(
        self,
        *,
        db: Any,
        internal_subscription_url_aliases: Callable[[Any], List[str]],
        normalize_subscription_display_name: Callable[[Optional[str], str], str],
        normalize_internal_subscription_url: Callable[[Any], Optional[str]],
        normalize_legacy_internal_subscription_alias: Callable[[Any], Optional[str]],
        safe_number: Callable[..., float],
    ):
        self.db = db
        self.internal_subscription_url_aliases = internal_subscription_url_aliases
        self.normalize_subscription_display_name = normalize_subscription_display_name
        self.normalize_internal_subscription_url = normalize_internal_subscription_url
        self.normalize_legacy_internal_subscription_alias = normalize_legacy_internal_subscription_alias
        self.safe_number = safe_number

    def get_stats_keys(self, source_url: Optional[str]) -> List[str]:
        source = str(source_url or "").strip()
        if not source:
            return []
        alias_urls = self.internal_subscription_url_aliases(source)
        return alias_urls if alias_urls else [source]

    def empty_stats(self) -> Dict[str, Any]:
        return {"count": 0, "tasks": [], "decorative_covers": []}

    def get_actress_cover_terms(self, subscription: Dict[str, Any]) -> Dict[str, List[str]]:
        actress_ids: List[str] = []
        actress_names: List[str] = []

        def add_id(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in actress_ids:
                actress_ids.append(text)

        def add_name(value: Any) -> None:
            text = self.normalize_subscription_display_name(str(value or "")).strip()
            if not text:
                return
            text = re.split(r"[锛?銆怽[]", text, maxsplit=1)[0].strip()
            if text and text not in actress_names:
                actress_names.append(text)

        raw_url = subscription.get("url")
        canonical_url = (
            self.normalize_internal_subscription_url(raw_url)
            or self.normalize_legacy_internal_subscription_alias(raw_url)
        )
        if canonical_url:
            prefix, subject = canonical_url.rsplit(":", 1)
            add_id(canonical_url)
            add_id(subject)
            if prefix == "jellyfin:person":
                add_id(f"jf_person:{subject}")
            elif prefix == "jellyfin:actor":
                add_id(f"jf_actor:{subject}")
                add_name(subject)
            elif prefix == "jellyfin:actress":
                add_name(subject)

        add_name(subscription.get("name"))

        return {"ids": actress_ids, "names": actress_names}

    def get_local_library_covers_for_subscription(
        self,
        subscription: Dict[str, Any],
        limit: int = DECORATIVE_COVER_LIMIT,
    ) -> List[Dict[str, Any]]:
        terms = self.get_actress_cover_terms(subscription)
        covers_by_term = self.get_local_library_covers_for_terms(
            terms["ids"],
            terms["names"],
            limit=limit,
        )
        covers = []
        seen_urls = set()
        for term_type, values in (("id", terms["ids"]), ("name", terms["names"])):
            for value in values:
                for cover in covers_by_term.get((term_type, value), []):
                    cover_url = cover.get("cover")
                    if cover_url and cover_url not in seen_urls:
                        seen_urls.add(cover_url)
                        covers.append(cover)
                        if len(covers) >= limit:
                            return covers
        return covers

    def get_local_library_covers_for_terms(
        self,
        actress_ids: List[str],
        actress_names: List[str],
        limit: int = DECORATIVE_COVER_LIMIT,
    ) -> Dict[Any, List[Dict[str, Any]]]:
        if not actress_ids and not actress_names:
            return {}

        match_queries = []
        params: List[Any] = []
        placeholder = "%s" if self.db.is_postgres else "?"
        if actress_ids:
            placeholders = ", ".join([placeholder] * len(actress_ids))
            match_queries.append(
                "SELECT 'id' AS match_type, ma.actress_id AS match_term, "
                "m.id, m.title, m.cover, "
                "COALESCE(l.added_at, m.scraped_at, 0) AS created_at, "
                "CASE WHEN l.movie_id IS NULL THEN 'movie_library' ELSE 'local_library' END AS cover_source "
                "FROM movie_actresses ma "
                "JOIN movies m ON m.id = ma.movie_id "
                "LEFT JOIN local_files l ON l.movie_id = m.id "
                "WHERE m.cover IS NOT NULL AND m.cover <> '' "
                f"AND ma.actress_id IN ({placeholders})"
            )
            params.extend(actress_ids)
        if actress_names:
            placeholders = ", ".join([placeholder] * len(actress_names))
            match_queries.append(
                "SELECT 'name' AS match_type, ma.actress_name AS match_term, "
                "m.id, m.title, m.cover, "
                "COALESCE(l.added_at, m.scraped_at, 0) AS created_at, "
                "CASE WHEN l.movie_id IS NULL THEN 'movie_library' ELSE 'local_library' END AS cover_source "
                "FROM movie_actresses ma "
                "JOIN movies m ON m.id = ma.movie_id "
                "LEFT JOIN local_files l ON l.movie_id = m.id "
                "WHERE m.cover IS NOT NULL AND m.cover <> '' "
                f"AND ma.actress_name IN ({placeholders})"
            )
            params.extend(actress_names)

        sql = f"""
            WITH matches AS (
                {' UNION ALL '.join(match_queries)}
            ), ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY match_type, match_term
                    ORDER BY cover_source ASC, created_at DESC, id DESC
                ) AS rn
                FROM matches
            )
            SELECT match_type, match_term, id, title, cover, created_at, cover_source
            FROM ranked
            WHERE rn <= {placeholder}
            ORDER BY match_type, match_term, rn
        """
        params.append(limit)

        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()
            covers_by_term: Dict[Any, List[Dict[str, Any]]] = {}
            for row in rows:
                if hasattr(row, "keys"):
                    match_key = (row["match_type"], row["match_term"])
                    cover = {
                        "id": row["id"],
                        "title": row["title"],
                        "cover": row["cover"],
                        "created_at": row["created_at"],
                        "source": row["cover_source"],
                    }
                else:
                    match_key = (row[0], row[1])
                    cover = {
                        "id": row[2],
                        "title": row[3],
                        "cover": row[4],
                        "created_at": row[5],
                        "source": row[6],
                    }
                covers_by_term.setdefault(match_key, []).append(cover)
            return covers_by_term
        finally:
            self.db._release_connection(conn)

    def get_stats(self, include_decorative_covers: bool = False) -> Dict[str, Any]:
        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            stats: Dict[str, Dict[str, Any]] = {}

            cursor.execute(
                "SELECT source_url, COUNT(*) as count "
                "FROM download_queue "
                "WHERE source_url IS NOT NULL "
                "GROUP BY source_url"
            )
            count_rows = cursor.fetchall()
            for row in count_rows:
                if hasattr(row, "keys"):
                    source = row["source_url"]
                    count = row["count"]
                else:
                    source = row[0]
                    count = row[1]
                for stats_key in self.get_stats_keys(source):
                    if stats_key not in stats:
                        stats[stats_key] = self.empty_stats()
                    stats[stats_key]["count"] += int(count or 0)

            task_sql = (
                "SELECT source_url, id, title, status, created_at, cover FROM ("
                "  SELECT q.source_url, q.id, q.title, q.status, q.created_at, m.cover, "
                "         ROW_NUMBER() OVER (PARTITION BY q.source_url ORDER BY q.created_at DESC) as rn "
                "  FROM download_queue q "
                "  LEFT JOIN movies m ON q.movie_id = m.id "
                "  WHERE q.source_url IS NOT NULL"
                ") ranked "
                "WHERE rn <= 15 "
                "ORDER BY source_url, created_at DESC"
            )
            cursor.execute(task_sql)
            task_rows = cursor.fetchall()

            for row in task_rows:
                if hasattr(row, "keys"):
                    source = row["source_url"]
                    task = {
                        "id": row["id"],
                        "title": row["title"],
                        "status": row["status"],
                        "created_at": row["created_at"],
                        "cover": row["cover"],
                    }
                else:
                    source = row[0]
                    task = {
                        "id": row[1],
                        "title": row[2],
                        "status": row[3],
                        "created_at": row[4],
                        "cover": row[5],
                    }
                if source not in stats:
                    stats[source] = self.empty_stats()
                for stats_key in self.get_stats_keys(source):
                    if stats_key not in stats:
                        stats[stats_key] = self.empty_stats()
                    stats[stats_key]["tasks"].append(task)

            for item in stats.values():
                item["tasks"] = sorted(
                    item["tasks"],
                    key=lambda task: self.safe_number(task.get("created_at"), 0),
                    reverse=True,
                )[:15]

        finally:
            self.db._release_connection(conn)

        # ⚠️ 装饰封面（给每张订阅卡补图）要拿 234 个订阅的演员名/ID 去 JOIN
        # movie_actresses(177.9万) × movies(144.8万) 再做窗口排序 —— 实测 15 秒。
        # 前端已改为「展开卡片时才加载封面」，列表概览根本不需要它，
        # 所以默认不算，只有展开详情时才显式要求。
        if include_decorative_covers:
            self._merge_decorative_covers(stats)
        return stats

    def covers_for_subscription(self, source_url: str) -> List[Dict[str, Any]]:
        """只算**单个订阅**的装饰封面，供用户展开卡片时按需取。

        列表接口不再批量算（那要 15 秒），这里一次只处理一个订阅，
        代价从 234 个 JOIN 降到 1 个。
        """
        source = str(source_url or "").strip()
        if not source:
            return []
        subscription = {"url": source, "name": source.rsplit(":", 1)[-1]}
        stats: Dict[str, Dict[str, Any]] = {}
        self._merge_decorative_covers(stats, subscriptions=[subscription])
        covers: List[Dict[str, Any]] = []
        for stats_key in self.get_stats_keys(source):
            for cover in (stats.get(stats_key) or {}).get("decorative_covers") or []:
                if cover not in covers:
                    covers.append(cover)
        return covers[:DECORATIVE_COVER_LIMIT]

    def _merge_decorative_covers(
        self,
        stats: Dict[str, Dict[str, Any]],
        subscriptions: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        subs = subscriptions if subscriptions is not None else self.db.get_subscriptions()
        pending = []
        actress_ids = []
        actress_names = []
        for subscription in subs:
            stats_keys = self.get_stats_keys(subscription.get("url"))
            if not stats_keys:
                continue

            existing_covers = set()
            for stats_key in stats_keys:
                item = stats.setdefault(stats_key, self.empty_stats())
                existing_covers.update(
                    task.get("cover")
                    for task in item.get("tasks", [])
                    if task.get("cover")
                )
                existing_covers.update(
                    cover.get("cover")
                    for cover in item.get("decorative_covers", [])
                    if cover.get("cover")
                )
            if len(existing_covers) >= 5:
                continue

            terms = self.get_actress_cover_terms(subscription)
            for actress_id in terms["ids"]:
                if actress_id not in actress_ids:
                    actress_ids.append(actress_id)
            for actress_name in terms["names"]:
                if actress_name not in actress_names:
                    actress_names.append(actress_name)
            pending.append((stats_keys, existing_covers, terms))

        covers_by_term = self.get_local_library_covers_for_terms(actress_ids, actress_names)
        for stats_keys, existing_covers, terms in pending:
            decorative_covers = []
            seen_urls = set(existing_covers)
            for term_type, values in (("id", terms["ids"]), ("name", terms["names"])):
                for value in values:
                    for cover in covers_by_term.get((term_type, value), []):
                        cover_url = cover.get("cover")
                        if cover_url and cover_url not in seen_urls:
                            seen_urls.add(cover_url)
                            decorative_covers.append(cover)
                            if len(decorative_covers) >= DECORATIVE_COVER_LIMIT:
                                break
                    if len(decorative_covers) >= DECORATIVE_COVER_LIMIT:
                        break
                if len(decorative_covers) >= DECORATIVE_COVER_LIMIT:
                    break
            if not decorative_covers:
                continue

            for stats_key in stats_keys:
                item = stats.setdefault(stats_key, self.empty_stats())
                item["decorative_covers"] = (item.get("decorative_covers") or []) + decorative_covers
                item["decorative_covers"] = item["decorative_covers"][:DECORATIVE_COVER_LIMIT]
