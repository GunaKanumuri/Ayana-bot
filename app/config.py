"""AYANA configuration — all settings from environment."""

import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # Supabase
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "")

    # WhatsApp provider: "twilio" or "meta"
    WHATSAPP_PROVIDER: str = os.getenv("WHATSAPP_PROVIDER", "twilio")

    # Twilio
    TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    TWILIO_WHATSAPP_FROM: str = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    TWILIO_VOICE_PHONE: str = os.getenv("TWILIO_VOICE_PHONE", "")

    # Meta Cloud API
    META_WHATSAPP_TOKEN: str = os.getenv("META_WHATSAPP_TOKEN", "")
    META_PHONE_NUMBER_ID: str = os.getenv("META_PHONE_NUMBER_ID", "")
    META_VERIFY_TOKEN: str = os.getenv("META_VERIFY_TOKEN", "ayana-verify-2026")

    # Sarvam AI
    SARVAM_API_KEY: str = os.getenv("SARVAM_API_KEY", "")
    SARVAM_BASE_URL: str = "https://api.sarvam.ai"

    # Language code to Sarvam language code mapping
    SARVAM_LANG_MAP: dict = {
        "te": "te-IN", "hi": "hi-IN", "ta": "ta-IN",
        "kn": "kn-IN", "ml": "ml-IN", "bn": "bn-IN",
        "mr": "mr-IN", "gu": "gu-IN", "pa": "pa-IN",
        "od": "or-IN", "en": "en-IN",
    }

    # Gemini
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

    # App — backend API URL (Railway)
    APP_URL: str = os.getenv("APP_URL", "http://localhost:8000")

    # Dashboard — Next.js frontend URL (Vercel)
    # Used to build report links embedded in WhatsApp messages.
    # If not set, falls back to APP_URL (works in local dev).
    # Production: set to https://your-dashboard.vercel.app
    DASHBOARD_URL: str = os.getenv("DASHBOARD_URL", "") or os.getenv("APP_URL", "http://localhost:3000")

    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "changethis")
    TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Kolkata")


settings = Settings()