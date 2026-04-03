"""AYANA background scheduler — APScheduler with a 5-minute polling loop.

Architecture
────────────
One BackgroundScheduler runs a single job every 5 minutes:
  _main_loop() → asyncio.run(_async_main_loop())

Inside _async_main_loop() six checks happen every tick:

  1. Morning check-ins
     For every active parent whose checkin_time falls within the current
     5-minute window, advance_health_flows() is called first, then
     start_daily_conversation() if today's conversation_state doesn't exist.

  2. Medicine reminders
     For every medicine_group whose time_window falls in the window,
     send a standalone reminder IF the corresponding check_in for today
     hasn't already been sent.

  3. Nudge (3 h after unanswered morning check-in)

  4. Missed check (6 h after unanswered check-in)

  5. Evening reports (daily + weekly on Sundays)

  6. Letter / note deliveries
     Any pending letters whose deliver_date is today get delivered.

Timezone: Asia/Kolkata (IST, UTC+5:30)
"""

import asyncio
import logging
from datetime import date, datetime, timedelta

from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.db import get_db

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

_daily_reports_sent:  dict[str, str] = {}
_weekly_reports_sent: dict[str, str] = {}

_IST = ZoneInfo(settings.TIMEZONE)
_WINDOW_MINS = 4


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC: start / stop
# ═══════════════════════════════════════════════════════════════════════════════

def start_scheduler() -> None:
    """Start the APScheduler BackgroundScheduler."""
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
    """Gracefully shut down the scheduler."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")


# ═══════════════════════════════════════════════════════════════════════════════
# SYNC ENTRY POINT
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
    """Core polling logic — executed every 5 minutes."""
    now_ist   = datetime.now(_IST)
    hhmm      = now_ist.strftime("%H:%M")
    today     = now_ist.date().isoformat()
    is_sunday = now_ist.weekday() == 6

    logger.debug(f"Scheduler tick — IST {hhmm} ({today})")

    await _check_morning_greetings(hhmm, today)
    await _check_medicine_reminders(hhmm, today)
    await _check_medicine_retries(today)
    await _check_nudges(today)
    await _check_missed_checkins(today)
    await _check_evening_reports(hhmm, today, is_sunday)
    await _check_letter_deliveries(today)


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 1 — MORNING GREETINGS  (+ health flow advance)
# ═══════════════════════════════════════════════════════════════════════════════

async def _check_morning_greetings(hhmm: str, today: str) -> None:
    """Start daily conversation for every parent whose checkin_time is now.

    Before starting the conversation, advances any active health flows by one
    day so the greeting reflects the parent's current recovery state.
    """
    from app.services.conversation import start_daily_conversation
    from app.engine.health_flow import advance_health_flows

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

            # Advance health flows before starting conversation
            # This ensures morning_greeting reflects correct health state
            try:
                await advance_health_flows(parent["id"])
            except Exception as hf_err:
                logger.warning(
                    f"[scheduler] health_flow advance failed for {parent['phone']}: {hf_err}"
                )

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
    """Send standalone medicine reminders for groups whose time_window is now."""
    from app.services.conversation import send_medicine_reminder

    db = get_db()
    try:
        groups = (
            db.table("medicine_groups")
            .select(
                "*, medicines(*), "
                "parents(id, phone, nickname, language, tts_voice, is_active, paused_until, family_id)"
            )
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error(f"[scheduler] medicine_groups fetch failed: {e}")
        return

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
# CHECK 2b — MEDICINE RETRIES (picks up "will take soon" / "remind later")
# ═══════════════════════════════════════════════════════════════════════════════

async def _check_medicine_retries(today: str) -> None:
    """Re-send medicine reminders when parent said 'will take soon'.

    conversation.py writes medicine_retry_at into conversation_state.context.
    This function checks if the retry time has passed and the medicine
    touchpoint still has status=replied with action=medicine_later.
    """
    from app.services.conversation import send_medicine_reminder

    db = get_db()
    now_utc = datetime.utcnow().isoformat()

    try:
        states = (
            db.table("conversation_state")
            .select("id, parent_id, context")
            .eq("date", today)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error(f"[scheduler] medicine retry state fetch failed: {e}")
        return

    for state in states:
        ctx = state.get("context") or {}
        retry_at = ctx.get("medicine_retry_at")
        if not retry_at or retry_at > now_utc:
            continue

        parent_id = state["parent_id"]
        group_id = ctx.get("medicine_retry_group_id")
        retry_count = ctx.get("medicine_retry_count", 0)

        try:
            # Check if medicine was taken since the retry was scheduled
            med_checkins = (
                db.table("check_ins")
                .select("medicine_taken")
                .eq("parent_id", parent_id)
                .eq("date", today)
                .like("touchpoint", "medicine_%")
                .eq("status", "replied")
                .execute()
                .data or []
            )
            already_taken = any(
                isinstance(c.get("medicine_taken"), dict) and c["medicine_taken"].get("taken")
                for c in med_checkins
            )
            if already_taken:
                # Clear retry — medicine was taken
                ctx.pop("medicine_retry_at", None)
                ctx.pop("medicine_retry_count", None)
                ctx.pop("medicine_retry_group_id", None)
                db.table("conversation_state").update({"context": ctx}).eq("id", state["id"]).execute()
                continue

            # Load parent and medicine group for re-send
            parent_rows = (
                db.table("parents")
                .select("id, phone, nickname, language, tts_voice, is_active, family_id")
                .eq("id", parent_id)
                .eq("is_active", True)
                .execute()
                .data
            )
            if not parent_rows:
                continue
            parent = parent_rows[0]

            if group_id:
                grp_rows = (
                    db.table("medicine_groups")
                    .select("*, medicines(*)")
                    .eq("id", group_id)
                    .execute()
                    .data
                )
                if grp_rows:
                    logger.info(
                        f"[scheduler] Medicine retry #{retry_count} for {parent['phone']}"
                    )
                    await send_medicine_reminder(parent, grp_rows[0])

            # Clear the retry (max 2 retries enforced by conversation.py)
            ctx.pop("medicine_retry_at", None)
            db.table("conversation_state").update({"context": ctx}).eq("id", state["id"]).execute()

        except Exception as e:
            logger.error(
                f"[scheduler] Medicine retry failed for parent {parent_id}: {e}",
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
            db.table("check_ins").update({"status": "missed"}).eq(
                "id", ci["id"]
            ).execute()

            if ci["touchpoint"] != "morning_greeting":
                continue

            parent_rows = (
                db.table("parents")
                .select("nickname, family_id")
                .eq("id", parent_id)
                .execute()
                .data
            )
            if not parent_rows:
                continue

            parent    = parent_rows[0]
            family_id = parent["family_id"]
            nickname  = parent["nickname"]

            db.table("alerts").insert(
                {
                    "family_id": family_id,
                    "parent_id": parent_id,
                    "type":      "missed_checkin",
                    "message":   f"{nickname} has not responded to today's check-in",
                    "context":   {"date": today, "touchpoint": ci["touchpoint"]},
                }
            ).execute()

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
    """Send daily (and weekly on Sunday) reports at each child's report_time."""
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

    seen_families: set[str] = set()

    for child in children:
        family_id   = child.get("family_id")
        report_time = str(child.get("report_time", "20:00"))[:5]

        if not family_id or family_id in seen_families:
            continue

        if not _in_window(hhmm, report_time):
            continue

        seen_families.add(family_id)

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
# CHECK 6 — LETTER / NOTE DELIVERIES
# ═══════════════════════════════════════════════════════════════════════════════

async def _check_letter_deliveries(today: str) -> None:
    """Deliver any pending letters/notes whose deliver_date is today.

    Marks each letter as delivered (or failed) after processing so it
    never fires twice.
    """
    from app.services import sarvam, whatsapp

    db = get_db()
    try:
        letters = (
            db.table("letters")
            .select(
                "*, "
                "parents(id, phone, nickname, language, tts_voice), "
                "children(name)"
            )
            .eq("deliver_date", today)
            .eq("status", "pending")
            .execute()
            .data or []
        )
    except Exception as e:
        # Table may not exist yet — fail silently
        if "letters" not in str(e).lower():
            logger.error(f"[scheduler] letters fetch failed: {e}")
        return

    for letter in letters:
        try:
            parent = letter.get("parents")
            if not parent:
                continue

            child_name = (letter.get("children") or {}).get("name", "your child")
            content    = letter["content"]
            msg_en     = f"{child_name} sent this for you:\n\n{content}"

            try:
                audio_url, translated = await sarvam.english_to_parent_audio(
                    msg_en,
                    parent["language"],
                    parent["tts_voice"],
                    parent["nickname"],
                )
                await whatsapp.send_audio_and_buttons(
                    to=parent["phone"],
                    audio_url=audio_url or "",
                    text=translated or msg_en,
                )
            except Exception as send_err:
                logger.warning(
                    f"[scheduler] Letter audio failed, falling back to text: {send_err}"
                )
                await whatsapp.send_message(parent["phone"], msg_en)

            db.table("letters").update(
                {
                    "status":       "delivered",
                    "delivered_at": datetime.utcnow().isoformat(),
                }
            ).eq("id", letter["id"]).execute()

            logger.info(f"[scheduler] Letter delivered to {parent['phone']}")

        except Exception as e:
            logger.error(
                f"[scheduler] Letter delivery failed for {letter.get('id')}: {e}",
                exc_info=True,
            )
            try:
                db.table("letters").update({"status": "failed"}).eq(
                    "id", letter["id"]
                ).execute()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY
# ═══════════════════════════════════════════════════════════════════════════════

def _in_window(current_hhmm: str, target_hhmm: str, window: int = _WINDOW_MINS) -> bool:
    """Return True if current_hhmm is within `window` minutes of target_hhmm.

    Handles midnight wrap-around (e.g. 23:58 vs 00:01 are 3 minutes apart).
    """
    try:
        ch, cm = map(int, current_hhmm[:5].split(":"))
        th, tm = map(int, target_hhmm[:5].split(":"))
        diff = abs((ch * 60 + cm) - (th * 60 + tm))
        diff = min(diff, 1440 - diff)
        return diff <= window
    except Exception:
        return False