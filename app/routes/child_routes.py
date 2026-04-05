"""Child-facing REST routes for AYANA.

Routes
──────
  POST /child/onboard
    Web signup from the landing page. Creates family + child + parent records,
    runs Gemini routine extraction, sets up medicines, and fires the first
    check-in immediately if within the parent's check-in window, otherwise
    schedules it for the next tick.

  POST /child/trigger-checkin
    Dev/admin route to manually fire a check-in for a parent (by parent_id).

All routes return JSON and use the service key — they are called from the
Next.js frontend (onboard.js) or internal tooling, not from WhatsApp.
"""

import collections
import logging
import time
from datetime import date

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from app.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

# ─── Default TTS voice per language ──────────────────────────────────────────
_DEFAULT_VOICE: dict[str, str] = {
    "te": "roopa",   "hi": "meera",    "ta": "pavithra", "kn": "suresh",
    "ml": "aparna",  "bn": "ananya",   "mr": "sumedha",  "gu": "nandita",
    "pa": "suresh",  "en": "anushka",
}

# ─── Rate limiting: max 5 signups per IP per hour ────────────────────────────
# In-memory — fine for Railway single-instance deployment.
_RATE_LIMIT: dict[str, collections.deque] = {}
_RATE_LIMIT_MAX    = 5
_RATE_LIMIT_WINDOW = 3600  # seconds


def _check_rate_limit(ip: str) -> bool:
    """Return True if this IP is within the rate limit, False if exceeded."""
    now = time.time()
    if ip not in _RATE_LIMIT:
        _RATE_LIMIT[ip] = collections.deque()
    window = _RATE_LIMIT[ip]
    # Drop timestamps outside the rolling window
    while window and window[0] < now - _RATE_LIMIT_WINDOW:
        window.popleft()
    if len(window) >= _RATE_LIMIT_MAX:
        return False
    window.append(now)
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# REQUEST / RESPONSE MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class OnboardRequest(BaseModel):
    """Matches the payload posted by onboard.js on the landing page."""
    child_name:      str
    child_phone:     str           # E.164 e.g. +919876543210
    parent_name:     str
    parent_nickname: str
    parent_phone:    str           # E.164
    language:        str = "te"
    checkin_time:    str = "08:00" # HH:MM
    routine:         str = ""      # Natural language description (optional)


class TriggerCheckinRequest(BaseModel):
    parent_id: str


# ═══════════════════════════════════════════════════════════════════════════════
# POST /child/onboard
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/onboard", tags=["child"])
async def onboard(
    payload: OnboardRequest,
    background: BackgroundTasks,
    request: Request,
):
    """Create a new family from the web landing page signup.

    Steps:
      1. Rate-limit check (5 signups per IP per hour).
      2. Normalise phone numbers to E.164.
      3. Upsert family + child + parent records (idempotent — reuse if exists).
         If a parent's phone is already registered under a different family,
         they are reassigned to the current child's family (prevents data leakage).
      4. Run Gemini extraction on the routine description (if provided).
      5. Send welcome message + fire first check-in as background tasks.

    Returns:
      {status, family_id, child_id, parent_id, message}
    """
    # ── Rate limit ─────────────────────────────────────────────────────────────
    ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many signups from this address. Please try again later.",
        )

    db = get_db()

    child_phone  = _normalise_phone(payload.child_phone)
    parent_phone = _normalise_phone(payload.parent_phone)

    # ── Validate phones ────────────────────────────────────────────────────────
    if not child_phone or not parent_phone:
        raise HTTPException(status_code=400, detail="Invalid phone number format")

    if child_phone == parent_phone:
        raise HTTPException(status_code=400, detail="Child and parent phones must be different")

    # ── Upsert child + family ──────────────────────────────────────────────────
    try:
        existing_child = (
            db.table("children")
            .select("id, family_id")
            .eq("phone", child_phone)
            .execute()
            .data
        )

        if existing_child:
            child_id  = existing_child[0]["id"]
            family_id = existing_child[0]["family_id"]
            logger.info(f"Reusing existing child {child_id} / family {family_id}")
        else:
            fam = db.table("families").insert({
                "plan":          "trial",
                "report_format": "combined",
            }).execute().data[0]
            family_id = fam["id"]

            child = db.table("children").insert({
                "family_id":   family_id,
                "phone":       child_phone,
                "name":        payload.child_name.strip(),
                "is_primary":  True,
                "report_time": "20:00",
            }).execute().data[0]
            child_id = child["id"]
            logger.info(f"Created family {family_id} + child {child_id}")

    except Exception as e:
        logger.error(f"Family/child creation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create family record")

    # ── Upsert parent ──────────────────────────────────────────────────────────
    try:
        existing_parent = (
            db.table("parents")
            .select("id, family_id")           # fetch family_id for cross-family check
            .eq("phone", parent_phone)
            .execute()
            .data
        )

        voice = _DEFAULT_VOICE.get(payload.language, "roopa")

        if existing_parent:
            parent_id          = existing_parent[0]["id"]
            existing_family_id = existing_parent[0].get("family_id")

            update_data: dict = {
                "nickname":     payload.parent_nickname.strip(),
                "language":     payload.language,
                "tts_voice":    voice,
                "checkin_time": payload.checkin_time,
                "is_active":    True,
            }

            # If this parent was registered under a different family, reassign them.
            # This prevents cross-family data leakage when a parent re-signs up via
            # a different child's landing page form.
            if existing_family_id and existing_family_id != family_id:
                logger.warning(
                    f"Parent {parent_id} was under family {existing_family_id}, "
                    f"reassigning to {family_id} (child {child_id})"
                )
                update_data["family_id"] = family_id

            db.table("parents").update(update_data).eq("id", parent_id).execute()
            logger.info(f"Reusing existing parent {parent_id}")

        else:
            parent_row = db.table("parents").insert({
                "family_id":    family_id,
                "phone":        parent_phone,
                "name":         payload.parent_name.strip() or payload.parent_nickname.strip(),
                "nickname":     payload.parent_nickname.strip(),
                "language":     payload.language,
                "tts_voice":    voice,
                "checkin_time": payload.checkin_time,
                "is_active":    True,
            }).execute().data[0]
            parent_id = parent_row["id"]
            logger.info(f"Created parent {parent_id}")

    except Exception as e:
        logger.error(f"Parent creation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create parent record")

    # ── Run Gemini extraction on routine description ───────────────────────────
    # Synchronous so the full profile is ready before the first check-in fires.
    if payload.routine.strip():
        try:
            await _extract_and_apply_routine(
                db, parent_id, payload.parent_nickname.strip(), payload.routine.strip()
            )
        except Exception as e:
            # Non-fatal — parent still gets check-ins, just without personalisation
            logger.error(f"Routine extraction failed for parent {parent_id}: {e}", exc_info=True)
    else:
        logger.info(f"No routine description for parent {parent_id} — skipping extraction")

    # ── Send welcome message + fire first check-in ─────────────────────────────
    background.add_task(
        _send_parent_welcome,
        parent_phone,
        payload.parent_nickname.strip(),
        payload.language,
        payload.checkin_time,
    )
    background.add_task(_trigger_first_checkin, parent_id)

    return {
        "status":    "created",
        "family_id": family_id,
        "child_id":  child_id,
        "parent_id": parent_id,
        "message":   (
            f"{payload.parent_nickname} is set up! "
            f"They'll receive their first check-in at {payload.checkin_time} IST."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# POST /child/trigger-checkin  (dev/admin use)
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/trigger-checkin", tags=["child"])
async def trigger_checkin(payload: TriggerCheckinRequest, background: BackgroundTasks):
    """Manually fire a check-in for a parent. Useful for testing and dev."""
    background.add_task(_trigger_first_checkin, payload.parent_id)
    return {"status": "queued", "parent_id": payload.parent_id}


# ═══════════════════════════════════════════════════════════════════════════════
# PRIVATE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _normalise_phone(raw: str) -> str:
    """Convert any phone format to E.164.

    Strips spaces, dashes, parentheses. Adds +91 for bare 10-digit numbers.
    """
    cleaned = ""
    for ch in raw.strip():
        if ch == "+" and not cleaned:
            cleaned += ch
        elif ch.isdigit():
            cleaned += ch

    if not cleaned:
        return ""

    if not cleaned.startswith("+"):
        if len(cleaned) == 10:
            cleaned = "+91" + cleaned
        else:
            cleaned = "+" + cleaned

    return cleaned


async def _extract_and_apply_routine(
    db,
    parent_id: str,
    nickname: str,
    routine_text: str,
) -> None:
    """Run Gemini extraction and update the parent row + create medicines.

    Updates:
      - parents.activities
      - parents.conditions
      - parents.alone_during_day
      - parents.routine  (with all meal times)
      - medicine_groups + medicines rows
    """
    from app.services.gemini import extract_routine
    from app.services.medicine import setup_medicines_from_routine

    logger.info(f"Running Gemini routine extraction for parent {parent_id}")
    routine = await extract_routine(routine_text, nickname)

    meal_times = routine.meal_times if routine.meal_times else {}
    routine_dict = {
        "wake_time":      routine.wake_time or "06:30",
        "breakfast_time": meal_times.get("tiffin") or meal_times.get("breakfast") or "08:30",
        "lunch_time":     meal_times.get("lunch") or "13:00",
        "evening_time":   meal_times.get("tea") or "17:00",
        "dinner_time":    meal_times.get("dinner") or "20:00",
        "sleep_time":     "22:00",
        "notes":          routine.notes,
    }

    try:
        db.table("parents").update({
            "activities":       routine.activities,
            "conditions":       routine.conditions,
            "alone_during_day": routine.alone_during_day,
            "routine":          routine_dict,
        }).eq("id", parent_id).execute()

        logger.info(
            f"Updated parent {parent_id} profile — "
            f"activities={len(routine.activities)}, "
            f"conditions={len(routine.conditions)}, "
            f"medicines={len(routine.medicines)}"
        )
    except Exception as e:
        logger.error(f"Parent profile update failed for {parent_id}: {e}")

    if routine.medicines:
        try:
            count = setup_medicines_from_routine(parent_id, routine)
            logger.info(f"Created {count} medicine group(s) for parent {parent_id}")
        except Exception as e:
            logger.error(f"Medicine setup failed for parent {parent_id}: {e}")


async def _send_parent_welcome(
    phone: str,
    nickname: str,
    language: str,
    checkin_time: str,
) -> None:
    """Send a welcome WhatsApp message to a newly onboarded parent."""
    from app.services.whatsapp import send_message
    from app.services import sarvam

    msg_en = (
        f"Namaste! I'm *AYANA*, your daily care companion 🙏\n\n"
        f"Your family has set me up to check in with you each morning at "
        f"*{checkin_time}* every day.\n\n"
        f"I'll send you a voice message in your language asking how you are. "
        f"Just tap a button to reply — no typing needed.\n\n"
        f"Looking forward to our daily chats, {nickname}!"
    )

    try:
        audio_url, translated = await sarvam.english_to_parent_audio(
            msg_en, language, _DEFAULT_VOICE.get(language, "roopa"), nickname
        )
        from app.services.whatsapp import send_audio_and_buttons
        await send_audio_and_buttons(
            to=phone, audio_url=audio_url or "", text=translated or msg_en
        )
        logger.info(f"Welcome message sent to parent {phone}")
    except Exception as e:
        logger.warning(f"Welcome audio failed for {phone}, trying text: {e}")
        try:
            await send_message(phone, msg_en)
        except Exception as e2:
            logger.error(f"Welcome message completely failed for {phone}: {e2}")


async def _trigger_first_checkin(parent_id: str) -> None:
    """Fire start_daily_conversation for a parent, clearing any stale state first."""
    from app.services.conversation import start_daily_conversation

    db    = get_db()
    today = date.today().isoformat()

    try:
        db.table("conversation_state").delete().eq("parent_id", parent_id).eq("date", today).execute()
        logger.info(f"Cleared existing conversation_state for parent {parent_id}")
    except Exception as e:
        logger.warning(f"conversation_state clear failed (may not exist): {e}")

    try:
        rows = db.table("parents").select("*").eq("id", parent_id).execute().data
        if not rows:
            logger.error(f"_trigger_first_checkin: parent {parent_id} not found")
            return
        parent = rows[0]
    except Exception as e:
        logger.error(f"_trigger_first_checkin: parent fetch failed: {e}")
        return

    if not parent.get("is_active"):
        logger.info(f"_trigger_first_checkin: parent {parent_id} is inactive, skipping")
        return

    try:
        ok = await start_daily_conversation(parent_id)
        if ok:
            logger.info(f"First check-in sent to parent {parent_id} ({parent.get('phone')})")
        else:
            logger.warning(f"start_daily_conversation returned False for parent {parent_id}")
    except Exception as e:
        logger.error(f"_trigger_first_checkin failed for parent {parent_id}: {e}", exc_info=True)