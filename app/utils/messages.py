"""AYANA message pipeline helper — convenience wrapper for the TTS pipeline.

Every touchpoint that AYANA sends to a parent goes through this pipeline:
  English text → Sarvam translate → Sarvam TTS → audio URL
  Button labels → Sarvam translate (parallel)

This module exposes a single function `prepare_parent_message()` that
runs the full pipeline and returns a ready-to-send bundle, so callers
never have to remember the correct call order or handle fallbacks.

Usage example
─────────────
    from app.utils.messages import prepare_parent_message

    bundle = await prepare_parent_message(
        english_text="Good morning {nickname}! How are you feeling?",
        parent=parent_row,
        buttons=[
            {"emoji": "😊", "text_english": "Feeling good", "action": "mood_good"},
            {"emoji": "😐", "text_english": "Okay",          "action": "mood_okay"},
            {"emoji": "😔", "text_english": "Not well",      "action": "mood_bad"},
        ],
        include_voice_invite=True,
    )

    await whatsapp.send_audio_and_buttons(
        to=parent["phone"],
        audio_url=bundle["audio_url"] or "",
        text=bundle["translated_text"],
        buttons=bundle["translated_buttons"] or None,
    )

Return value shape
──────────────────
    {
        "audio_url":          str | None,   # public URL or None if TTS failed
        "translated_text":    str,          # parent-language text (fallback = English)
        "translated_buttons": list[dict],   # [{id, title, emoji}, …] up to 3 items
        "language":           str,          # language code used
    }
"""

import asyncio
import logging

from app.services import sarvam

logger = logging.getLogger(__name__)

# Voice-invite suffix — appended when include_voice_invite=True
_VOICE_INVITE_EN = "\n(You can also reply with a voice message 🎤)"


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC
# ═══════════════════════════════════════════════════════════════════════════════

async def prepare_parent_message(
    english_text: str,
    parent: dict,
    buttons: list[dict] | None = None,
    include_voice_invite: bool = False,
) -> dict:
    """Run the full AYANA message pipeline for a single message.

    Steps:
      1. Replace {nickname} placeholder in english_text.
      2. Translate english_text → parent's language via Sarvam.
      3. Generate TTS audio → save to /tmp/ayana_audio → return public URL.
      4. If include_voice_invite is True, translate and append the invite suffix.
      5. Translate button labels → parent's language in parallel.

    Failures at any step are caught and logged; the function always returns
    a usable bundle (text falls back to English, audio_url may be None).

    Args:
        english_text:         Message body in English. Use {nickname} as
                              placeholder for the parent's name.
        parent:               Full parent row from Supabase
                              (needs: language, tts_voice, nickname).
        buttons:              Optional list of button dicts.
                              Input format:  {emoji, text_english, action}
                              Output format: {id, title, emoji}
        include_voice_invite: If True, appends a translated voice-message
                              invite to the translated_text.

    Returns:
        {
            "audio_url":          str | None,
            "translated_text":    str,
            "translated_buttons": list[dict],
            "language":           str,
        }
    """
    language = parent.get("language", "te")
    voice    = parent.get("tts_voice", "roopa")
    nickname = parent.get("nickname", "")

    # ── 1. Personalise ────────────────────────────────────────────────────────
    text = english_text.replace("{nickname}", nickname) if nickname else english_text

    # ── 2 + 3. Translate + TTS ────────────────────────────────────────────────
    audio_url: str | None = None
    translated_text: str  = text  # fallback

    try:
        audio_url, translated = await sarvam.english_to_parent_audio(
            text, language, voice, nickname
        )
        if translated:
            translated_text = translated
    except Exception as e:
        logger.error(
            "prepare_parent_message: TTS pipeline failed (%s): %s",
            parent.get("id", "?"), e,
        )

    # ── 4. Voice invite suffix ────────────────────────────────────────────────
    if include_voice_invite:
        try:
            invite_tr = await sarvam.translate(_VOICE_INVITE_EN, "en", language)
            translated_text += (invite_tr or _VOICE_INVITE_EN)
        except Exception as e:
            logger.warning("Voice invite translate failed: %s", e)
            translated_text += _VOICE_INVITE_EN

    # ── 5. Translate buttons ──────────────────────────────────────────────────
    translated_buttons: list[dict] = []
    if buttons:
        translated_buttons = await _translate_buttons(buttons[:3], language)

    return {
        "audio_url":          audio_url,
        "translated_text":    translated_text,
        "translated_buttons": translated_buttons,
        "language":           language,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PRIVATE
# ═══════════════════════════════════════════════════════════════════════════════

async def _translate_buttons(
    buttons: list[dict],
    language: str,
) -> list[dict]:
    """Translate button labels to parent's language in parallel.

    Input  format: {emoji, text_english, action}
    Output format: {id, title, emoji}

    WhatsApp enforces a 20-character limit on button title.
    Emoji is preserved; only text_english is translated.

    Args:
        buttons:  List of button dicts (max 3 enforced by caller).
        language: Target language code.

    Returns:
        List of translated button dicts ready for whatsapp.send_message().
    """
    async def _one(btn: dict, idx: int) -> dict:
        text_en = btn.get("text_english", "")
        if language == "en":
            title = text_en
        else:
            try:
                title = await sarvam.translate(text_en, "en", language) or text_en
            except Exception:
                title = text_en
        emoji = btn.get("emoji", "")
        return {
            "id":    btn.get("action", f"btn_{idx}"),
            "title": title[:20],
            "emoji": emoji,
        }

    tasks = [_one(btn, i) for i, btn in enumerate(buttons)]
    return list(await asyncio.gather(*tasks))
