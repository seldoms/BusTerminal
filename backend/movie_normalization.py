import re
from typing import Any, Dict, List, Optional


_PLACEHOLDER_IDS = {"", "UNKNOWN", "NONE", "NULL", "N/A"}
_UPSTREAM_ERROR_PATTERN = re.compile(
    r"(?:error\s*[45]\d{2}|bad gateway|service unavailable|gateway timeout|"
    r"that.?s an error|cloudflare ray id)",
    re.IGNORECASE,
)


def normalize_movie_id(value: Any) -> Optional[str]:
    movie_id = str(value or "").strip().upper()
    return movie_id if movie_id not in _PLACEHOLDER_IDS else None


def _identity(movie: Dict[str, Any]) -> Optional[str]:
    movie_id = normalize_movie_id(
        movie.get("id") or movie.get("movie_id") or movie.get("video_id")
    )
    if movie_id:
        return f"id:{movie_id}"

    detail_url = str(movie.get("detail_url") or "").strip().rstrip("/").lower()
    if detail_url:
        return f"url:{detail_url}"

    title = str(movie.get("original_title") or movie.get("title") or "").strip().lower()
    cover = str(movie.get("cover") or "").strip().lower()
    if title or cover:
        return f"content:{title}|{cover}"
    return None


def is_upstream_error_movie(movie: Dict[str, Any]) -> bool:
    """Reject upstream error documents accidentally parsed as movie cards."""
    text = " ".join(
        str(movie.get(key) or "").strip()
        for key in ("title", "original_title")
    )
    return bool(text and _UPSTREAM_ERROR_PATTERN.search(text))


def normalize_movie_list(items: Any) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []

    normalized: List[Dict[str, Any]] = []
    by_identity: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue

        movie = dict(item)
        if is_upstream_error_movie(movie):
            continue
        movie_id = normalize_movie_id(
            movie.get("id") or movie.get("movie_id") or movie.get("video_id")
        )
        if movie_id:
            movie["id"] = movie_id
        elif "id" in movie:
            movie["id"] = ""

        if "detail_url" in movie:
            movie["detail_url"] = str(movie.get("detail_url") or "").strip()

        identity = _identity(movie)
        if not identity:
            continue

        existing = by_identity.get(identity)
        if existing is None:
            by_identity[identity] = movie
            normalized.append(movie)
            continue

        for key, value in movie.items():
            if not existing.get(key) and value not in (None, "", [], {}):
                existing[key] = value

    return normalized
