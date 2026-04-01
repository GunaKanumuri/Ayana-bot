"""AYANA medicine helper — creation, query, and confirmation utilities.

Public API
──────────
  setup_medicines_from_routine(parent_id, routine_extraction)
    Takes a RoutineExtraction (from gemini.extract_routine) and creates the
    corresponding medicine_groups + medicines rows in Supabase.
    Groups medicines by anchor_event so that multiple medicines at the same
    meal time share one medicine_group row.

  get_pending_medicines(parent_id)
    Returns medicine_groups whose touchpoint has NOT been confirmed today.
    Used by the scheduler to decide whether a standalone reminder is needed.

  mark_medicine_taken(parent_id, group_id)
    Updates the check_in row for today's medicine touchpoint to status=replied
    with medicine_taken = {taken: True}.
    Creates the row if it doesn't exist yet (handles out-of-order replies).
"""

import logging
import re
from datetime import date, datetime

from app.db import get_db
from app.models.schemas import RoutineExtraction

logger = logging.getLogger(__name__)

# ─── Timing string → anchor_event ─────────────────────────────────────────────
# Matches values that gemini.extract_routine() puts in medicine.timing
_TIMING_TO_ANCHOR: dict[str, str] = {
    "before_food":     "before_food",
    "before_tea":      "wake",
    "wake":            "wake",
    "after_food":      "after_food",
    "after_breakfast": "after_food",
    "with_food":       "after_food",
    "afternoon":       "afternoon",
    "evening":         "evening",
    "dinner":          "dinner",
    "with_dinner":     "dinner",
    "after_dinner":    "after_dinner",
    "night":           "night",
    "as_needed":       "after_food",  # default time slot; is_as_needed=True
}

# ─── anchor_event → human-readable label ──────────────────────────────────────
_ANCHOR_LABEL: dict[str, str] = {
    "wake":        "Morning (empty stomach)",
    "before_food": "Before food",
    "after_food":  "After meals",
    "afternoon":   "Afternoon",
    "evening":     "Evening",
    "dinner":      "With dinner",
    "after_dinner": "After dinner",
    "night":       "Night medicines",
}

# ─── anchor_event → default time if not provided ──────────────────────────────
_ANCHOR_DEFAULT_TIME: dict[str, str] = {
    "wake":        "06:30",
    "before_food": "07:45",
    "after_food":  "09:00",
    "afternoon":   "13:30",
    "evening":     "17:00",
    "dinner":      "20:00",
    "after_dinner": "21:00",
    "night":       "21:30",
}

# ─── anchor_event → touchpoint_type ───────────────────────────────────────────
_ANCHOR_TO_TP: dict[str, str] = {
    "wake":        "medicine_before_food",
    "before_food": "medicine_before_food",
    "after_food":  "medicine_after_food",
    "afternoon":   "medicine_after_food",
    "evening":     "medicine_after_food",
    "dinner":      "medicine_after_food",
    "after_dinner": "medicine_night",
    "night":       "medicine_night",
}


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC: setup_medicines_from_routine
# ═══════════════════════════════════════════════════════════════════════════════

def setup_medicines_from_routine(
    parent_id: str,
    routine: RoutineExtraction,
) -> int:
    """Create medicine_groups and medicines from a Gemini routine extraction.

    Groups medicines by their anchor_event.  One medicine_group row is
    created per anchor, with all medicines sharing that timing as children.

    Existing medicine_groups for the parent are NOT deleted — call this only
    once at onboarding, or after explicitly clearing old groups.

    Args:
        parent_id: UUID of the parent row.
        routine:   RoutineExtraction returned by gemini.extract_routine().

    Returns:
        Number of medicine_groups created (0 on failure or no medicines).
    """
    if not routine.medicines:
        logger.info("No medicines in routine for parent %s — skipping", parent_id)
        return 0

    db = get_db()
    groups_created = 0

    # ── Group medicines by anchor_event ────────────────────────────────────────
    grouped: dict[str, list[dict]] = {}
    for med in routine.medicines:
        timing = med.get("timing", "after_food")
        anchor = _TIMING_TO_ANCHOR.get(timing, "after_food")
        grouped.setdefault(anchor, []).append(med)

    for sort_order, (anchor, meds) in enumerate(grouped.items()):
        try:
            # Pick time from the first medicine's time_estimate, or use default
            raw_time = meds[0].get("time_estimate") or _ANCHOR_DEFAULT_TIME.get(anchor, "08:00")
            time_window = _normalise_time(str(raw_time))

            label = _ANCHOR_LABEL.get(anchor, anchor.replace("_", " ").title())

            grp_resp = db.table("medicine_groups").insert(
                {
                    "parent_id":  parent_id,
                    "label":      label,
                    "anchor_event": anchor,
                    "time_window": time_window,
                    "sort_order": sort_order,
                }
            ).execute()

            group_id = grp_resp.data[0]["id"]
            logger.info(
                "Created medicine_group '%s' (%s) for parent %s",
                label, anchor, parent_id,
            )

            for med in meds:
                is_as_needed = med.get("timing") == "as_needed"
                db.table("medicines").insert(
                    {
                        "group_id":     group_id,
                        "name":         med.get("name", "medicine"),
                        "display_name": med.get("display_name") or med.get("name", "medicine"),
                        "instructions": med.get("instructions", ""),
                        "is_as_needed": is_as_needed,
                        "trigger_symptom": med.get("trigger_symptom"),
                    }
                ).execute()

            groups_created += 1

        except Exception as e:
            logger.error(
                "Failed to create medicine_group '%s' for parent %s: %s",
                anchor, parent_id, e,
            )

    logger.info(
        "setup_medicines_from_routine: %d group(s) created for parent %s",
        groups_created, parent_id,
    )
    return groups_created


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC: get_pending_medicines
# ═══════════════════════════════════════════════════════════════════════════════

def get_pending_medicines(parent_id: str) -> list[dict]:
    """Return medicine_groups not yet confirmed (replied) in today's check_ins.

    A group is "pending" if there is no check_in row for today whose
    touchpoint matches the group's anchor_event → touchpoint_type AND
    status == 'replied'.

    Args:
        parent_id: UUID of the parent row.

    Returns:
        List of medicine_groups rows (with their medicines nested).
        Empty list if all groups are confirmed or on error.
    """
    db    = get_db()
    today = date.today().isoformat()

    # Load all medicine groups for this parent
    try:
        all_groups = (
            db.table("medicine_groups")
            .select("*, medicines(*)")
            .eq("parent_id", parent_id)
            .order("sort_order")
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error("get_pending_medicines: groups fetch failed for %s: %s", parent_id, e)
        return []

    # Load today's replied medicine check_ins
    try:
        replied_tps = set(
            row["touchpoint"]
            for row in (
                db.table("check_ins")
                .select("touchpoint")
                .eq("parent_id", parent_id)
                .eq("date", today)
                .eq("status", "replied")
                .execute()
                .data or []
            )
        )
    except Exception as e:
        logger.error("get_pending_medicines: check_in fetch failed for %s: %s", parent_id, e)
        replied_tps = set()

    pending = []
    for grp in all_groups:
        anchor   = grp.get("anchor_event", "after_food")
        tp_type  = _ANCHOR_TO_TP.get(anchor, "medicine_after_food")

        # Skip as-needed-only groups
        meds = grp.get("medicines") or []
        if all(m.get("is_as_needed") for m in meds):
            continue

        if tp_type not in replied_tps:
            pending.append(grp)

    return pending


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC: mark_medicine_taken
# ═══════════════════════════════════════════════════════════════════════════════

def mark_medicine_taken(parent_id: str, group_id: str) -> bool:
    """Mark a medicine group as taken in today's check_in record.

    If no check_in row exists yet for this group's touchpoint, one is
    created with status=replied.  If one exists, it is updated.

    Args:
        parent_id: UUID of the parent row.
        group_id:  UUID of the medicine_group row.

    Returns:
        True on success, False on error.
    """
    db    = get_db()
    today = date.today().isoformat()

    # Determine touchpoint_type from the group's anchor_event
    try:
        grp_rows = (
            db.table("medicine_groups")
            .select("anchor_event")
            .eq("id", group_id)
            .execute()
            .data
        )
        if not grp_rows:
            logger.warning("mark_medicine_taken: group %s not found", group_id)
            return False
        anchor  = grp_rows[0]["anchor_event"]
        tp_type = _ANCHOR_TO_TP.get(anchor, "medicine_after_food")
    except Exception as e:
        logger.error("mark_medicine_taken: group fetch failed: %s", e)
        return False

    medicine_taken_payload = {"taken": True, "group_id": group_id, "manual": True}
    now_iso = datetime.utcnow().isoformat()

    try:
        # Check if a check_in already exists for this touchpoint today
        existing = (
            db.table("check_ins")
            .select("id, status")
            .eq("parent_id", parent_id)
            .eq("date", today)
            .eq("touchpoint", tp_type)
            .execute()
            .data
        )

        if existing:
            db.table("check_ins").update(
                {
                    "status":         "replied",
                    "medicine_taken": medicine_taken_payload,
                    "replied_at":     now_iso,
                }
            ).eq("id", existing[0]["id"]).execute()
            logger.info(
                "mark_medicine_taken: updated check_in for %s/%s", parent_id, tp_type
            )
        else:
            db.table("check_ins").insert(
                {
                    "parent_id":      parent_id,
                    "date":           today,
                    "touchpoint":     tp_type,
                    "status":         "replied",
                    "medicine_taken": medicine_taken_payload,
                    "sent_at":        now_iso,
                    "replied_at":     now_iso,
                }
            ).execute()
            logger.info(
                "mark_medicine_taken: created check_in for %s/%s", parent_id, tp_type
            )
        return True

    except Exception as e:
        logger.error(
            "mark_medicine_taken: DB update failed for %s/%s: %s",
            parent_id, group_id, e,
        )
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# PRIVATE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _normalise_time(raw: str) -> str:
    """Convert a time string to HH:MM format.

    Handles common formats: "8:30", "08:30", "8:30:00", "8", "08".

    Args:
        raw: Raw time string from Gemini output.

    Returns:
        "HH:MM" string, falling back to "08:00" if unparseable.
    """
    raw = str(raw).strip()

    # Already HH:MM or HH:MM:SS
    m = re.match(r"^(\d{1,2}):(\d{2})", raw)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return f"{h:02d}:{mn:02d}"

    # Just an hour: "8" or "08"
    m = re.match(r"^(\d{1,2})$", raw)
    if m:
        h = int(m.group(1))
        if 0 <= h <= 23:
            return f"{h:02d}:00"

    logger.warning("_normalise_time: could not parse '%s', using 08:00", raw)
    return "08:00"
