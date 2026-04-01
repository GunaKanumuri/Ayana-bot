"""AYANA background scheduler — APScheduler with a 5-minute polling loop.

Architecture
────────────
One BackgroundScheduler runs a single job every 5 minutes:
  _main_loop() → asyncio.run(_async_main_loop())

Inside _async_main_loop() five checks happen every tick:

  1. Morning check-ins
     For every active parent whose checkin_time falls within the current
     5-minute window, start_daily_conversation() is called if today's
     conversation_state doesn't already exist.

  2. Medicine reminders
     For every medicine_group whose time_window falls in the window,
     send a standalone reminder IF the corresponding check_in for today
     hasn't already been sent (guards against double-sending when the
     conversation flow already covers it).

  3. Nudge (3 h after unanswered morning check-in)
     If the parent's morning_greeting check_in has status=sent and
     sent_at < now-3h, and nudge_sent is False in conversation_state,
     call send_nudge() and flip nudge_sent=True.

  4. Missed check (6 h after unanswered check-in)
     Any check_in with status=sent and sent_at < now-6h is marked
     status=missed and an alert is sent to the family children.

  5. Evening reports
     For every child whose report_time falls in the window, call
     generate_daily_report(family_id) once per family per day.
     Sunday ticks also call generate_weekly_report().

Timezone
────────
All comparisons use Asia/Kolkata (IST, UTC+5:30).
checkin_time / time_window / report_time are stored as TIME in Supabase
and come back as "HH:MM:SS" strings — we compare only the HH:MM part.

Thread safety
─────────────
APScheduler BackgroundScheduler runs jobs in a thread pool.
max_instances=1 ensures the 5-minute job never overlaps itself.
async code is executed via asyncio.run() — each job call gets its own
event loop, completely separate from FastAPI's event loop.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.db import get_db

logger = logging.getLogger(__name__)

# ── Module-level scheduler instance ──────────────────────────────────────────
_scheduler: BackgroundScheduler | None = None

# ── Per-day deduplication for evening reports ─────────────────────────────────
# { family_id: "YYYY-MM-DD" }  — cleared implicitly (date changes each day)
_daily_reports_sent:  dict[str, str] = {}
_weekly_reports_sent: dict[str, str] = {}

# IST timezone object
_IST = pytz.timezone(settings.TIMEZONE)

# How wide the matching window is (should be >= scheduler interval)
_WINDOW_MINS = 4


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC: start / stop
# ═══════════════════════════════════════════════════════════════════════════════

def start_scheduler() -> None:
    """Start the APScheduler BackgroundScheduler.

    Registers one IntervalTrigger job that runs every 5 minutes.
    Called from FastAPI lifespan on startup.
    """
    global _scheduler

    _scheduler = BackgroundScheduler(
        timezone=_IST,
        job_defaults={"coalesce": True, "max_instances": 1},
    )
    _scheduler.add_job(
        _main_loop,
        trigger=IntervalTrigger(minutes=5, timezone=_IST),
        id="ayana_main_loop",
        name="AYANA 5-minute check",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("APScheduler started — main loop every 5 minutes")


def stop_scheduler() -> None:
    """Gracefully shut down the scheduler. Called from FastAPI lifespan on shutdown."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")


# ═══════════════════════════════════════════════════════════════════════════════
# SYNC ENTRY POINT (APScheduler calls this in a thread)
# ═══════════════════════════════════════════════════════════════════════════════

def _main_loop() -> None:
    """Synchronous wrapper — runs the async main loop in a fresh event loop."""
    try:
        asyncio.run(_async_main_loop())
    except Exception as e:
        logger.error(f"_main_loop crashed: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
# ASYNC MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════════

async def _async_main_loop() -> None:
    """Core polling logic — executed every 5 minutes.

    All five checks run sequentially; individual failures are caught and logged
    so one bad check never prevents the others from running.
    """
    now_ist   = datetime.now(_IST)
    hhmm      = now_ist.strftime("%H:%M")
    today     = now_ist.date().isoformat()
    is_sunday = now_ist.weekday() == 6

    logger.debug(f"Scheduler tick — IST {hhmm} ({today})")

    # Run all checks; each has its own try/except
    await _check_morning_greetings(hhmm, today)
    await _check_medicine_reminders(hhmm, today)
    await _check_nudges(today)
    await _check_missed_checkins(today)
    await _check_evening_reports(hhmm, today, is_sunday)


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 1 — MORNING GREETINGS
# ═══════════════════════════════════════════════════════════════════════════════

async def _check_morning_greetings(hhmm: str, today: str) -> None:
    """Start daily conversation for every parent whose checkin_time is now."""
    from app.services.conversation import start_daily_conversation

    db = get_db()
    try:
        parents = (
            db.table("parents")
            .select("id, phone, nickname, checkin_time, is_active, paused_until")
            .eq("is_active", True)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error(f"[scheduler] parents fetch failed: {e}")
        return

    for parent in parents:
        try:
            # Skip paused parents
            paused = parent.get("paused_until")
            if paused and str(paused) >= today:
                continue

            ct = str(parent.get("checkin_time", "08:00"))[:5]
            if not _in_window(hhmm, ct):
                continue

            # Already started today?
            existing = (
                db.table("conversation_state")
                .select("id")
                .eq("parent_id", parent["id"])
                .eq("date", today)
                .execute()
                .data
            )
            if existing:
                continue

            logger.info(
                f"[scheduler] Starting conversation for {parent['nickname']} "
                f"({parent['phone']}) at {hhmm} IST"
            )
            await start_daily_conversation(parent["id"])

        except Exception as e:
            logger.error(
                f"[scheduler] Morning greeting failed for {parent.get('phone')}: {e}",
                exc_info=True,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 2 — MEDICINE REMINDERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _check_medicine_reminders(hhmm: str, today: str) -> None:
    """Send standalone medicine reminders for groups whose time_window is now.

    Skips if the corresponding check_in touchpoint was already sent today
    (which means the Gemini-planned conversation already covered it).
    """
    from app.services.conversation import send_medicine_reminder

    db = get_db()
    try:
        groups = (
            db.table("medicine_groups")
            .select("*, medicines(*), parents(id, phone, nickname, language, tts_voice, is_active, paused_until, family_id)")
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error(f"[scheduler] medicine_groups fetch failed: {e}")
        return

    # Map anchor_event → touchpoint_type (mirrors conversation.py)
    anchor_to_tp = {
        "wake":        "medicine_before_food",
        "before_food": "medicine_before_food",
        "after_food":  "medicine_after_food",
        "afternoon":   "medicine_after_food",
        "evening":     "medicine_after_food",
        "dinner":      "medicine_after_food",
        "after_dinner":"medicine_night",
        "night":       "medicine_night",
    }

    for grp in groups:
        try:
            parent = grp.get("parents")
            if not parent or not parent.get("is_active"):
                continue

            paused = parent.get("paused_until")
            if paused and str(paused) >= today:
                continue

            tw = str(grp.get("time_window", ""))[:5]
            if not tw or not _in_window(hhmm, tw):
                continue

            anchor  = grp.get("anchor_event", "after_food")
            tp_type = anchor_to_tp.get(anchor, "medicine_after_food")

            # Already sent today via conversation flow?
            existing = (
                db.table("check_ins")
                .select("id")
                .eq("parent_id", parent["id"])
                .eq("date", today)
                .eq("touchpoint", tp_type)
                .execute()
                .data
            )
            if existing:
                logger.debug(
                    f"[scheduler] Medicine reminder for {parent['phone']}/{tp_type} "
                    "already handled by conversation flow — skipping"
                )
                continue

            logger.info(
                f"[scheduler] Medicine reminder → {parent['nickname']} "
                f"({parent['phone']}) anchor={anchor}"
            )
            await send_medicine_reminder(parent, grp)

        except Exception as e:
            logger.error(
                f"[scheduler] Medicine reminder failed for group {grp.get('id')}: {e}",
                exc_info=True,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 3 — NUDGE (3 h after unanswered morning check-in)
# ═══════════════════════════════════════════════════════════════════════════════

async def _check_nudges(today: str) -> None:
    """Send a single follow-up nudge 3 hours after an unanswered morning greeting."""
    from app.services.conversation import send_nudge

    db = get_db()
    cutoff = (datetime.utcnow() - timedelta(hours=3)).isoformat()

    try:
        # Find morning_greeting check-ins that are still 'sent' and older than 3 h
        stale = (
            db.table("check_ins")
            .select("parent_id, date, sent_at")
            .eq("date", today)
            .eq("touchpoint", "morning_greeting")
            .eq("status", "sent")
            .lte("sent_at", cutoff)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error(f"[scheduler] nudge fetch failed: {e}")
        return

    for ci in stale:
        parent_id = ci["parent_id"]
        try:
            # Check nudge_sent flag
            state = (
                db.table("conversation_state")
                .select("id, nudge_sent")
                .eq("parent_id", parent_id)
                .eq("date", today)
                .execute()
                .data
            )
            if not state or state[0].get("nudge_sent"):
                continue

            # Load parent
            parent_rows = (
                db.table("parents")
                .select("id, phone, nickname, language, tts_voice, family_id")
                .eq("id", parent_id)
                .eq("is_active", True)
                .execute()
                .data
            )
            if not parent_rows:
                continue

            parent = parent_rows[0]
            logger.info(f"[scheduler] Sending nudge to {parent['phone']}")
            await send_nudge(parent)

            # Mark nudge as sent
            db.table("conversation_state").update({"nudge_sent": True}).eq(
                "id", state[0]["id"]
            ).execute()

        except Exception as e:
            logger.error(
                f"[scheduler] Nudge failed for parent {parent_id}: {e}", exc_info=True
            )


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 4 — MISSED CHECK-INS (6 h after unanswered)
# ═══════════════════════════════════════════════════════════════════════════════

async def _check_missed_checkins(today: str) -> None:
    """Mark check-ins as 'missed' and alert family 6 hours after no reply."""
    from app.services.whatsapp import send_message

    db = get_db()
    cutoff = (datetime.utcnow() - timedelta(hours=6)).isoformat()

    try:
        stale = (
            db.table("check_ins")
            .select("id, parent_id, touchpoint, date")
            .eq("date", today)
            .eq("status", "sent")
            .lte("sent_at", cutoff)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error(f"[scheduler] missed check-ins fetch failed: {e}")
        return

    for ci in stale:
        parent_id = ci["parent_id"]
        try:
            # Mark missed
            db.table("check_ins").update({"status": "missed"}).eq(
                "id", ci["id"]
            ).execute()

            # Only alert once per day per parent (on the morning_greeting)
            if ci["touchpoint"] != "morning_greeting":
                continue

            # Load parent + family children
            parent_rows = (
                db.table("parents")
                .select("nickname, family_id")
                .eq("id", parent_id)
                .execute()
                .data
            )
            if not parent_rows:
                continue
            parent     = parent_rows[0]
            family_id  = parent["family_id"]
            nickname   = parent["nickname"]

            # Create missed_checkin alert
            db.table("alerts").insert(
                {
                    "family_id": family_id,
                    "parent_id": parent_id,
                    "type":      "missed_checkin",
                    "message":   f"{nickname} has not responded to today's check-in",
                    "context":   {"date": today, "touchpoint": ci["touchpoint"]},
                }
            ).execute()

            # Notify children
            children = (
                db.table("children")
                .select("phone, name")
                .eq("family_id", family_id)
                .execute()
                .data or []
            )
            for child in children:
                await send_message(
                    child["phone"],
                    f"⚠️ *AYANA — Missed check-in*\n\n"
                    f"*{nickname}* has not responded to today's check-in "
                    f"(sent 6+ hours ago).\n\n"
                    f"Please give them a call to make sure they're okay.",
                )
            logger.warning(
                f"[scheduler] Missed check-in alert sent for {nickname} "
                f"(parent {parent_id})"
            )

        except Exception as e:
            logger.error(
                f"[scheduler] Missed check-in handling failed for {parent_id}: {e}",
                exc_info=True,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 5 — EVENING REPORTS
# ═══════════════════════════════════════════════════════════════════════════════

async def _check_evening_reports(hhmm: str, today: str, is_sunday: bool) -> None:
    """Send daily (and weekly on Sunday) reports at each child's report_time.

    Uses _daily_reports_sent / _weekly_reports_sent dicts for per-day
    deduplication so the same family never gets two reports on the same day.
    """
    from app.services.report import generate_daily_report, generate_weekly_report

    db = get_db()
    try:
        children = (
            db.table("children")
            .select("family_id, report_time")
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error(f"[scheduler] children fetch for reports failed: {e}")
        return

    # Deduplicate by family (multiple children in same family share same report)
    seen_families: set[str] = set()

    for child in children:
        family_id   = child.get("family_id")
        report_time = str(child.get("report_time", "20:00"))[:5]

        if not family_id or family_id in seen_families:
            continue

        if not _in_window(hhmm, report_time):
            continue

        seen_families.add(family_id)

        # ── Daily report ──────────────────────────────────────
        if _daily_reports_sent.get(family_id) != today:
            try:
                logger.info(f"[scheduler] Generating daily report for family {family_id}")
                await generate_daily_report(family_id)
                _daily_reports_sent[family_id] = today
            except Exception as e:
                logger.error(
                    f"[scheduler] Daily report failed for family {family_id}: {e}",
                    exc_info=True,
                )

        # ── Weekly report (Sundays only) ──────────────────────
        if is_sunday and _weekly_reports_sent.get(family_id) != today:
            try:
                logger.info(f"[scheduler] Generating weekly report for family {family_id}")
                await generate_weekly_report(family_id)
                _weekly_reports_sent[family_id] = today
            except Exception as e:
                logger.error(
                    f"[scheduler] Weekly report failed for family {family_id}: {e}",
                    exc_info=True,
                )


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY
# ═══════════════════════════════════════════════════════════════════════════════

def _in_window(current_hhmm: str, target_hhmm: str, window: int = _WINDOW_MINS) -> bool:
    """Return True if current_hhmm is within `window` minutes of target_hhmm.

    Handles midnight wrap-around (e.g. 23:58 vs 00:01 are 3 minutes apart).

    Args:
        current_hhmm: Current time as "HH:MM".
        target_hhmm:  Scheduled time as "HH:MM" (leading zeros optional).
        window:       Tolerance in minutes (default 4).

    Returns:
        True if within window, False otherwise.
    """
    try:
        ch, cm = map(int, current_hhmm[:5].split(":"))
        th, tm = map(int, target_hhmm[:5].split(":"))
        diff = abs((ch * 60 + cm) - (th * 60 + tm))
        # Wrap around midnight
        diff = min(diff, 1440 - diff)
        return diff <= window
    except Exception:
        return False
