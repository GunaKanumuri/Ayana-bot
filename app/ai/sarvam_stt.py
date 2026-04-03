"""ai/sarvam_stt.py — Shim: re-exports from services/sarvam.py."""

from app.services.sarvam import (
    speech_to_text,
    parent_voice_to_english,
)

__all__ = ["speech_to_text", "parent_voice_to_english"]