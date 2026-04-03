"""ai/sarvam_tts.py — Shim: re-exports from services/sarvam.py."""

from app.services.sarvam import (
    text_to_speech,
    save_tts_audio,
    english_to_parent_audio,
)

__all__ = ["text_to_speech", "save_tts_audio", "english_to_parent_audio"]