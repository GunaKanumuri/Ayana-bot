"""WhatsApp messaging service — supports Twilio sandbox and Meta Cloud API.

Switch between providers via WHATSAPP_PROVIDER env var.
All other code calls these functions — never Twilio/Meta directly.

Robustness features:
  - send_with_retry(): exponential backoff wrapper for all outbound
  - mark_read(): blue-tick read receipts (Meta only)
  - send_template(): WhatsApp template messages (24-hour window)
  - send_list(): list messages for menus > 3 options
"""

import asyncio
import logging
import httpx
import base64
from twilio.rest import Client as TwilioClient
from app.config import settings

logger = logging.getLogger(__name__)

_twilio: TwilioClient | None = None


def _get_twilio() -> TwilioClient:
    global _twilio
    if _twilio is None:
        _twilio = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    return _twilio


# ═══════════════ RETRY WRAPPER ═══════════════

async def send_with_retry(
    send_fn,
    *args,
    max_retries: int = 3,
    base_delay: float = 1.0,
    **kwargs,
) -> bool:
    """Exponential backoff wrapper for any send function.

    Retries on failure with delays of 1s, 2s, 4s (base_delay * 2^attempt).
    Logs each retry and gives up after max_retries.

    Args:
        send_fn:     Async or sync callable that returns bool.
        max_retries: Number of retries (default 3).
        base_delay:  Initial delay in seconds.
        *args, **kwargs: Forwarded to send_fn.

    Returns:
        True if any attempt succeeded, False if all failed.
    """
    for attempt in range(max_retries):
        try:
            if asyncio.iscoroutinefunction(send_fn):
                result = await send_fn(*args, **kwargs)
            else:
                result = send_fn(*args, **kwargs)
            if result:
                return True
        except Exception as e:
            logger.warning(
                "send_with_retry attempt %d/%d failed: %s",
                attempt + 1, max_retries, e,
            )
        if attempt < max_retries - 1:
            delay = base_delay * (2 ** attempt)
            logger.info("Retrying in %.1fs...", delay)
            await asyncio.sleep(delay)
    return False


# ═══════════════ SEND TEXT + BUTTONS ═══════════════

async def send_message(to: str, text: str, buttons: list[dict] | None = None) -> bool:
    """Send a WhatsApp text message with optional buttons.
    
    Args:
        to: Phone number (e.g., "+919876543210")
        text: Message body
        buttons: List of {id, title} dicts (max 3). Twilio doesn't support 
                 interactive buttons in sandbox, so we append as text.
    Returns:
        True if sent successfully.
    """
    if settings.WHATSAPP_PROVIDER == "meta":
        return await _meta_send_interactive(to, text, buttons)
    else:
        return _twilio_send_text(to, text, buttons)


async def send_audio(to: str, audio_url: str, caption: str = "") -> bool:
    """Send a WhatsApp audio message.
    
    Args:
        to: Phone number
        audio_url: Public URL of the audio file
        caption: Optional caption text
    """
    if settings.WHATSAPP_PROVIDER == "meta":
        return await _meta_send_audio(to, audio_url, caption)
    else:
        return _twilio_send_media(to, audio_url, caption)


async def send_audio_and_buttons(
    to: str,
    audio_url: str,
    text: str,
    buttons: list[dict] | None = None,
) -> bool:
    """Send audio message followed by text with buttons.
    
    This is the standard AYANA message format:
    1. Audio (parent hears it)
    2. Text + buttons (parent sees and taps)
    """
    audio_ok = await send_with_retry(send_audio, to, audio_url)
    text_ok = await send_with_retry(send_message, to, text, buttons)
    return audio_ok and text_ok


# ═══════════════ EXTRACT INCOMING MESSAGE ═══════════════

def extract_twilio_message(form_data: dict) -> dict:
    """Extract message from Twilio webhook form data.
    
    Returns:
        {phone, body, media_url, media_type, num_media, button_reply, is_voice_note, message_id}
    """
    phone = form_data.get("From", "").replace("whatsapp:", "")
    body = form_data.get("Body", "").strip()
    num_media = int(form_data.get("NumMedia", "0"))
    media_url = form_data.get("MediaUrl0", "") if num_media > 0 else ""
    media_type = form_data.get("MediaContentType0", "") if num_media > 0 else ""
    
    # Twilio sends button replies as regular text in sandbox
    button_reply = form_data.get("ButtonText", body)

    return {
        "phone": phone,
        "body": body,
        "media_url": media_url,
        "media_type": media_type,
        "num_media": num_media,
        "button_reply": button_reply,
        "is_voice_note": "audio" in media_type,
        "message_id": form_data.get("MessageSid", ""),
    }


async def extract_meta_message(payload: dict) -> dict | None:
    """Extract message from Meta Cloud API webhook payload.
    
    Returns:
        {phone, body, media_url, media_type, button_reply, is_voice_note, message_id}
        or None if not a user message.
    """
    try:
        entry = payload.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        
        if not messages:
            return None
        
        msg = messages[0]
        phone = msg.get("from", "")
        msg_type = msg.get("type", "")
        
        result = {
            "phone": f"+{phone}" if not phone.startswith("+") else phone,
            "body": "",
            "media_url": "",
            "media_type": "",
            "button_reply": "",
            "is_voice_note": False,
            "message_id": msg.get("id", ""),
        }
        
        if msg_type == "text":
            result["body"] = msg.get("text", {}).get("body", "")
            result["button_reply"] = result["body"]
        
        elif msg_type == "interactive":
            interactive = msg.get("interactive", {})
            int_type = interactive.get("type", "")
            if int_type == "button_reply":
                reply = interactive.get("button_reply", {})
                result["button_reply"] = reply.get("title", "")
                result["body"] = reply.get("id", "")
            elif int_type == "list_reply":
                reply = interactive.get("list_reply", {})
                result["button_reply"] = reply.get("title", "")
                result["body"] = reply.get("id", "")
        
        elif msg_type == "audio":
            audio = msg.get("audio", {})
            media_id = audio.get("id", "")
            if media_id:
                result["media_url"] = await _meta_download_media(media_id)
                result["media_type"] = audio.get("mime_type", "audio/ogg")
                result["is_voice_note"] = audio.get("voice", False)
        
        return result
    except Exception as e:
        logger.error(f"Failed to extract Meta message: {e}", exc_info=True)
        return None


# ═══════════════ DOWNLOAD VOICE NOTE ═══════════════

async def download_voice_note(url: str) -> bytes | None:
    """Download a voice note from WhatsApp (works for both providers)."""
    try:
        if settings.WHATSAPP_PROVIDER == "meta":
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}"},
                )
                resp.raise_for_status()
                return resp.content
        else:
            # Twilio media URL — needs auth
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    url,
                    auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
                )
                resp.raise_for_status()
                return resp.content
    except Exception as e:
        logger.error(f"Failed to download voice note: {e}")
        return None


# ═══════════════ MARK READ (BLUE TICKS) ═══════════════

async def mark_read(message_id: str) -> bool:
    """Send a read receipt (blue ticks) for a received message.

    Only works on Meta Cloud API — Twilio sandbox doesn't support this.
    Called after processing each incoming message for a more human feel.

    Args:
        message_id: The wamid (WhatsApp message ID) from the incoming payload.

    Returns:
        True if the read receipt was sent successfully.
    """
    if settings.WHATSAPP_PROVIDER != "meta" or not message_id:
        return False
    try:
        url = f"https://graph.facebook.com/v21.0/{settings.META_PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.debug("mark_read failed for %s: %s", message_id, e)
        return False


# ═══════════════ SEND TEMPLATE (META 24H WINDOW) ═══════════════

async def send_template(
    to: str,
    template_name: str,
    language: str = "en",
    components: list[dict] | None = None,
) -> bool:
    """Send a pre-approved WhatsApp template message via Meta Cloud API.

    Template messages are required to re-open a conversation after the
    24-hour session window has expired. They must be pre-approved in the
    Meta Business Manager.

    For Twilio sandbox: falls back to send_message() since sandbox
    doesn't enforce the 24h window.

    Args:
        to:            Recipient phone number.
        template_name: Name of the approved template (e.g. "ayana_morning_greeting").
        language:      Template language code (e.g. "en", "te", "hi").
        components:    Optional template components for dynamic values.

    Returns:
        True if sent successfully.
    """
    if settings.WHATSAPP_PROVIDER != "meta":
        # Twilio sandbox fallback — templates not applicable
        logger.debug("send_template: Twilio mode, falling back to send_message")
        return await send_message(to, f"[Template: {template_name}]")

    try:
        url = f"https://graph.facebook.com/v21.0/{settings.META_PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        phone = to.replace("+", "").replace("whatsapp:", "")

        # Map our 2-letter codes to Meta's template language codes
        meta_lang_map = {
            "te": "te", "hi": "hi", "ta": "ta", "kn": "kn",
            "ml": "ml", "bn": "bn", "mr": "mr", "gu": "gu",
            "pa": "pa", "en": "en",
        }
        meta_lang = meta_lang_map.get(language, "en")

        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": meta_lang},
            },
        }
        if components:
            payload["template"]["components"] = components

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            logger.info("Template '%s' sent to %s", template_name, phone)
            return True
    except Exception as e:
        logger.error("Template send failed to %s: %s", to, e)
        return False


# ═══════════════ SEND LIST MESSAGE ═══════════════

async def send_list(
    to: str,
    body_text: str,
    button_text: str,
    sections: list[dict],
) -> bool:
    """Send a list message with selectable rows (Meta Cloud API only).

    List messages support up to 10 rows per section and multiple sections.
    Useful for menus with more than 3 options.

    For Twilio: falls back to numbered text.

    Args:
        to:          Recipient phone number.
        body_text:   Message body above the list button.
        button_text: Text shown on the list button (max 20 chars).
        sections:    List of sections, each:
                     {"title": "...", "rows": [{"id": "...", "title": "...", "description": "..."}]}

    Returns:
        True if sent successfully.
    """
    if settings.WHATSAPP_PROVIDER != "meta":
        # Twilio fallback: render as numbered text
        lines = [body_text, ""]
        idx = 1
        for section in sections:
            if section.get("title"):
                lines.append(f"*{section['title']}*")
            for row in section.get("rows", []):
                lines.append(f"{idx}. {row.get('title', '')}")
                idx += 1
        return _twilio_send_text(to, "\n".join(lines))

    try:
        url = f"https://graph.facebook.com/v21.0/{settings.META_PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        phone = to.replace("+", "").replace("whatsapp:", "")

        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body_text},
                "action": {
                    "button": button_text[:20],
                    "sections": sections,
                },
            },
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            logger.info("List message sent to %s", phone)
            return True
    except Exception as e:
        logger.error("List send failed to %s: %s", to, e)
        return False


# ═══════════════ PRIVATE — TWILIO ═══════════════

def _twilio_send_text(to: str, text: str, buttons: list[dict] | None = None) -> bool:
    """Send via Twilio. Sandbox doesn't support interactive buttons, so we
    render them as numbered options. Parent replies "1", "2", or "3" and the
    conversation engine resolves the number back to a button action.

    Format:
        <translated text>

        *Reply with a number:*
        1️⃣ బాగున్నాను
        2️⃣ ఓకే గా ఉన్నాను
        3️⃣ బాగా లేను
    """
    _NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣"]

    try:
        client = _get_twilio()
        if not to.startswith("whatsapp:"):
            to = f"whatsapp:{to}"

        # Language-aware reply prompt — no English shown to Telugu/Hindi parents
        _REPLY_PROMPTS = {
            "te": "సంఖ్య reply చేయండి:",
            "hi": "नंबर से जवाब दें:",
            "ta": "எண்ணில் பதில் அனுப்பவும்:",
            "kn": "ಸಂಖ್ಯೆಯಲ್ಲಿ ಉತ್ತರಿಸಿ:",
            "ml": "നമ്പർ ഉപയോഗിച്ച് മറുപടി നൽകൂ:",
            "en": "Reply with a number:",
        }

        body = text
        if buttons:
            # Detect language from button titles (Telugu/Hindi chars present)
            sample = " ".join(b.get("title","") for b in buttons[:3])
            if any("\u0c00" <= c <= "\u0c7f" for c in sample):
                lang_key = "te"
            elif any("\u0900" <= c <= "\u097f" for c in sample):
                lang_key = "hi"
            elif any("\u0b80" <= c <= "\u0bff" for c in sample):
                lang_key = "ta"
            else:
                lang_key = "en"
            prompt = _REPLY_PROMPTS.get(lang_key, "Reply with a number:")
            body += f"\n\n*{prompt}*\n"
            for i, btn in enumerate(buttons[:3], 0):
                emoji  = btn.get("emoji", "")
                title  = btn.get("title", btn.get("text", f"Option {i+1}"))
                number = _NUMBER_EMOJI[i]
                body += f"{number} {emoji} {title}\n"

        msg = client.messages.create(
            body=body,
            from_=settings.TWILIO_WHATSAPP_FROM,
            to=to,
        )
        logger.info(f"Twilio sent to {to}: {msg.sid}")
        return True
    except Exception as e:
        logger.error(f"Twilio send failed to {to}: {e}")
        return False


def _twilio_send_media(to: str, media_url: str, caption: str = "") -> bool:
    """Send media via Twilio."""
    try:
        client = _get_twilio()
        if not to.startswith("whatsapp:"):
            to = f"whatsapp:{to}"
        
        msg = client.messages.create(
            body=caption or " ",
            media_url=[media_url],
            from_=settings.TWILIO_WHATSAPP_FROM,
            to=to,
        )
        logger.info(f"Twilio media sent to {to}: {msg.sid}")
        return True
    except Exception as e:
        logger.error(f"Twilio media send failed to {to}: {e}")
        return False


# ═══════════════ PRIVATE — META CLOUD API ═══════════════

async def _meta_send_interactive(to: str, text: str, buttons: list[dict] | None = None) -> bool:
    """Send interactive message via Meta Cloud API."""
    try:
        url = f"https://graph.facebook.com/v21.0/{settings.META_PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        
        phone = to.replace("+", "").replace("whatsapp:", "")
        
        if buttons and len(buttons) <= 3:
            payload = {
                "messaging_product": "whatsapp",
                "to": phone,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": text},
                    "action": {
                        "buttons": [
                            {
                                "type": "reply",
                                "reply": {
                                    "id": btn.get("id", f"btn_{i}"),
                                    "title": f"{btn.get('emoji', '')} {btn.get('title', '')}".strip()[:20],
                                }
                            }
                            for i, btn in enumerate(buttons)
                        ]
                    }
                }
            }
        else:
            payload = {
                "messaging_product": "whatsapp",
                "to": phone,
                "type": "text",
                "text": {"body": text},
            }
        
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            logger.info(f"Meta sent to {phone}")
            return True
    except Exception as e:
        logger.error(f"Meta send failed to {to}: {e}")
        return False


async def _meta_send_audio(to: str, audio_url: str, caption: str = "") -> bool:
    """Send audio via Meta Cloud API."""
    try:
        url = f"https://graph.facebook.com/v21.0/{settings.META_PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        phone = to.replace("+", "").replace("whatsapp:", "")
        
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "audio",
            "audio": {"link": audio_url},
        }
        
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.error(f"Meta audio send failed to {to}: {e}")
        return False


async def _meta_download_media(media_id: str) -> str:
    """Get download URL for a Meta media object."""
    try:
        url = f"https://graph.facebook.com/v21.0/{media_id}"
        headers = {"Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}"}
        
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json().get("url", "")
    except Exception as e:
        logger.error(f"Meta media download failed: {e}")
        return ""