from typing import Any, Dict, List, Optional
import re
import uuid


TRANSLATION_BATCH_LIMIT = 500
TRANSLATION_TEXT_MAX_LENGTH = 300


def normalize_item_id(raw_item_id: Any) -> Optional[str]:
    value = str(raw_item_id or "").strip()
    if (
        value
        and len(value) <= 128
        and re.fullmatch(r"[A-Za-z0-9_-]+", value)
    ):
        return value
    return None


def normalize_item_ids(raw_item_ids: Any) -> List[str]:
    if not isinstance(raw_item_ids, list):
        return []
    normalized = []
    for item_id in raw_item_ids:
        value = normalize_item_id(item_id)
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def normalize_task_id(raw_task_id: Any) -> Optional[str]:
    value = str(raw_task_id or "").strip()
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError):
        return None


def normalize_enum_value(raw_value: Any, allowed_values: set, default: str) -> Optional[str]:
    value = str(raw_value or default).strip()
    for allowed in allowed_values:
        if value.lower() == allowed.lower():
            return allowed
    return None


def normalize_magnet_url(raw_url: Any) -> Optional[str]:
    magnet = str(raw_url or "").strip()
    if not magnet or len(magnet) > 8192:
        return None
    lower = magnet.lower()
    if not lower.startswith("magnet:?") or "xt=urn:btih:" not in lower:
        return None
    return magnet


def normalize_translation_texts(raw_texts: Any) -> Dict[str, Any]:
    if not isinstance(raw_texts, list):
        return {"success": False, "message": "texts_must_be_list", "texts": []}
    if len(raw_texts) > TRANSLATION_BATCH_LIMIT:
        return {"success": False, "message": "translation_batch_too_large", "texts": []}

    cleaned: List[str] = []
    seen = set()
    for item in raw_texts:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > TRANSLATION_TEXT_MAX_LENGTH:
            return {"success": False, "message": "translation_text_too_long", "texts": []}
        if text not in seen:
            seen.add(text)
            cleaned.append(text)
    return {"success": True, "texts": cleaned}
