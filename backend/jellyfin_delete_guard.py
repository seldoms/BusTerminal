from typing import Any, Dict, List, Optional, Tuple

from api_param_utils import normalize_item_id


JELLYFIN_DELETE_CONFIRMATION = "DELETE_JELLYFIN_ITEMS"
JELLYFIN_DELETE_MAX_ITEMS = 100


def validate_jellyfin_item_ids_payload(raw_item_ids: Any) -> Tuple[List[str], Optional[str]]:
    if not isinstance(raw_item_ids, list):
        return [], "missing_or_invalid_item_ids"

    normalized = []
    invalid_count = 0
    for item_id in raw_item_ids:
        value = normalize_item_id(item_id)
        if not value:
            invalid_count += 1
            continue
        if value not in normalized:
            normalized.append(value)

    if invalid_count:
        return [], "invalid_item_ids"
    if not normalized:
        return [], "missing_or_invalid_item_ids"
    return normalized, None


def validate_jellyfin_delete_request(
    payload: Dict[str, Any],
    item_count: int,
    *,
    delete_enabled: bool,
    confirmation_phrase: str = JELLYFIN_DELETE_CONFIRMATION,
    max_items: int = JELLYFIN_DELETE_MAX_ITEMS,
) -> Dict[str, Any]:
    confirm = str(payload.get("confirm") or payload.get("confirm_delete") or "").strip()
    reason = str(payload.get("reason") or "").strip()

    if not delete_enabled:
        return {
            "success": False,
            "message": "jellyfin_delete_disabled",
            "requires_confirmation": True,
            "confirmation_phrase": confirmation_phrase,
        }
    if confirm != confirmation_phrase:
        return {
            "success": False,
            "message": f"missing_confirmation_phrase:{confirmation_phrase}",
            "requires_confirmation": True,
            "confirmation_phrase": confirmation_phrase,
        }
    if item_count > max_items:
        return {
            "success": False,
            "message": f"delete_item_limit_exceeded:{max_items}",
            "max_items": max_items,
        }
    if len(reason) < 4:
        return {
            "success": False,
            "message": "delete_reason_too_short",
            "requires_reason": True,
        }
    return {"success": True, "reason": reason, "item_count": item_count}
