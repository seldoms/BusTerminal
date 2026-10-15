from typing import Any, Optional
import re


INTERNAL_SUBSCRIPTION_PREFIXES = (
    "jellyfin:actress:",
    "jellyfin:actor:",
    "jellyfin:person:",
)


def is_valid_internal_subscription_subject(subject: Any) -> bool:
    value = str(subject or "").strip()
    if not value or len(value) > 256:
        return False
    if any(char in value for char in ("\x00", "\r", "\n", "/", "\\", "?", "#")):
        return False
    return True


def normalize_internal_subscription_url(raw_url: Any) -> Optional[str]:
    value = str(raw_url or "").strip().lower()
    raw_value = str(raw_url or "").strip()
    for prefix in INTERNAL_SUBSCRIPTION_PREFIXES:
        if value.startswith(prefix):
            subject = raw_value[len(prefix):].strip()
            if not is_valid_internal_subscription_subject(subject):
                return None
            return f"{prefix}{subject}"
    return None


def normalize_legacy_internal_subscription_alias(raw_url: Any) -> Optional[str]:
    value = str(raw_url or "").strip()
    lowered = value.lower()
    legacy_prefixes = {
        "jf_person:": "jellyfin:person:",
        "jf_actor:": "jellyfin:actor:",
    }
    for legacy_prefix, canonical_prefix in legacy_prefixes.items():
        if lowered.startswith(legacy_prefix):
            subject = value[len(legacy_prefix):].strip()
            if not is_valid_internal_subscription_subject(subject):
                return None
            return f"{canonical_prefix}{subject}"
    return None


def internal_subscription_url_aliases(raw_url: Any) -> list[str]:
    canonical_url = normalize_internal_subscription_url(raw_url) or normalize_legacy_internal_subscription_alias(raw_url)
    if not canonical_url:
        return []

    urls = [canonical_url]
    if canonical_url.startswith("jellyfin:person:"):
        subject = canonical_url.split(":", 2)[2].strip()
        urls.append(f"jf_person:{subject}")
    elif canonical_url.startswith("jellyfin:actor:"):
        subject = canonical_url.split(":", 2)[2].strip()
        urls.append(f"jf_actor:{subject}")
    return list(dict.fromkeys(urls))


def is_internal_subscription_url(raw_url: Any) -> bool:
    return normalize_internal_subscription_url(raw_url) is not None or normalize_legacy_internal_subscription_alias(raw_url) is not None


def jellyfin_person_subscription_url_from_actress_id(actress_id: Optional[str]) -> Optional[str]:
    value = str(actress_id or "").strip()
    person_url = normalize_internal_subscription_url(value)
    if person_url and person_url.startswith("jellyfin:person:"):
        return person_url
    if value.startswith("jf_person:"):
        person_id = value.split(":", 1)[1].strip()
        if is_valid_internal_subscription_subject(person_id):
            return f"jellyfin:person:{person_id}"
    return None


def jellyfin_actor_subscription_url_from_actress_id(actress_id: Optional[str]) -> Optional[str]:
    value = str(actress_id or "").strip()
    actor_url = normalize_internal_subscription_url(value)
    if actor_url and actor_url.startswith("jellyfin:actor:"):
        return actor_url
    if value.startswith("jf_actor:"):
        actor_id = value.split(":", 1)[1].strip()
        if is_valid_internal_subscription_subject(actor_id):
            return f"jellyfin:actor:{actor_id}"
    return None


def internal_actress_subscription_url(actress_id: Optional[str]) -> Optional[str]:
    value = str(actress_id or "").strip()
    return f"jellyfin:actress:{value}" if is_valid_internal_subscription_subject(value) else None


def internal_actor_subscription_url(actress_id: Optional[str]) -> Optional[str]:
    value = str(actress_id or "").strip()
    return f"jellyfin:actor:{value}" if is_valid_internal_subscription_subject(value) else None


def mojibake_score(text: str) -> int:
    suspicious_chars = "鑴欒剹姘撳繖鑾界尗鑼呯洸鏋氱湁鍐掑附鍨勬嫝闄囨ゼ濞勬悅绡撴紡闄嬭姦鍗㈠簮鐐夋幊鍗よ檹椴侀簱纰岄湶璺"
    score = sum(1 for char in text if char in suspicious_chars)
    score += sum(2 for char in text if "\u0080" <= char <= "\u009f" or char == "\ufffd")
    return score


def repair_mojibake_text(text: str) -> str:
    original = str(text or "")
    if not original:
        return original
    best_text = original
    best_score = mojibake_score(original)
    for encoding in ("latin1", "cp1252"):
        try:
            candidate = original.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        candidate_score = mojibake_score(candidate)
        has_cjk = bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", candidate))
        if has_cjk and candidate_score < best_score:
            best_text = candidate
            best_score = candidate_score
    return best_text


def normalize_subscription_display_name(name: Optional[str], fallback: str = "") -> str:
    text = repair_mojibake_text(str(name or "")).strip()
    text = re.sub(
        r"^(?:subscription|follow|subscribed|followed|订阅|关注|璁㈤槄|鍏虫敞|闂滄敞|[?�]+)\s*[:：]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    for prefix in ("subscription:", "follow:", "subscribed:", "followed:", "璁㈤槄:", "闂滄敞:", "鐠併垽妲?", "???:"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
            break
    return text or fallback
