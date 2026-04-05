"""engine/health_flow.py — Health state machine with day-advance logic.

States: active → recovery → confirmation → resolved

Called by scheduler each morning before start_daily_conversation() so the
morning greeting reflects the parent's current health state.

Flow:
  Day 0  parent reports pain/fever     → create health_flow  state=active
  Day 1  morning                       → advance_health_flows() moves to recovery
                                         morning_greeting asks "feeling better?"
  Day 2  parent confirms feeling better → state=confirmation
  Day 3  no new complaints             → state=resolved

If parent reports still unwell on any day:
  → reset day_count, stay in active

Also exports: detect_urgency (re-export from emergency.py for convenience)
"""

import logging
from datetime import datetime

from app.db import get_db
from app.services.whatsapp import send_message  # hoisted — no more late import inside loop

logger = logging.getLogger(__name__)

# State transition map — each day we advance one step if no new complaints
_STATE_PROGRESSION = {
    "active":       "recovery",
    "recovery":     "confirmation",
    "confirmation": "resolved",
}

# Human-readable labels for morning greeting context
STATE_LABELS = {
    "active":       "unwell",
    "recovery":     "recovering",
    "confirmation": "check if fully recovered",
    "resolved":     "recovered",
}


async def advance_health_flows(parent_id: str) -> list[dict]:
    """Advance all active health flows for a parent by one day.

    Called each morning before start_daily_conversation() so that:
      - active flows get context injected into the day's plan
      - stale flows are auto-resolved after 7 days without recovery

    Returns:
        List of currently active/recovery health_flow rows (post-advance).
        Empty if parent is healthy.
    """
    db = get_db()

    try:
        flows = (
            db.table("health_flows")
            .select("*")
            .eq("parent_id", parent_id)
            .neq("state", "resolved")
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error(f"health_flow fetch failed for {parent_id}: {e}")
        return []

    active_flows = []

    for flow in flows:
        flow_id   = flow["id"]
        state     = flow["state"]
        details   = flow.get("details") or {}
        day_count = details.get("day_count", 1)

        # Increment day counter
        day_count += 1
        details["day_count"] = day_count

        # Alert children and auto-resolve after 7 days (safety net)
        if day_count > 7:
            await _send_7day_alert(db, parent_id, flow, details)
            _resolve_flow(db, flow_id, "auto-resolved after 7 days with child alert")
            continue

        # Advance state — if next step would be resolved, resolve cleanly
        next_state = _STATE_PROGRESSION.get(state)
        if next_state == "resolved":
            _resolve_flow(db, flow_id, "progression complete")
            continue

        try:
            db.table("health_flows").update({
                "state":   next_state or state,
                "details": details,
            }).eq("id", flow_id).execute()
            flow["state"]   = next_state or state
            flow["details"] = details
            active_flows.append(flow)
            logger.info(
                f"health_flow {flow_id} advanced: {state} → {next_state} "
                f"(day {day_count}) for parent {parent_id}"
            )
        except Exception as e:
            logger.error(f"health_flow advance failed for {flow_id}: {e}")
            active_flows.append(flow)  # keep so morning greeting still contextualises

    return active_flows


async def _send_7day_alert(db, parent_id: str, flow: dict, details: dict) -> None:
    """Notify all children when a parent has been unwell for 7+ days."""
    try:
        parent_rows = (
            db.table("parents")
            .select("nickname, family_id")
            .eq("id", parent_id)
            .execute()
            .data
        )
        if not parent_rows:
            return

        nickname  = parent_rows[0]["nickname"]
        family_id = parent_rows[0]["family_id"]
        condition = flow.get("condition", "health issue")

        children = (
            db.table("children")
            .select("phone")
            .eq("family_id", family_id)
            .execute()
            .data or []
        )

        for child in children:
            await send_message(
                child["phone"],
                f"⚠️ *AYANA Health Alert — {nickname}*\n\n"
                f"*{nickname}* has been unwell with *{condition}* for over 7 days now.\n\n"
                f"Please consider scheduling a doctor visit or check-up.",
            )

        db.table("alerts").insert({
            "family_id": family_id,
            "parent_id": parent_id,
            "type":      "concern_pattern",
            "message":   f"{nickname} unwell for 7+ days: {condition}",
            "context":   details,
        }).execute()

        logger.warning(f"7-day health alert sent for {parent_id}: {condition}")

    except Exception as alert_err:
        logger.error(f"7-day health alert failed for {parent_id}: {alert_err}")


def open_health_flow(
    parent_id: str,
    condition: str,
    severity: str = "mild",
    location: str = "",
) -> str | None:
    """Create a new health flow or reset an existing one for the same condition.

    Args:
        parent_id: UUID of parent.
        condition: Short description e.g. "fever", "knee_pain".
        severity:  mild | moderate | severe.
        location:  Body part if relevant.

    Returns:
        health_flow UUID or None on error.
    """
    db = get_db()
    details = {"severity": severity, "location": location, "day_count": 1}

    try:
        # Check if an active flow for this condition already exists
        existing = (
            db.table("health_flows")
            .select("id")
            .eq("parent_id", parent_id)
            .eq("condition", condition)
            .neq("state", "resolved")
            .execute()
            .data
        )

        if existing:
            flow_id = existing[0]["id"]
            db.table("health_flows").update({
                "state":      "active",
                "details":    details,
                "started_at": datetime.utcnow().isoformat(),
            }).eq("id", flow_id).execute()
            logger.info(f"health_flow {flow_id} reset to active ({condition})")
            return flow_id

        resp = db.table("health_flows").insert({
            "parent_id": parent_id,
            "condition": condition,
            "state":     "active",
            "details":   details,
        }).execute()
        flow_id = resp.data[0]["id"]
        logger.info(f"health_flow {flow_id} opened ({condition}, {severity})")
        return flow_id

    except Exception as e:
        logger.error(f"open_health_flow failed ({parent_id}/{condition}): {e}")
        return None


def resolve_health_flow(parent_id: str, condition: str) -> None:
    """Mark a health flow as resolved when parent confirms they're better."""
    db = get_db()
    try:
        rows = (
            db.table("health_flows")
            .select("id")
            .eq("parent_id", parent_id)
            .eq("condition", condition)
            .neq("state", "resolved")
            .execute()
            .data
        )
        for row in rows:
            _resolve_flow(db, row["id"], "parent confirmed recovery")
    except Exception as e:
        logger.error(f"resolve_health_flow failed ({parent_id}/{condition}): {e}")


def get_active_flows(parent_id: str) -> list[dict]:
    """Return all non-resolved health flows for a parent."""
    db = get_db()
    try:
        return (
            db.table("health_flows")
            .select("*")
            .eq("parent_id", parent_id)
            .neq("state", "resolved")
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error(f"get_active_flows failed ({parent_id}): {e}")
        return []


# ── Private ───────────────────────────────────────────────────────────────────

def _resolve_flow(db, flow_id: str, reason: str) -> None:
    try:
        db.table("health_flows").update({
            "state":       "resolved",
            "resolved_at": datetime.utcnow().isoformat(),
        }).eq("id", flow_id).execute()
        logger.info(f"health_flow {flow_id} resolved: {reason}")
    except Exception as e:
        logger.error(f"_resolve_flow failed for {flow_id}: {e}")


# ── Re-export for integration prompt compatibility ────────────────────────────
from app.services.emergency import detect_urgency  # noqa: F401, E402