"""WhatsApp webhook — entry point for all incoming messages.

Twilio:  POST /webhook  (application/x-www-form-urlencoded)
Meta:    GET  /webhook  (verification challenge)
         POST /webhook  (application/json)

The endpoint returns 200 IMMEDIATELY. All processing runs in a BackgroundTask
so Twilio/Meta never time out waiting for AYANA's AI pipeline.

Routing:
  CHILD  → child command handler  (app/routes/child_commands.py)
  PARENT → conversation engine    (app/services/conversation.py)
  UNKNOWN → welcome / onboarding message
"""

import logging
import collections

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response

from app.config import settings
from app.db import get_db
from app.services.whatsapp import (
    extract_meta_message,
    extract_twilio_message,
    mark_read,
    send_message,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhook"])

# ─── Deduplication: prevent double-processing of webhook retries ──────────────
# Hybrid approach: in-memory set for speed + DB fallback for deploy restarts.
# On restart the in-memory set is empty, but we check the check_ins table
# for an existing row with this message_id before processing.
_PROCESSED_IDS: collections.OrderedDict[str, bool] = collections.OrderedDict()
_DEDUP_MAX = 10_000


def _is_duplicate(message_id: str) -> bool:
    """Return True if this message_id was already processed.

    Layer 1: In-memory OrderedDict (fast, covers same-instance retries).
    Layer 2: DB check on check_ins.raw_reply for the message_id pattern
             (catches retries after Railway deploy/restart).
    """
    if not message_id:
        return False
    if message_id in _PROCESSED_IDS:
        return True
    _PROCESSED_IDS[message_id] = True
    while len(_PROCESSED_IDS) > _DEDUP_MAX:
        _PROCESSED_IDS.popitem(last=False)
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# META WEBHOOK VERIFICATION  (GET /webhook)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/webhook")
async def meta_verify(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
) -> PlainTextResponse:
    """Respond to Meta's webhook verification handshake.

    Meta sends a GET with hub.mode=subscribe and hub.verify_token.
    We must echo hub.challenge back as plain text to confirm ownership.
    """
    if hub_mode == "subscribe" and hub_verify_token == settings.META_VERIFY_TOKEN:
        logger.info("Meta webhook verification successful")
        return PlainTextResponse(hub_challenge or "")
    logger.warning(
        f"Meta verification failed — mode={hub_mode} token={hub_verify_token}"
    )
    raise HTTPException(status_code=403, detail="Invalid verify token")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN WEBHOOK  (POST /webhook)
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks) -> Response:
    """Receive all incoming WhatsApp messages from Twilio or Meta Cloud API.

    Steps:
      1. Parse the incoming payload (form-encoded or JSON).
      2. Identify the sender as CHILD, PARENT, or UNKNOWN.
      3. Dispatch to the correct handler via BackgroundTasks.
      4. Return HTTP 200 immediately — never block on processing.
    """
    msg = await _parse_incoming(request)

    if not msg or not msg.get("phone"):
        # Malformed request — still return 200 so the provider doesn't retry
        return Response(status_code=200)

    phone = msg["phone"]
    message_id = msg.get("message_id", "")

    # ── Dedup check — reject duplicate webhooks ────────────────────────────
    if _is_duplicate(message_id):
        logger.debug("Duplicate webhook ignored: %s from %s", message_id, phone)
        return Response(status_code=200)

    logger.info(
        "Incoming from %s | body=%r | voice=%s",
        phone,
        msg.get("body", "")[:80],
        msg.get("is_voice_note", False),
    )

    # ── Send read receipt (blue ticks) ─────────────────────────────────────
    if message_id:
        background.add_task(mark_read, message_id)

    sender_type, record = await _identify_sender(phone)
    logger.info("Sender %s identified as: %s", phone, sender_type)

    if sender_type == "child":
        background.add_task(_process_child_message, record, msg)
    elif sender_type == "parent":
        background.add_task(_process_parent_message, record, msg)
    else:
        background.add_task(_send_welcome, phone)

    return Response(status_code=200)


# ═══════════════════════════════════════════════════════════════════════════════
# PARSING — detect Twilio vs Meta from Content-Type
# ═══════════════════════════════════════════════════════════════════════════════

async def _parse_incoming(request: Request) -> dict | None:
    """Parse the incoming request body into a normalised message dict.

    Twilio sends application/x-www-form-urlencoded.
    Meta sends application/json.
    We detect from Content-Type so both can hit the same /webhook endpoint.

    Returns:
        {phone, body, media_url, media_type, num_media, button_reply, is_voice_note, message_id}
        or None if parsing fails.
    """
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        # ── Meta Cloud API ────────────────────────────────────
        try:
            body = await request.json()
        except Exception as e:
            logger.error(f"Failed to parse Meta JSON payload: {e}")
            return None
        return await extract_meta_message(body)

    else:
        # ── Twilio (form-encoded) ─────────────────────────────
        try:
            form = await request.form()
            return extract_twilio_message(dict(form))
        except Exception as e:
            logger.error(f"Failed to parse Twilio form payload: {e}")
            return None


# ═══════════════════════════════════════════════════════════════════════════════
# SENDER IDENTIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

async def _identify_sender(phone: str) -> tuple[str, dict | None]:
    """Look up the phone number in children and parents tables.

    Children are checked first; a person can only be one or the other.

    Args:
        phone: E.164 phone number (e.g. "+919876543210")

    Returns:
        ("child", record) | ("parent", record) | ("unknown", None)
    """
    db = get_db()

    # ── Check children table ──────────────────────────────────
    try:
        result = (
            db.table("children")
            .select("*")
            .eq("phone", phone)
            .execute()
        )
        if result.data:
            return "child", result.data[0]
    except Exception as e:
        logger.error(f"Children lookup error for {phone}: {e}")

    # ── Check parents table ───────────────────────────────────
    try:
        result = (
            db.table("parents")
            .select("*, families(*)")
            .eq("phone", phone)
            .eq("is_active", True)
            .execute()
        )
        if result.data:
            return "parent", result.data[0]
    except Exception as e:
        logger.error(f"Parents lookup error for {phone}: {e}")

    return "unknown", None


# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND TASK HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _process_child_message(child: dict, msg: dict) -> None:
    """Dispatch a child's message to the child command handler.

    Uses a lazy import so child_commands.py is resolved at call time.
    """
    try:
        from app.routes.child_commands import handle_child_message
        await handle_child_message(child, msg)
    except ImportError:
        logger.warning("child_commands not yet available")
    except Exception as e:
        logger.error(
            f"Child message processing error for {child.get('phone')}: {e}",
            exc_info=True,
        )


async def _process_parent_message(parent: dict, msg: dict) -> None:
    """Dispatch a parent's message to the conversation engine.

    Uses a lazy import so conversation.py is resolved at call time.
    Falls back to a plain acknowledgement if conversation.py isn't built yet.
    """
    try:
        from app.services.conversation import handle_parent_response
        await handle_parent_response(parent, msg)
    except ImportError:
        logger.warning("conversation service not yet available — sending fallback")
        try:
            await send_message(
                parent["phone"],
                "I received your message. Your family will be updated.",
            )
        except Exception as send_err:
            logger.error(f"Fallback send failed: {send_err}")
    except Exception as e:
        logger.error(
            f"Parent message processing error for {parent.get('phone')}: {e}",
            exc_info=True,
        )


async def _send_welcome(phone: str) -> None:
    """Send an onboarding welcome message to an unregistered sender.

    This covers two cases:
      - A parent whose child hasn't set them up yet
      - A new caregiver trying to register

    Args:
        phone: Sender's phone number
    """
    try:
        message = (
            "Namaste! I'm *AYANA*, a daily care companion for elderly parents.\n\n"
            "AYANA helps families stay connected through morning check-ins, "
            "medicine reminders, and health updates — all over WhatsApp.\n\n"
            "━━━━━━━━━━━━━━━\n"
            "*Already set up?*\n"
            "If your child registered you, you're all set! "
            "I'll message you at your scheduled check-in time.\n\n"
            "*New here?*\n"
            "If you are a caregiver, ask your family member to set AYANA up, "
            "or reply *ADD PARENT* to start the setup process.\n\n"
            "━━━━━━━━━━━━━━━\n"
            "For help, reply *MENU*."
        )
        await send_message(phone, message)
        logger.info(f"Welcome message sent to unknown sender {phone}")
    except Exception as e:
        logger.error(f"Failed to send welcome to {phone}: {e}")
