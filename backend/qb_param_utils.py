from typing import Any, Iterable, List, Optional
import re


def normalize_qb_root_path(raw_path: Any) -> Optional[str]:
    if raw_path in (None, ""):
        return None

    path = str(raw_path).strip().replace("\\", "/")
    while len(path) > 1 and path.endswith("/") and not re.fullmatch(r"[A-Za-z]:/", path):
        path = path[:-1]

    if (
        not path
        or "\x00" in path
        or path.startswith("//")
        or any(part == ".." for part in path.split("/"))
    ):
        return None
    if path == "/" or re.fullmatch(r"[A-Za-z]:/?", path):
        return None
    if not (path.startswith("/") or re.match(r"^[A-Za-z]:/", path)):
        return None
    return path


def normalize_qb_save_path(raw_path: Any, allowed_roots: Iterable[str]) -> Optional[str]:
    path = normalize_qb_root_path(raw_path)
    if not path:
        return None

    for root in allowed_roots:
        normalized_root = normalize_qb_root_path(root)
        if normalized_root and (path == normalized_root or path.startswith(f"{normalized_root}/")):
            return path
    return None


def normalize_qb_category(raw_category: Any) -> Optional[str]:
    category = str(raw_category or "").strip()
    if not category:
        return None
    if len(category) > 64 or not re.fullmatch(r"[\w .:@+-]+", category):
        return None
    return category


def normalize_qb_hashes(raw_hashes: Any) -> List[str]:
    if not isinstance(raw_hashes, list):
        return []
    normalized = []
    for item in raw_hashes:
        value = str(item or "").strip().lower()
        if value and re.fullmatch(r"[a-f0-9]{32,64}", value) and value not in normalized:
            normalized.append(value)
    return normalized
