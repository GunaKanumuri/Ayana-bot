"""WhatsApp messaging service — supports Twilio sandbox and Meta Cloud API.

Switch between providers via WHATSAPP_PROVIDER env var.
All other code calls these functions — never Twilio/Meta directly.
"""

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
    audio_ok = await send_audio(to, audio_url)
    text_ok = await send_message(to, text, buttons)
    return audio_ok and text_ok


# ═══════════════ EXTRACT INCOMING MESSAGE ═══════════════

def extract_twilio_message(form_data: dict) -> dict:
    """Extract message from Twilio webhook form data.
    
    Returns:
        {phone, body, media_url, media_type, num_media, button_reply}
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
    }


async def extract_meta_message(payload: dict) -> dict | None:
    """Extract message from Meta Cloud API webhook payload.
    
    Returns:
        {phone, body, media_url, media_type, button_reply, is_voice_note}
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


# ═══════════════ PRIVATE — TWILIO ═══════════════

def _twilio_send_text(to: str, text: str, buttons: list[dict] | None = None) -> bool:
    """Send via Twilio. Buttons appended as text (sandbox limitation)."""
    try:
        client = _get_twilio()
        if not to.startswith("whatsapp:"):
            to = f"whatsapp:{to}"
        
        body = text
        if buttons:
            body += "\n\n"
            for i, btn in enumerate(buttons, 1):
                emoji = btn.get("emoji", "")
                title = btn.get("title", btn.get("text", f"Option {i}"))
                body += f"{emoji} {title}\n"
        
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
