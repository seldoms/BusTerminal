from typing import Any, Callable, Dict, List, Optional


AUTOMATION_MODES = {"off", "suggest", "auto_approve_low_risk"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class AutomationService:
    def __init__(
        self,
        *,
        db: Any,
        jellyfin_service: Any,
        ensure_followed_actress_subscriptions: Callable[[], Dict[str, int]],
        subscription_visible_for_actress: Callable[..., bool],
        get_actress_subscription_url_candidates: Callable[..., List[str]],
        normalize_magnet_url: Callable[[Any], Optional[str]],
        queue_download_if_absent: Callable[[str, str, str, Optional[str]], Dict[str, Any]],
    ):
        self.db = db
        self.jellyfin_service = jellyfin_service
        self.ensure_followed_actress_subscriptions = ensure_followed_actress_subscriptions
        self.subscription_visible_for_actress = subscription_visible_for_actress
        self.get_actress_subscription_url_candidates = get_actress_subscription_url_candidates
        self.normalize_magnet_url = normalize_magnet_url
        self.queue_download_if_absent = queue_download_if_absent

    def decode_candidate(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        return self.jellyfin_service.decode_automation_candidate(candidate)

    def list_candidates(
        self,
        status: Optional[str] = "candidate",
        candidate_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        rows = self.db.get_automation_candidates(status, candidate_type, limit, offset)
        return [self.decode_candidate(row) for row in rows]

    def approve_candidate(
        self,
        candidate_id: str,
        actor: str = "user",
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        candidate = self.db.get_automation_candidate_by_id(candidate_id)
        if not candidate:
            return {"success": False, "message": "candidate_not_found"}
        current_status = str(candidate.get("status") or "candidate")
        if current_status == "approved":
            return {
                "success": True,
                "duplicate": True,
                "data": self.decode_candidate(candidate),
                "application": {"applied": True, "already_decided": True},
            }
        if current_status != "candidate":
            return {
                "success": False,
                "message": "candidate_state_conflict",
                "status": current_status,
            }

        if candidate.get("candidate_type") == "subscription_download":
            payload = self.decode_candidate(candidate).get("payload") or {}
            movie_id = str(candidate.get("subject_id") or candidate.get("source_movie_id") or "").strip()
            title = str(candidate.get("source_title") or candidate.get("subject_name") or movie_id).strip()
            magnet = self.normalize_magnet_url(payload.get("magnet"))
            source_url = payload.get("source_url")
            if not movie_id or not magnet:
                return {"success": False, "message": "invalid_download_candidate"}
            queue_result = self.queue_download_if_absent(magnet, movie_id, title, source_url)
            application = {
                "applied": True,
                "action": "queue_download",
                **queue_result,
            }
        else:
            application = self.jellyfin_service.apply_approved_automation_candidate(candidate)
        if not application.get("applied"):
            return {
                "success": False,
                "message": application.get("message") or "candidate_apply_failed",
                "application": application,
            }

        updated = self.db.update_automation_candidate_status(
            candidate_id,
            "approved",
            actor or "user",
            reason,
        )
        if candidate.get("candidate_type") == "subscription_download":
            return {
                "success": True,
                "data": self.decode_candidate(updated),
                "application": application,
            }

        repair = self.ensure_followed_actress_subscriptions()
        subscription_visible = self.subscription_visible_for_actress(
            application.get("actress_id"),
            application.get("page_url"),
            application.get("source_url"),
            application.get("subscription_url"),
        )
        subscription_urls = self.get_actress_subscription_url_candidates(
            application.get("actress_id"),
            application.get("page_url"),
            application.get("source_url"),
            application.get("subscription_url"),
        )
        application = {
            **application,
            "subscription_visible": subscription_visible,
            "subscription_urls": application.get("subscription_urls") or subscription_urls,
            "subscription_repair": repair,
        }
        return {
            "success": True,
            "data": self.decode_candidate(updated),
            "application": application,
        }

    def reject_candidate(
        self,
        candidate_id: str,
        actor: str = "user",
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        candidate = self.db.get_automation_candidate_by_id(candidate_id)
        if not candidate:
            return {"success": False, "message": "candidate_not_found"}
        current_status = str(candidate.get("status") or "candidate")
        if current_status == "rejected":
            return {
                "success": True,
                "duplicate": True,
                "data": self.decode_candidate(candidate),
            }
        if current_status != "candidate":
            return {
                "success": False,
                "message": "candidate_state_conflict",
                "status": current_status,
            }

        updated = self.db.update_automation_candidate_status(
            candidate_id,
            "rejected",
            actor or "user",
            reason,
        )
        return {"success": True, "data": self.decode_candidate(updated)}

    def approve_all(
        self,
        actor: str = "user",
        reason: str = "Bulk approve automation candidates",
    ) -> Dict[str, Any]:
        report = self.jellyfin_service.approve_pending_favorite_actor_candidates(
            actor=actor or "user",
            reason=reason or "Bulk approve automation candidates",
        )
        report["subscription_repair"] = self.ensure_followed_actress_subscriptions()
        return report

    def get_settings(self) -> Dict[str, Any]:
        legacy_enabled = _as_bool(self.db.get_config("jellyfin_auto_approve_candidates"), False)
        configured_mode = str(self.db.get_config("automation_mode") or "").strip().lower()
        mode = configured_mode if configured_mode in AUTOMATION_MODES else (
            "auto_approve_low_risk" if legacy_enabled else "suggest"
        )
        legacy_action_default = legacy_enabled and not configured_mode
        auto_follow_enabled = _as_bool(
            self.db.get_config("auto_follow_enabled"),
            legacy_action_default,
        )
        auto_subscribe_enabled = _as_bool(
            self.db.get_config("auto_subscribe_enabled"),
            legacy_action_default,
        )
        return {
            "automation_mode": mode,
            "auto_follow_enabled": auto_follow_enabled,
            "auto_subscribe_enabled": auto_subscribe_enabled,
            "auto_download_enabled": _as_bool(self.db.get_config("auto_download_enabled"), False),
            "auto_cleanup_enabled": _as_bool(self.db.get_config("auto_cleanup_enabled"), False),
            "jellyfin_auto_approve_candidates": (
                mode == "auto_approve_low_risk"
                and (auto_follow_enabled or auto_subscribe_enabled)
            ),
        }

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        current = self.get_settings()
        mode = str(updates.get("automation_mode", current["automation_mode"])).strip().lower()
        if mode not in AUTOMATION_MODES:
            raise ValueError("invalid_automation_mode")

        if "jellyfin_auto_approve_candidates" in updates and "automation_mode" not in updates:
            legacy_enabled = _as_bool(updates["jellyfin_auto_approve_candidates"])
            mode = "auto_approve_low_risk" if legacy_enabled else "suggest"
            updates.setdefault("auto_follow_enabled", legacy_enabled)
            updates.setdefault("auto_subscribe_enabled", legacy_enabled)

        values = {
            "automation_mode": mode,
            "auto_follow_enabled": _as_bool(updates.get("auto_follow_enabled"), current["auto_follow_enabled"]),
            "auto_subscribe_enabled": _as_bool(updates.get("auto_subscribe_enabled"), current["auto_subscribe_enabled"]),
            "auto_download_enabled": _as_bool(updates.get("auto_download_enabled"), current["auto_download_enabled"]),
            "auto_cleanup_enabled": _as_bool(updates.get("auto_cleanup_enabled"), current["auto_cleanup_enabled"]),
        }
        for key, value in values.items():
            self.db.set_config(key, value if key == "automation_mode" else ("true" if value else "false"))

        legacy_enabled = mode == "auto_approve_low_risk" and (
            values["auto_follow_enabled"] or values["auto_subscribe_enabled"]
        )
        self.db.set_config("jellyfin_auto_approve_candidates", "true" if legacy_enabled else "false")
        return self.get_settings()
