"""AYANA emergency system — urgency detection, escalation, silence alerts.

Three trigger types:
  1. Parent-triggered  — parent taps 🆘 button
  2. AI-detected       — fast keyword check OR Gemini urgency flag in voice note
  3. Silence-triggered — no reply all day (amber warning, NOT escalation)

Escalation sequence (emergency only):
  Step 0  immediate    → WhatsApp alert to child with action buttons
  Step 1  +5 seconds   → Twilio auto-call to primary child
  Step 2  +60 seconds  → Retry call if unanswered
  Step 3  +2 minutes   → WhatsApp to backup contact
  Step 4  +3 minutes   → WhatsApp to parent with local ambulance number

Silence alert is separate — amber tone, no call, just WhatsApp with buttons.
"""

import asyncio
import logging
from datetime import datetime

from app.config import settings
from app.db import get_db
from app.models.schemas import HealthExtraction

logger = logging.getLogger(__name__)

# ── Timing constants ──────────────────────────────────────────────────────────
_STEP1_DELAY_S  = 5    # seconds after WA alert → first call
_STEP2_DELAY_S  = 60   # seconds after first call → retry call
_STEP3_DELAY_S  = 120  # seconds after retry → backup contact WA
_STEP4_DELAY_S  = 180  # seconds after backup → ambulance message to parent

# ── TwiML template ────────────────────────────────────────────────────────────
_TWIML_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Aditi" language="hi-IN">
    This is AYANA. {nickname} may need immediate help. Please call them right away.
  </Say>
  <Pause length="2"/>
  <Say voice="Polly.Aditi" language="hi-IN">
    Repeating: {nickname} may need immediate help. Please call them right away.
  </Say>
</Response>"""

# ── Critical keywords — English only (conversation.py translates before calling) ──
_CRITICAL_KEYWORDS: set[str] = {
    "chest pain", "heart pain", "can't breathe", "cannot breathe",
    "fell down", "i fell", "i've fallen", "fallen down",
    "bleeding", "blood coming", "unconscious", "fainted",
    "very bad pain", "unbearable pain", "severe pain",
    "stroke", "paralysis", "can't move", "cannot move",
    "ambulance", "hospital now", "emergency",
}

# ── City → ambulance number ───────────────────────────────────────────────────
AMBULANCE_NUMBERS: dict[str, str] = {
    "hyderabad": "108", "vijayawada": "108", "vizag": "108",
    "visakhapatnam": "108", "tirupati": "108", "warangal": "108",
    "bangalore": "108", "bengaluru": "108", "mysore": "108",
    "chennai": "108", "coimbatore": "108", "madurai": "108",
    "mumbai": "108", "pune": "108", "nagpur": "108",
    "delhi": "112", "new delhi": "112", "gurgaon": "112", "noida": "112",
    "kolkata": "108", "ahmedabad": "108", "surat": "108",
    "jaipur": "108", "lucknow": "108", "patna": "108",
    "default": "108",
}


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC: DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def fast_keyword_check(english_text: str) -> bool:
    """Return True if english_text contains a critical emergency keyword.

    Called BEFORE Gemini to give immediate response without waiting for AI.
    Only runs on English text — caller must translate first.
    """
    text_lower = english_text.lower()
    return any(kw in text_lower for kw in _CRITICAL_KEYWORDS)


def detect_urgency(health_extraction: HealthExtraction) -> bool:
    """Return True if health extraction signals an urgent situation."""
    return health_extraction.urgency_flag or health_extraction.severity == "severe"


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC: EMERGENCY TRIGGER
# ═══════════════════════════════════════════════════════════════════════════════

async def trigger_emergency(parent_id: str, context: dict) -> None:
    """Initiate the full 5-step emergency escalation for a parent.

    Args:
        parent_id: UUID of the parent row.
        context:   {raw_summary, severity, trigger_type, ...}
    """
    db = get_db()
    parent = _load_parent(db, parent_id)
    if not parent:
        logger.error("trigger_emergency: parent %s not found", parent_id)
        return

    family_id    = parent.get("family_id")
    nickname     = parent.get("nickname", "your parent")
    language     = parent.get("language", "te")
    voice        = parent.get("tts_voice", "roopa")
    parent_phone = parent.get("phone", "")
    city         = (parent.get("routine") or {}).get("city", "")

    children    = _load_children(db, family_id)
    primary     = next((c for c in children if c.get("is_primary")), None) or (children[0] if children else None)
    raw_summary = context.get("raw_summary", "distress signal")

    alert_id = _persist_alert(db, family_id, parent_id, nickname, raw_summary, context)

    # ── Step 0: immediate ─────────────────────────────────────────────────────
    await asyncio.gather(
        _confirm_with_parent(parent_phone, nickname, language, voice),
        _alert_children_whatsapp(children, nickname, raw_summary, context, alert_id),
    )

    # ── Step 1 (+5s): Auto-call primary child ─────────────────────────────────
    if primary and settings.TWILIO_VOICE_PHONE:
        await asyncio.sleep(_STEP1_DELAY_S)
        if await _is_acknowledged(db, alert_id):
            return
        call_ok = await _make_voice_call(primary["phone"], nickname)
        logger.info("Step 1 call to %s: %s", primary["phone"], "sent" if call_ok else "failed")

        # ── Step 2 (+60s): Retry call ─────────────────────────────────────────
        await asyncio.sleep(_STEP2_DELAY_S)
        if await _is_acknowledged(db, alert_id):
            return
        await _make_voice_call(primary["phone"], nickname)
        logger.info("Step 2 retry call to %s", primary["phone"])

    # ── Step 3 (+2min): WhatsApp backup contact ───────────────────────────────
    await asyncio.sleep(_STEP3_DELAY_S - _STEP2_DELAY_S - _STEP1_DELAY_S)
    if await _is_acknowledged(db, alert_id):
        return

    backup = _load_backup_contact(db, family_id)
    if backup:
        child_name = primary["name"] if primary else "their child"
        await _alert_backup_contact(backup, nickname, child_name)
        logger.info("Step 3 backup contact alerted: %s", backup)
    else:
        logger.warning("No backup contact for family %s", family_id)

    # ── Step 4 (+3min): Ambulance number to parent ────────────────────────────
    await asyncio.sleep(_STEP4_DELAY_S - _STEP3_DELAY_S)
    if await _is_acknowledged(db, alert_id):
        return

    ambulance = AMBULANCE_NUMBERS.get(city.lower(), AMBULANCE_NUMBERS["default"])
    try:
        from app.services import sarvam, whatsapp
        msg_en = (
            f"{nickname}, if you need immediate help, please call *{ambulance}* (ambulance). "
            f"Your family is on the way."
        )
        audio_url, translated = await sarvam.english_to_parent_audio(
            msg_en, language, voice, nickname
        )
        await whatsapp.send_audio_and_buttons(
            to=parent_phone, audio_url=audio_url or "", text=translated or msg_en
        )
        logger.info("Step 4 ambulance number sent to parent %s", parent_phone)
    except Exception as e:
        logger.error("Step 4 ambulance message failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC: SILENCE ALERT (amber warning — NOT emergency)
# ═══════════════════════════════════════════════════════════════════════════════

async def send_silence_flag(family_id: str, parent_id: str) -> None:
    """Send an amber warning to children when parent hasn't checked in.

    This is NOT an emergency. No auto-call. No escalation.
    Just a warm WhatsApp with buttons: 📞 I'll call / ✅ They're fine.

    Uses gender-neutral language — "They're fine" not "She's fine".

    Args:
        family_id: UUID of the family.
        parent_id: UUID of the parent who hasn't replied.
    """
    from app.services.whatsapp import send_message

    db = get_db()

    try:
        parent_rows = (
            db.table("parents")
            .select("nickname, phone")
            .eq("id", parent_id)
            .execute()
            .data
        )
        if not parent_rows:
            return
        nickname = parent_rows[0]["nickname"]

        children = _load_children(db, family_id)
        if not children:
            return

        # Log as a non-emergency alert
        db.table("alerts").insert({
            "family_id": family_id,
            "parent_id": parent_id,
            "type":      "missed_checkin",
            "message":   f"{nickname} hasn't checked in today",
            "context":   {"trigger": "silence"},
        }).execute()

        message = (
            f"🔔 *AYANA — {nickname} hasn't checked in today*\n\n"
            f"*{nickname}* saw today's morning message but hasn't responded. "
            f"This could just mean they're busy.\n\n"
            f"_You may want to give them a quick call._\n\n"
            f"Reply *1* — I'll call them now\n"
            f"Reply *2* — They're fine, I know"    # ← gender-neutral
        )

        for child in children:
            await send_message(child["phone"], message)

        logger.info("Silence flag sent to family %s for parent %s", family_id, parent_id)

    except Exception as e:
        logger.error("send_silence_flag failed for parent %s: %s", parent_id, e)


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC: ACKNOWLEDGE (child taps button on emergency alert)
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_child_emergency_reply(child_phone: str, action: str) -> None:
    """Handle a child's button reply to an emergency alert.

    action: "calling_now" | "resolved"
    """
    from app.services.whatsapp import send_message

    db = get_db()

    try:
        child_rows = (
            db.table("children")
            .select("family_id, name")
            .eq("phone", child_phone)
            .execute()
            .data
        )
        if not child_rows:
            return

        family_id  = child_rows[0]["family_id"]
        child_name = child_rows[0]["name"]

        alerts = (
            db.table("alerts")
            .select("id, parent_id")
            .eq("family_id", family_id)
            .eq("acknowledged", False)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        if not alerts:
            await send_message(child_phone, "No active alerts to acknowledge.")
            return

        alert = alerts[0]

        if action == "resolved":
            db.table("alerts").update({
                "acknowledged":    True,
                "acknowledged_by": child_phone,
            }).eq("id", alert["id"]).execute()
            await send_message(child_phone, "✅ Alert marked as resolved. Thank you for checking in.")
            logger.info("Alert %s acknowledged as resolved by %s", alert["id"], child_phone)

        elif action == "calling_now":
            logger.info("Alert %s: %s (%s) said they're calling", alert["id"], child_name, child_phone)
            await send_message(
                child_phone,
                "Got it — please call them now. "
                "Reply *2* (resolved) once you've reached them.",
            )

    except Exception as e:
        logger.error("handle_child_emergency_reply failed for %s: %s", child_phone, e)


# ═══════════════════════════════════════════════════════════════════════════════
# PRIVATE: ESCALATION STEPS
# ═══════════════════════════════════════════════════════════════════════════════

async def _confirm_with_parent(phone: str, nickname: str, language: str, voice: str) -> None:
    from app.services import sarvam, whatsapp
    msg_en = (
        f"{nickname}, I am alerting your family right now. "
        f"Please stay calm. Help is on its way. "
        f"Don't move around — your family will contact you very soon."
    )
    try:
        audio_url, translated = await sarvam.english_to_parent_audio(msg_en, language, voice, nickname)
        await whatsapp.send_audio_and_buttons(to=phone, audio_url=audio_url or "", text=translated or msg_en)
        logger.info("Emergency confirmation sent to parent %s", phone)
    except Exception as e:
        logger.error("Emergency parent confirmation failed for %s: %s", phone, e)


async def _alert_children_whatsapp(
    children: list[dict],
    nickname: str,
    raw_summary: str,
    context: dict,
    alert_id: str | None = None,
) -> None:
    from app.services.whatsapp import send_message
    severity = context.get("severity", "severe")
    concerns = ", ".join(context.get("concerns", [])) or raw_summary
    message = (
        f"🚨 *AYANA EMERGENCY — {nickname}*\n\n"
        f"*{nickname}* may need immediate help.\n\n"
        f"*What they reported:* _{concerns}_\n"
        f"*Severity:* {severity.upper()}\n\n"
        f"Please call *{nickname}* right away or go to them immediately.\n\n"
        f"_AYANA is also calling you now._\n\n"
        f"Reply *1* — I'm calling them now\n"
        f"Reply *2* — Resolved, I've reached them"
    )
    tasks   = [send_message(child["phone"], message) for child in children]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for child, result in zip(children, results):
        if isinstance(result, Exception):
            logger.error("Emergency WA alert failed to %s: %s", child["phone"], result)
        else:
            logger.info("Emergency WA alert sent to %s", child["phone"])


async def _alert_backup_contact(backup_phone: str, parent_nickname: str, child_name: str) -> None:
    from app.services.whatsapp import send_message
    message = (
        f"This is *AYANA*, a care companion service.\n\n"
        f"*{parent_nickname}* pressed the emergency button. "
        f"Their family member *{child_name}* has been notified but hasn't responded yet.\n\n"
        f"*Please check on {parent_nickname} immediately.*\n\n"
        f"If they need an ambulance, call *108*."
    )
    try:
        await send_message(backup_phone, message)
    except Exception as e:
        logger.error("Backup contact alert failed to %s: %s", backup_phone, e)


async def _make_voice_call(to_phone: str, parent_nickname: str) -> bool:
    if not settings.TWILIO_VOICE_PHONE:
        logger.warning("TWILIO_VOICE_PHONE not set — skipping voice call to %s", to_phone)
        return False
    try:
        from twilio.rest import Client as TwilioClient
        client = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        twiml  = _TWIML_TEMPLATE.format(nickname=parent_nickname)
        call   = client.calls.create(to=to_phone, from_=settings.TWILIO_VOICE_PHONE, twiml=twiml)
        logger.info("Voice call initiated to %s — SID %s", to_phone, call.sid)
        return True
    except Exception as e:
        logger.error("Voice call to %s failed: %s", to_phone, e)
        return False


async def _is_acknowledged(db, alert_id: str | None) -> bool:
    if not alert_id:
        return False
    try:
        rows = db.table("alerts").select("acknowledged").eq("id", alert_id).execute().data
        return bool(rows and rows[0].get("acknowledged"))
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# PRIVATE: DB HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _persist_alert(db, family_id: str | None, parent_id: str, nickname: str, raw_summary: str, context: dict) -> str | None:
    if not family_id:
        return None
    try:
        resp = db.table("alerts").insert({
            "family_id":      family_id,
            "parent_id":      parent_id,
            "type":           "emergency",
            "message":        f"Emergency triggered for {nickname}: {raw_summary[:200]}",
            "context":        context,
            "call_attempted": True,
        }).execute()
        alert_id = resp.data[0]["id"] if resp.data else None
        logger.info("Emergency alert record persisted for parent %s: %s", parent_id, alert_id)
        return alert_id
    except Exception as e:
        logger.error("Alert persist failed for %s: %s", parent_id, e)
        return None


def _load_parent(db, parent_id: str) -> dict | None:
    try:
        rows = db.table("parents").select("*").eq("id", parent_id).execute().data
        return rows[0] if rows else None
    except Exception as e:
        logger.error("Parent fetch failed (%s): %s", parent_id, e)
        return None


def _load_children(db, family_id: str) -> list[dict]:
    if not family_id:
        return []
    try:
        return (
            db.table("children")
            .select("id, phone, name, is_primary")
            .eq("family_id", family_id)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error("Children fetch failed (family %s): %s", family_id, e)
        return []


def _load_backup_contact(db, family_id: str) -> str | None:
    try:
        rows = (
            db.table("families")
            .select("backup_contact")
            .eq("id", family_id)
            .execute()
            .data
        )
        if rows and rows[0].get("backup_contact"):
            return rows[0]["backup_contact"]
    except Exception as e:
        logger.warning("Backup contact fetch failed: %s", e)
    return None