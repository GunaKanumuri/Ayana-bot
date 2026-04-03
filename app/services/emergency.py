"""AYANA emergency system — urgent alerts and Twilio Voice calls.

LANGUAGE-AGNOSTIC DESIGN:
  All parent messages are translated to English by Sarvam BEFORE reaching
  this module. Therefore we only need ONE set of English keywords.
  This works for all 10 languages Sarvam supports — no static translations.

Flow when a parent signals distress:
  1. trigger_emergency(parent_id, context)
       → Sends parent a confirmation (help is on the way)
       → WhatsApp alert to ALL children in the family
       → Auto-calls primary child via Twilio Voice
       → Retry once → backup contact fallback

  2. detect_urgency(health_extraction)
       → Returns True if urgency_flag is set OR severity == "severe"

  3. fast_keyword_check(english_text)
       → Runs on ENGLISH translation (post-Sarvam)
       → One keyword set covers all 10 languages
"""

import asyncio
import logging
from datetime import datetime

from app.config import settings
from app.db import get_db
from app.models.schemas import HealthExtraction

logger = logging.getLogger(__name__)

_CALL_RETRY_DELAY_S = 45

_TWIML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Aditi" language="hi-IN">
    This is AYANA. {nickname} may need immediate help. Please call them right away.
  </Say>
  <Pause length="2"/>
  <Say voice="Polly.Aditi" language="hi-IN">
    Repeating: {nickname} may need immediate help. Please call them right away.
  </Say>
</Response>"""


# ─── Critical keywords — ENGLISH ONLY ────────────────────────────────────────
# All parent messages arrive here ALREADY translated to English by Sarvam.
# One comprehensive English list → covers Te, Hi, Ta, Kn, Ml, Bn, Mr, Gu, Pa, En.

_CRITICAL_KEYWORDS: set[str] = {
    # Cardiac / breathing
    "chest pain", "heart attack", "heart pain", "can't breathe",
    "cannot breathe", "difficulty breathing", "breathless",
    "shortness of breath", "not breathing", "suffocating",
    # Falls / injuries
    "fell down", "fall down", "fallen down", "collapsed",
    "fainted", "lost consciousness", "slipped", "fracture",
    # Bleeding / stroke
    "bleeding", "blood coming", "heavy bleeding",
    "stroke", "paralysis", "face drooping", "numb",
    # Consciousness
    "unconscious", "unresponsive", "not waking up", "coma",
    # General distress
    "help me", "emergency", "ambulance", "dying",
    "very serious", "critical condition", "can't move",
    "severe pain", "unbearable pain", "extreme pain",
    # Choking / poisoning / burns
    "choking", "swallowed", "poisoning", "poison",
    "burnt", "burning", "scalded",
}

# Ambulance numbers — 108 is universal across most Indian states
AMBULANCE_NUMBERS: dict[str, str] = {
    "default": "108",
    "police":  "100",
    "fire":    "101",
    "national": "112",
}


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════════

def detect_urgency(health_extraction: HealthExtraction) -> bool:
    """Return True if the health extraction signals an urgent situation."""
    return health_extraction.urgency_flag or health_extraction.severity == "severe"


def fast_keyword_check(english_text: str) -> bool:
    """Check ENGLISH text for critical emergency keywords.

    The caller must pass text ALREADY translated to English by Sarvam.
    This single English check covers all 10 supported languages.
    """
    if not english_text:
        return False
    text_lower = english_text.lower()
    return any(kw in text_lower for kw in _CRITICAL_KEYWORDS)


async def handle_child_emergency_reply(child: dict, action: str) -> None:
    """Handle a child's response to an emergency alert."""
    from app.services.whatsapp import send_message

    db = get_db()
    phone = child["phone"]
    family_id = child.get("family_id")

    if not family_id:
        return

    try:
        alerts = (
            db.table("alerts")
            .select("id, parent_id, message")
            .eq("family_id", family_id)
            .eq("type", "emergency")
            .eq("acknowledged", False)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data or []
        )

        if not alerts:
            await send_message(phone, "No active alerts to acknowledge.")
            return

        alert = alerts[0]

        if action in ("calling_now", "resolved"):
            db.table("alerts").update({"acknowledged": True}).eq(
                "id", alert["id"]
            ).execute()

            if action == "resolved":
                await send_message(phone, "✅ Alert marked as *resolved*. Thank you for checking in.")
            else:
                await send_message(phone, "✅ Got it — you're calling now. Alert acknowledged.")
            logger.info("Emergency alert %s acknowledged by %s (action=%s)", alert["id"], phone, action)

        elif action == "need_help":
            backup = _load_backup_contact(db, family_id)
            if backup:
                parent_rows = db.table("parents").select("nickname").eq("id", alert["parent_id"]).execute().data
                nickname = parent_rows[0]["nickname"] if parent_rows else "parent"
                await _make_voice_call(backup, nickname)
                await send_message(phone, f"📞 Calling backup contact {backup}...")
            else:
                await send_message(phone, f"⚠️ No backup contact set. Consider calling {AMBULANCE_NUMBERS['default']} (ambulance).")

    except Exception as e:
        logger.error("handle_child_emergency_reply failed: %s", e, exc_info=True)


async def trigger_emergency(parent_id: str, context: dict) -> None:
    """Initiate the full emergency response sequence for a parent."""
    db = get_db()
    parent = _load_parent(db, parent_id)
    if not parent:
        logger.error("trigger_emergency: parent %s not found", parent_id)
        return

    family_id = parent.get("family_id")
    nickname  = parent.get("nickname", "your parent")
    language  = parent.get("language", "te")
    voice     = parent.get("tts_voice", "roopa")
    phone     = parent.get("phone", "")

    await _confirm_with_parent(phone, nickname, language, voice)

    children = _load_children(db, family_id)
    raw_summary = context.get("raw_summary", "distress signal")
    await _alert_children_whatsapp(children, nickname, raw_summary, context)

    primary = next((c for c in children if c.get("is_primary")), None)
    if not primary and children:
        primary = children[0]

    if primary:
        call_answered = await _make_voice_call(primary["phone"], nickname)
        if not call_answered:
            logger.info("Voice call unanswered — retrying in %ds", _CALL_RETRY_DELAY_S)
            await asyncio.sleep(_CALL_RETRY_DELAY_S)
            call_answered = await _make_voice_call(primary["phone"], nickname)

        if not call_answered:
            backup = _load_backup_contact(db, family_id)
            if backup:
                logger.info("Calling backup contact %s", backup)
                await _make_voice_call(backup, nickname)
            else:
                logger.warning("No backup contact for family %s — call chain exhausted", family_id)

    _persist_alert(db, family_id, parent_id, nickname, raw_summary, context)


# ═══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _confirm_with_parent(phone: str, nickname: str, language: str, voice: str) -> None:
    """Tell the parent help is on the way (translated + audio in their language)."""
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
        logger.error("Failed to send emergency confirmation to %s: %s", phone, e)


async def _alert_children_whatsapp(children: list[dict], nickname: str, raw_summary: str, context: dict) -> None:
    """Send an urgent WhatsApp alert to every child in the family."""
    from app.services.whatsapp import send_message

    severity = context.get("severity", "severe")
    concerns = ", ".join(context.get("concerns", [])) or raw_summary

    message = (
        f"🚨 *AYANA EMERGENCY — {nickname}*\n\n"
        f"*{nickname}* is in distress and may need immediate help.\n\n"
        f"*What they reported:* _{concerns}_\n"
        f"*Severity:* {severity.upper()}\n\n"
        f"Please call {nickname} right away or go to them immediately.\n\n"
        f"_AYANA is also calling you now._"
    )

    tasks = [send_message(child["phone"], message) for child in children]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for child, result in zip(children, results):
        if isinstance(result, Exception):
            logger.error("WhatsApp alert failed to %s: %s", child["phone"], result)


async def _make_voice_call(to_phone: str, parent_nickname: str) -> bool:
    """Place a Twilio Voice call with TwiML inline."""
    if not settings.TWILIO_VOICE_PHONE:
        logger.warning("TWILIO_VOICE_PHONE not set — skipping voice call to %s", to_phone)
        return False
    try:
        from twilio.rest import Client as TwilioClient
        client = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        twiml = _TWIML_TEMPLATE.format(nickname=parent_nickname)
        call = client.calls.create(to=to_phone, from_=settings.TWILIO_VOICE_PHONE, twiml=twiml)
        logger.info("Voice call initiated to %s — SID %s", to_phone, call.sid)
        return True
    except Exception as e:
        logger.error("Voice call to %s failed: %s", to_phone, e)
        return False


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
        return db.table("children").select("id, phone, name, is_primary").eq("family_id", family_id).execute().data or []
    except Exception as e:
        logger.error("Children fetch failed (family %s): %s", family_id, e)
        return []


def _load_backup_contact(db, family_id: str) -> str | None:
    try:
        rows = db.table("families").select("backup_contact").eq("id", family_id).execute().data
        if rows and rows[0].get("backup_contact"):
            return rows[0]["backup_contact"]
    except Exception as e:
        logger.warning("Backup contact fetch failed: %s", e)
    return None


def _persist_alert(db, family_id, parent_id, nickname, raw_summary, context) -> None:
    if not family_id:
        return
    try:
        db.table("alerts").insert({
            "family_id": family_id,
            "parent_id": parent_id,
            "type": "emergency",
            "message": f"Emergency triggered for {nickname}: {raw_summary[:200]}",
            "context": context,
            "call_attempted": True,
        }).execute()
        logger.info("Emergency alert record persisted for parent %s", parent_id)
    except Exception as e:
        logger.error("Alert persist failed for %s: %s", parent_id, e)
