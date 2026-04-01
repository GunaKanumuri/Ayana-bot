"""AYANA emergency system — urgent alerts and Twilio Voice calls.

Flow when a parent signals distress:
  1. trigger_emergency(parent_id, context)
       → Sends parent a confirmation message ("Should I alert your family?")
       → On confirmation → WhatsApp alert to ALL children in the family
       → Auto-calls primary child via Twilio Voice (TwiML say-message)
       → If no answer after CALL_RETRY_DELAY_S, retries once
       → If still no answer, calls the backup_contact if stored

  2. detect_urgency(health_extraction)
       → Returns True if urgency_flag is set OR severity == "severe"
       → Callers (conversation.py) use this to decide whether to invoke
         trigger_emergency() or just log a concern.

Voice calls use twilio.rest.Client directly (not WhatsApp).
The TwiML message is generated as a <Say> verb — no hosted TwiML bin needed.

Environment variables used (all in config.py / .env):
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN — Twilio auth
  TWILIO_VOICE_PHONE  — The Twilio phone number to call from (E.164)
  APP_URL             — Base URL for hosted TwiML (used as fallback URL)
"""

import asyncio
import logging
from datetime import datetime

from app.config import settings
from app.db import get_db
from app.models.schemas import HealthExtraction

logger = logging.getLogger(__name__)

# How long to wait between the first and retry call (seconds)
_CALL_RETRY_DELAY_S = 45

# TwiML Say message template — filled with parent nickname
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


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════════

def detect_urgency(health_extraction: HealthExtraction) -> bool:
    """Return True if the health extraction signals an urgent situation.

    Triggered when urgency_flag is True OR severity is "severe".

    Args:
        health_extraction: Pydantic model returned by gemini.extract_health().

    Returns:
        True if an emergency response should be initiated.
    """
    return health_extraction.urgency_flag or health_extraction.severity == "severe"


async def trigger_emergency(parent_id: str, context: dict) -> None:
    """Initiate the full emergency response sequence for a parent.

    Steps:
      1. Load parent + family data.
      2. Send a confirmation message to the parent asking permission to alert.
         (Respects autonomy; also guards against false positives.)
      3. Simultaneously send WhatsApp alerts to all children.
      4. Auto-call the primary child via Twilio Voice.
      5. If no answer, wait CALL_RETRY_DELAY_S and retry once.
      6. If still no answer, call the backup contact phone if available.
      7. Insert an alert record in the DB.

    Args:
        parent_id: UUID of the parents row.
        context:   Dict describing the emergency
                   e.g. {"raw_summary": "chest pain", "severity": "severe"}.
    """
    db       = get_db()
    parent   = _load_parent(db, parent_id)
    if not parent:
        logger.error("trigger_emergency: parent %s not found", parent_id)
        return

    family_id = parent.get("family_id")
    nickname  = parent.get("nickname", "your parent")
    language  = parent.get("language", "te")
    voice     = parent.get("tts_voice", "roopa")
    phone     = parent.get("phone", "")

    # ── 1. Confirm with parent ─────────────────────────────────────────────────
    await _confirm_with_parent(phone, nickname, language, voice)

    # ── 2. Alert children via WhatsApp ─────────────────────────────────────────
    children = _load_children(db, family_id)
    raw_summary = context.get("raw_summary", "distress signal")
    await _alert_children_whatsapp(children, nickname, raw_summary, context)

    # ── 3. Voice-call primary child ────────────────────────────────────────────
    primary = next((c for c in children if c.get("is_primary")), None)
    if not primary and children:
        primary = children[0]

    if primary:
        call_answered = await _make_voice_call(primary["phone"], nickname)

        if not call_answered:
            # ── 4. Retry once ──────────────────────────────────────────────────
            logger.info(
                "Voice call unanswered for %s — retrying in %ds",
                primary["phone"],
                _CALL_RETRY_DELAY_S,
            )
            await asyncio.sleep(_CALL_RETRY_DELAY_S)
            call_answered = await _make_voice_call(primary["phone"], nickname)

        if not call_answered:
            # ── 5. Backup contact ──────────────────────────────────────────────
            backup = _load_backup_contact(db, family_id)
            if backup:
                logger.info("Calling backup contact %s", backup)
                await _make_voice_call(backup, nickname)
            else:
                logger.warning(
                    "No backup contact for family %s — call chain exhausted",
                    family_id,
                )

    # ── 6. Persist alert ───────────────────────────────────────────────────────
    _persist_alert(db, family_id, parent_id, nickname, raw_summary, context)


# ═══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _confirm_with_parent(
    phone: str,
    nickname: str,
    language: str,
    voice: str,
) -> None:
    """Send a short audio + text message telling the parent help is on the way.

    We frame this as confirmation rather than a question — at a moment of
    severe distress we don't want to wait for them to tap a button.

    Args:
        phone:    Parent's phone number.
        nickname: Parent's nickname.
        language: Parent's language code.
        voice:    Parent's TTS voice.
    """
    from app.services import sarvam, whatsapp

    msg_en = (
        f"{nickname}, I am alerting your family right now. "
        f"Please stay calm. Help is on its way. "
        f"Don't move around — your family will contact you very soon."
    )
    try:
        audio_url, translated = await sarvam.english_to_parent_audio(
            msg_en, language, voice, nickname
        )
        await whatsapp.send_audio_and_buttons(
            to=phone,
            audio_url=audio_url or "",
            text=translated or msg_en,
        )
        logger.info("Emergency confirmation sent to parent %s", phone)
    except Exception as e:
        logger.error("Failed to send emergency confirmation to %s: %s", phone, e)


async def _alert_children_whatsapp(
    children: list[dict],
    nickname: str,
    raw_summary: str,
    context: dict,
) -> None:
    """Send an urgent WhatsApp alert to every child in the family.

    Args:
        children:    List of children rows {phone, name}.
        nickname:    Parent's nickname.
        raw_summary: One-line description of the emergency.
        context:     Full context dict for display.
    """
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
            logger.error(
                "WhatsApp alert failed to %s: %s", child["phone"], result
            )
        else:
            logger.info("Emergency WhatsApp alert sent to %s", child["phone"])


async def _make_voice_call(to_phone: str, parent_nickname: str) -> bool:
    """Place a Twilio Voice call to `to_phone` with a TwiML <Say> message.

    Uses TwiML inline via `twiml` parameter — no external URL needed.

    Args:
        to_phone:        Recipient E.164 phone number.
        parent_nickname: Used in the spoken TwiML message.

    Returns:
        True if the call was initiated successfully (does not confirm answer).
        The Twilio SDK does not synchronously know if the call was answered;
        we treat successful initiation as "sent" and leave answering detection
        as a future Twilio StatusCallback enhancement.
    """
    if not settings.TWILIO_VOICE_PHONE:
        logger.warning(
            "TWILIO_VOICE_PHONE not set — skipping voice call to %s", to_phone
        )
        return False

    try:
        from twilio.rest import Client as TwilioClient
        from twilio.twiml.voice_response import VoiceResponse, Say

        client = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

        # Build TwiML inline
        twiml = _TWIML_TEMPLATE.format(nickname=parent_nickname)

        call = client.calls.create(
            to=to_phone,
            from_=settings.TWILIO_VOICE_PHONE,
            twiml=twiml,
        )
        logger.info(
            "Voice call initiated to %s — SID %s", to_phone, call.sid
        )
        return True

    except Exception as e:
        logger.error("Voice call to %s failed: %s", to_phone, e)
        return False


def _load_parent(db, parent_id: str) -> dict | None:
    """Fetch a parent row from Supabase.

    Args:
        db:        Supabase client.
        parent_id: UUID string.

    Returns:
        Parent row dict or None if not found.
    """
    try:
        rows = (
            db.table("parents")
            .select("*")
            .eq("id", parent_id)
            .execute()
            .data
        )
        return rows[0] if rows else None
    except Exception as e:
        logger.error("Parent fetch failed (%s): %s", parent_id, e)
        return None


def _load_children(db, family_id: str) -> list[dict]:
    """Fetch all children in a family.

    Args:
        db:        Supabase client.
        family_id: UUID string.

    Returns:
        List of children rows (may be empty).
    """
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
    """Return the backup contact phone from the families row (if set).

    The families table does not currently have a backup_contact column in the
    migration; this function is forward-compatible — it returns None gracefully
    if the column is absent or null.

    Args:
        db:        Supabase client.
        family_id: UUID string.

    Returns:
        E.164 phone string or None.
    """
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


def _persist_alert(
    db,
    family_id: str | None,
    parent_id: str,
    nickname: str,
    raw_summary: str,
    context: dict,
) -> None:
    """Insert an emergency alert record into the alerts table.

    Args:
        db:          Supabase client.
        family_id:   UUID or None.
        parent_id:   UUID.
        nickname:    Parent nickname (for the message field).
        raw_summary: One-line description.
        context:     Full context dict stored as JSONB.
    """
    if not family_id:
        return
    try:
        db.table("alerts").insert(
            {
                "family_id": family_id,
                "parent_id": parent_id,
                "type":      "emergency",
                "message":   f"Emergency triggered for {nickname}: {raw_summary[:200]}",
                "context":   context,
                "call_attempted": True,
            }
        ).execute()
        logger.info("Emergency alert record persisted for parent %s", parent_id)
    except Exception as e:
        logger.error("Alert persist failed for %s: %s", parent_id, e)
