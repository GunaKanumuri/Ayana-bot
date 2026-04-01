"""AYANA end-to-end test script.

Tests Sarvam TTS, Sarvam Translate, and WhatsApp delivery in one shot.
The parent will receive:
  1. An audio message (Telugu TTS)
  2. A text message with buttons

Usage
─────
Edit TO_PHONE below (or pass as CLI argument), then run:

    cd ayana
    python test_flow.py
    python test_flow.py +919876543210

The script:
  1. Translates the test message from English → Telugu via Sarvam
  2. Generates Telugu TTS audio via Sarvam Bulbul
  3. Saves audio to /tmp/ayana_audio/test_telugu.wav
  4. Sends audio + text + 3 buttons to TO_PHONE via WhatsApp (Twilio or Meta)

Expected outcome on your phone:
  - You hear: "Subhodayam Bujjamma! Epudu ela unnav?"
    (Good morning Bujjamma! How are you doing?)
  - You see the translated text + 3 tappable buttons
"""

import asyncio
import os
import sys
import logging

# ─── Load .env before importing app modules ────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from app.config import settings
from app.services import sarvam, whatsapp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
logger = logging.getLogger("test_flow")

# ─── Configuration ─────────────────────────────────────────────────────────────
# Change this to your WhatsApp number (include country code, no spaces)
DEFAULT_TO_PHONE = "+919876543210"

# The message we want the parent to hear — in Telugu
# "Subhodayam Bujjamma! Epudu ela unnav?" = "Good morning Bujjamma! How are you?"
TELUGU_MESSAGE = "సుభోదయం బుజ్జమ్మా! ఇప్పుడు ఎలా ఉన్నావ్?"

# English version (used as caption and verified against translation)
ENGLISH_MESSAGE = "Good morning Bujjamma! How are you doing today?"

# Buttons to send alongside the audio
TEST_BUTTONS = [
    {"id": "mood_good",  "title": "బాగున్నాను",  "emoji": "😊"},   # "I'm fine"
    {"id": "mood_okay",  "title": "ఓకే ఉన్నాను",  "emoji": "😐"},   # "Okay"
    {"id": "mood_bad",   "title": "బాగా లేదు",    "emoji": "😔"},   # "Not well"
]

# Local path for the generated audio file
AUDIO_DIR  = "/tmp/ayana_audio"
AUDIO_FILE = os.path.join(AUDIO_DIR, "test_telugu.wav")
AUDIO_URL  = f"{settings.APP_URL}/audio/test_telugu.wav"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST STEPS
# ═══════════════════════════════════════════════════════════════════════════════

async def test_translate() -> str:
    """Step 1 — Translate English → Telugu via Sarvam Mayura.

    Returns:
        Translated text (Telugu), or the hardcoded TELUGU_MESSAGE on failure.
    """
    print("\n── Step 1: Sarvam Translate (English → Telugu) ──")
    print(f"   Input:  {ENGLISH_MESSAGE}")

    try:
        translated = await sarvam.translate(ENGLISH_MESSAGE, source_lang="en", target_lang="te")
        if translated:
            print(f"   Output: {translated}")
            return translated
        print("   ⚠ Translation returned empty — using hardcoded Telugu")
    except Exception as e:
        print(f"   ✗ Translation failed: {e}")

    print(f"   Fallback: {TELUGU_MESSAGE}")
    return TELUGU_MESSAGE


async def test_tts(text: str) -> str | None:
    """Step 2 — Generate Telugu TTS audio via Sarvam Bulbul.

    Args:
        text: Telugu text to synthesise.

    Returns:
        Public URL of the saved audio file, or None on failure.
    """
    print("\n── Step 2: Sarvam TTS (Telugu → Audio) ──")
    print(f"   Text:  {text}")
    print(f"   Voice: roopa  |  Lang: te-IN")

    os.makedirs(AUDIO_DIR, exist_ok=True)

    try:
        audio_bytes = await sarvam.text_to_speech(text, language="te", speaker="roopa")
        if not audio_bytes:
            print("   ✗ TTS returned no audio bytes")
            return None

        with open(AUDIO_FILE, "wb") as f:
            f.write(audio_bytes)

        size_kb = len(audio_bytes) / 1024
        print(f"   ✓ Audio saved: {AUDIO_FILE}  ({size_kb:.1f} KB)")
        print(f"   ✓ Public URL:  {AUDIO_URL}")
        return AUDIO_URL

    except Exception as e:
        print(f"   ✗ TTS failed: {e}")
        return None


async def test_whatsapp_send(to_phone: str, audio_url: str | None, telugu_text: str) -> None:
    """Step 3 — Send audio + text + buttons via WhatsApp.

    Args:
        to_phone:    Recipient phone number (E.164).
        audio_url:   URL of the TTS audio, or None to skip audio.
        telugu_text: Translated message text.
    """
    print(f"\n── Step 3: WhatsApp Send → {to_phone} ──")
    print(f"   Provider: {settings.WHATSAPP_PROVIDER.upper()}")

    # ── 3a. Audio ─────────────────────────────────────────────────────────────
    if audio_url:
        print(f"   Sending audio: {audio_url}")
        try:
            ok = await whatsapp.send_audio(to_phone, audio_url)
            print(f"   {'✓' if ok else '✗'} Audio send {'ok' if ok else 'failed'}")
        except Exception as e:
            print(f"   ✗ Audio send error: {e}")
    else:
        print("   ⚠ No audio URL — skipping audio send")

    # ── 3b. Text + buttons ────────────────────────────────────────────────────
    print(f"   Sending text + buttons")
    try:
        ok = await whatsapp.send_message(to_phone, telugu_text, TEST_BUTTONS)
        print(f"   {'✓' if ok else '✗'} Text send {'ok' if ok else 'failed'}")
    except Exception as e:
        print(f"   ✗ Text send error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# PREFLIGHT CHECKS
# ═══════════════════════════════════════════════════════════════════════════════

def preflight() -> bool:
    """Verify required environment variables are set before running.

    Returns:
        True if all required vars are present, False otherwise.
    """
    print("\n── Preflight checks ──")
    required = {
        "SARVAM_API_KEY":    settings.SARVAM_API_KEY,
        "SUPABASE_URL":      settings.SUPABASE_URL,
        "SUPABASE_SERVICE_KEY": settings.SUPABASE_SERVICE_KEY,
    }

    if settings.WHATSAPP_PROVIDER == "twilio":
        required.update({
            "TWILIO_ACCOUNT_SID":    settings.TWILIO_ACCOUNT_SID,
            "TWILIO_AUTH_TOKEN":     settings.TWILIO_AUTH_TOKEN,
            "TWILIO_WHATSAPP_FROM":  settings.TWILIO_WHATSAPP_FROM,
        })
    else:
        required.update({
            "META_WHATSAPP_TOKEN":   settings.META_WHATSAPP_TOKEN,
            "META_PHONE_NUMBER_ID":  settings.META_PHONE_NUMBER_ID,
        })

    all_ok = True
    for name, value in required.items():
        ok = bool(value)
        print(f"   {'✓' if ok else '✗'} {name}: {'set' if ok else 'MISSING'}")
        if not ok:
            all_ok = False

    print(f"   APP_URL: {settings.APP_URL}")
    return all_ok


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    """Run the full test flow."""
    to_phone = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TO_PHONE

    print("=" * 60)
    print("  AYANA Test Flow")
    print(f"  Target: {to_phone}")
    print(f"  Provider: {settings.WHATSAPP_PROVIDER.upper()}")
    print("=" * 60)

    if not preflight():
        print("\n✗ Preflight failed — fix missing env vars and retry.\n")
        sys.exit(1)

    # Run all three steps
    telugu_text = await test_translate()
    audio_url   = await test_tts(telugu_text)
    await test_whatsapp_send(to_phone, audio_url, telugu_text)

    print("\n" + "=" * 60)
    print("  Test complete!")
    if audio_url:
        print(f"  Audio file: {AUDIO_FILE}")
        print(f"  Audio URL:  {AUDIO_URL}")
    print("  Check your WhatsApp — you should hear the Telugu greeting.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
