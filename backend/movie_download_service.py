import re
from typing import Any, Callable, Dict, Optional
from urllib.parse import unquote, urlparse


NormalizeUrl = Callable[[Any], Optional[str]]
NormalizeVideoId = Callable[[Any], Optional[str]]


def extract_movie_id_from_download_source(*values: Any) -> Optional[str]:
    for value in values:
        text = unquote(str(value or "")).strip()
        if not text:
            continue

        fc2_match = re.search(r'\b(FC2)[\s_-]*(?:PPV[\s_-]*)?(\d{3,8})\b', text, re.IGNORECASE)
        if fc2_match:
            return f"FC2-PPV-{fc2_match.group(2)}"

        match = re.search(r'\b([A-Z]{2,10})[\s_-]?(\d{2,6})\b', text, re.IGNORECASE)
        if match:
            return f"{match.group(1).upper()}-{match.group(2)}"

    return None


def resolve_movie_detail_url(
    raw_url: Any,
    *,
    normalize_url: NormalizeUrl,
    normalize_video_id: NormalizeVideoId,
    movie_id: Any = None,
    title: Any = None,
) -> Optional[Dict[str, Optional[str]]]:
    value = str(raw_url or "").strip()
    parsed = urlparse(value)
    is_absolute_url = bool(parsed.scheme or parsed.netloc)

    direct_url = normalize_url(value)
    if direct_url and is_absolute_url:
        return {
            "url": direct_url,
            "movie_id": extract_movie_id_from_download_source(movie_id, title, direct_url),
            "source": "javbus",
        }

    extracted_id = extract_movie_id_from_download_source(movie_id, title, value)
    safe_movie_id = normalize_video_id(extracted_id)
    if safe_movie_id:
        detail_url = normalize_url(f"/{safe_movie_id}")
        if detail_url:
            return {
                "url": detail_url,
                "movie_id": safe_movie_id,
                "source": "code_lookup",
            }

    if direct_url:
        return {
            "url": direct_url,
            "movie_id": extract_movie_id_from_download_source(movie_id, title, direct_url),
            "source": "javbus",
        }

    return None
