"""Sarvam AI service — Text-to-Speech, Speech-to-Text, Translation.

All parent-facing messages go through this pipeline:
    English text → translate(target_lang) → tts(translated_text) → audio URL
    
All parent voice notes go through:
    audio bytes → stt(audio) → native text → translate(to English) → structured text
"""

import logging
import base64
import httpx
import tempfile
import os
from app.config import settings

logger = logging.getLogger(__name__)

SARVAM_TIMEOUT = 30
MAX_RETRIES = 3


# ═══════════════ TEXT TO SPEECH ═══════════════

async def text_to_speech(
    text: str,
    language: str = "te",
    speaker: str = "roopa",
) -> bytes | None:
    """Convert text to speech audio using Sarvam Bulbul v3.
    
    Args:
        text: Text in the target language (already translated)
        language: ISO language code (te, hi, ta, etc.)
        speaker: Sarvam speaker voice name
        
    Returns:
        Audio bytes (WAV format) or None on failure.
    """
    sarvam_lang = settings.SARVAM_LANG_MAP.get(language, "te-IN")
    
    for attempt in range(MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=SARVAM_TIMEOUT) as client:
                resp = await client.post(
                    f"{settings.SARVAM_BASE_URL}/text-to-speech",
                    headers={
                        "Content-Type": "application/json",
                        "api-subscription-key": settings.SARVAM_API_KEY,
                    },
                    json={
                        "inputs": [text],
                        "target_language_code": sarvam_lang,
                        "speaker": speaker,
                        "model": "bulbul:v3",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                
                # Sarvam returns base64-encoded audio
                audios = data.get("audios", [])
                if audios:
                    audio_b64 = audios[0]
                    return base64.b64decode(audio_b64)
                    
                logger.warning("Sarvam TTS returned empty audios")
                return None
                
        except httpx.TimeoutException:
            logger.warning(f"Sarvam TTS timeout (attempt {attempt + 1}/{MAX_RETRIES})")
        except httpx.HTTPStatusError as e:
            body = ""
            try:
                body = e.response.text
            except Exception:
                pass
            logger.error(f"Sarvam TTS HTTP {e.response.status_code} (attempt {attempt + 1}): {body}")
            if attempt == MAX_RETRIES - 1:
                return None
        except Exception as e:
            logger.error(f"Sarvam TTS error (attempt {attempt + 1}): {e}")
            if attempt == MAX_RETRIES - 1:
                return None
    
    return None


async def save_tts_audio(
    text: str,
    language: str = "te",
    speaker: str = "roopa",
) -> str | None:
    """Generate TTS and save to Supabase Storage. Returns public URL.

    Uses a content-hash key so identical text+language+speaker combos
    are cached automatically. Falls back to /tmp/ if Supabase Storage
    is not configured or fails.
    """
    import hashlib
    audio_bytes = await text_to_speech(text, language, speaker)
    if not audio_bytes:
        return None

    filename = hashlib.md5(f"{text}{language}{speaker}".encode()).hexdigest()
    file_key = f"tts/{filename}.wav"

    # Try Supabase Storage first (persists across deploys)
    try:
        from app.db import get_db
        db = get_db()
        # Check if already cached
        try:
            existing = db.storage.from_("audio_cache").get_public_url(file_key)
            if existing:
                # Verify it actually exists by trying to download headers
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.head(existing)
                    if resp.status_code == 200:
                        return existing
        except Exception:
            pass

        # Upload to Supabase Storage
        db.storage.from_("audio_cache").upload(
            file_key,
            audio_bytes,
            file_options={"content-type": "audio/wav", "upsert": "true"},
        )
        public_url = db.storage.from_("audio_cache").get_public_url(file_key)
        logger.info(f"TTS audio cached to Supabase: {file_key}")
        return public_url

    except Exception as e:
        logger.warning(f"Supabase Storage upload failed, falling back to /tmp: {e}")

    # Fallback: local /tmp/ (works for dev, resets on deploy)
    os.makedirs("/tmp/ayana_audio", exist_ok=True)
    filepath = f"/tmp/ayana_audio/{filename}.wav"
    with open(filepath, "wb") as f:
        f.write(audio_bytes)
    return f"{settings.APP_URL}/audio/{filename}.wav"


# ═══════════════ SPEECH TO TEXT ═══════════════

async def speech_to_text(
    audio_bytes: bytes,
    language: str = "te",
) -> str | None:
    """Transcribe audio to text using Sarvam Saaras v3.
    
    Args:
        audio_bytes: Raw audio bytes (OGG/WAV)
        language: Expected language (Sarvam auto-detects anyway)
        
    Returns:
        Transcribed text or None on failure.
    """
    sarvam_lang = settings.SARVAM_LANG_MAP.get(language, "te-IN")
    
    for attempt in range(MAX_RETRIES):
        try:
            # Save to temp file for multipart upload
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
                f.write(audio_bytes)
                temp_path = f.name
            
            async with httpx.AsyncClient(timeout=60) as client:
                with open(temp_path, "rb") as f:
                    resp = await client.post(
                        f"{settings.SARVAM_BASE_URL}/speech-to-text",
                        headers={
                            "api-subscription-key": settings.SARVAM_API_KEY,
                        },
                        files={"file": ("voice.ogg", f, "audio/ogg")},
                        data={
                            "language_code": sarvam_lang,
                            "model": "saaras:v2",
                            "with_timestamps": "false",
                        },
                    )
                resp.raise_for_status()
                data = resp.json()
                
                transcript = data.get("transcript", "")
                if transcript:
                    logger.info(f"STT transcript ({language}): {transcript[:100]}...")
                    return transcript
                
                logger.warning("Sarvam STT returned empty transcript")
                return None
                
        except httpx.TimeoutException:
            logger.warning(f"Sarvam STT timeout (attempt {attempt + 1}/{MAX_RETRIES})")
        except Exception as e:
            logger.error(f"Sarvam STT error (attempt {attempt + 1}): {e}")
        finally:
            if 'temp_path' in locals():
                os.unlink(temp_path)
    
    return None


# ═══════════════ TRANSLATION ═══════════════

async def translate(
    text: str,
    source_lang: str = "en",
    target_lang: str = "te",
) -> str | None:
    """Translate text between languages using Sarvam Mayura.
    
    Args:
        text: Text to translate
        source_lang: Source language ISO code
        target_lang: Target language ISO code
        
    Returns:
        Translated text or None on failure. Returns original on same-language.
    """
    if source_lang == target_lang:
        return text
    
    src = settings.SARVAM_LANG_MAP.get(source_lang, "en-IN")
    tgt = settings.SARVAM_LANG_MAP.get(target_lang, "te-IN")
    
    for attempt in range(MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=SARVAM_TIMEOUT) as client:
                resp = await client.post(
                    f"{settings.SARVAM_BASE_URL}/translate",
                    headers={
                        "Content-Type": "application/json",
                        "api-subscription-key": settings.SARVAM_API_KEY,
                    },
                    json={
                        "input": text,
                        "source_language_code": src,
                        "target_language_code": tgt,
                        "mode": "modern-colloquial",
                        "model": "mayura:v1",
                        "enable_preprocessing": True,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                
                translated = data.get("translated_text", "")
                if translated:
                    logger.info(f"Translated ({source_lang}→{target_lang}): {text[:50]} → {translated[:50]}")
                    return translated
                    
                return text  # Fallback to original
                
        except httpx.TimeoutException:
            logger.warning(f"Sarvam translate timeout (attempt {attempt + 1}/{MAX_RETRIES})")
        except Exception as e:
            logger.error(f"Sarvam translate error (attempt {attempt + 1}): {e}")
    
    return text  # Fallback to original text on failure


# ═══════════════ FULL PIPELINE ═══════════════

async def english_to_parent_audio(
    english_text: str,
    parent_language: str = "te",
    parent_voice: str = "roopa",
    parent_nickname: str = "",
) -> tuple[str | None, str | None]:
    """Complete pipeline: English text → translated text → audio URL.
    
    This is the main function used for all parent-facing messages.
    
    Args:
        english_text: Message in English (may contain {nickname} placeholder)
        parent_language: Parent's language code
        parent_voice: Sarvam speaker voice
        parent_nickname: Replace {nickname} in text
        
    Returns:
        (audio_url, translated_text) — audio_url may be None if TTS fails
    """
    # Replace nickname placeholder
    if parent_nickname and "{nickname}" in english_text:
        english_text = english_text.replace("{nickname}", parent_nickname)
    
    # Translate
    translated = await translate(english_text, "en", parent_language)
    if not translated:
        translated = english_text  # Fallback
    
    # Generate audio
    audio_url = await save_tts_audio(translated, parent_language, parent_voice)
    
    return audio_url, translated


async def parent_voice_to_english(
    audio_bytes: bytes,
    parent_language: str = "te",
) -> str | None:
    """Complete pipeline: Parent voice note → English text.
    
    Args:
        audio_bytes: Voice note audio
        parent_language: Parent's language
        
    Returns:
        English text transcription, or None on failure.
    """
    # Transcribe in native language
    native_text = await speech_to_text(audio_bytes, parent_language)
    if not native_text:
        return None
    
    # Translate to English
    english_text = await translate(native_text, parent_language, "en")
    return english_text