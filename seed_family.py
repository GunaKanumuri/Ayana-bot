"""seed_family.py — Add a test family to Supabase and trigger a live check-in.

Usage
─────
    python seed_family.py

The script walks you through:
  1. Child details (your WhatsApp number, name)
  2. Parent details (phone, name, nickname, language, check-in time)
  3. Parent's medicine routine (paste natural language description)
  4. Immediately triggers a test check-in message to the parent

Run this once to set up your first family. Re-running with the same
child phone number will reuse the existing family record.

Requirements
────────────
    .env must have SUPABASE_URL, SUPABASE_SERVICE_KEY, SARVAM_API_KEY,
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, APP_URL.
"""

import asyncio
import sys
from dotenv import load_dotenv

load_dotenv()

from app.db import get_db
from app.config import settings


# ─── Language map ─────────────────────────────────────────────────────────────
LANGUAGES = {
    "1": ("Telugu", "te"),
    "2": ("Hindi", "hi"),
    "3": ("Tamil", "ta"),
    "4": ("Kannada", "kn"),
    "5": ("Malayalam", "ml"),
    "6": ("Bengali", "bn"),
    "7": ("Marathi", "mr"),
    "8": ("Gujarati", "gu"),
    "9": ("Punjabi", "pa"),
    "10": ("English", "en"),
}

DEFAULT_VOICE = {
    "te": "roopa", "hi": "meera", "ta": "pavithra", "kn": "suresh",
    "ml": "aparna", "bn": "ananya", "mr": "sumedha", "gu": "nandita",
    "pa": "suresh", "en": "anushka",
}

TIMING_TO_ANCHOR = {
    "before_food": "before_food", "before_tea": "wake",
    "after_food": "after_food",   "after_breakfast": "after_food",
    "afternoon": "afternoon",     "evening": "evening",
    "dinner": "dinner",           "after_dinner": "after_dinner",
    "night": "night",             "as_needed": "after_food",
}

ANCHOR_DEFAULT_TIME = {
    "wake": "06:30",        "before_food": "08:00",
    "after_food": "09:00",  "afternoon": "13:30",
    "evening": "17:00",     "dinner": "20:00",
    "after_dinner": "21:00","night": "21:30",
}


def _ask(prompt: str, default: str = "") -> str:
    val = input(prompt + (f" [{default}]" if default else "") + ": ").strip()
    return val or default


def _phone(raw: str) -> str:
    """Normalise to E.164 — strips spaces/dashes, preserves all digits."""
    # Keep only digits and a leading +
    cleaned = ""
    for ch in raw.strip():
        if ch == "+" and not cleaned:
            cleaned += ch
        elif ch.isdigit():
            cleaned += ch
    # Add +91 if no country code (bare 10-digit number)
    if not cleaned.startswith("+"):
        cleaned = "+91" + cleaned if len(cleaned) == 10 else "+" + cleaned
    return cleaned


async def main():
    db = get_db()
    print("\n" + "═" * 60)
    print("  AYANA — Seed First Family")
    print("═" * 60)

    # ── 1. Child ──────────────────────────────────────────────────────────────
    print("\n── Your details (the caregiver / child) ──")
    child_phone = _phone(_ask("Your WhatsApp number (e.g. 9876543210)"))
    child_name  = _ask("Your name", "Child")

    # Check if child already exists
    existing_child = (
        db.table("children").select("id, family_id").eq("phone", child_phone).execute().data
    )

    if existing_child:
        child_id  = existing_child[0]["id"]
        family_id = existing_child[0]["family_id"]
        print(f"✓ Found existing child record — reusing family {family_id}")
    else:
        # Create family first
        fam = db.table("families").insert({
            "plan":          "trial",
            "report_format": "combined",
        }).execute().data[0]
        family_id = fam["id"]

        # Create child
        child = db.table("children").insert({
            "family_id":   family_id,
            "phone":       child_phone,
            "name":        child_name,
            "report_time": "20:00",
        }).execute().data[0]
        child_id = child["id"]
        print(f"✓ Created family {family_id} and child record")

    # ── 2. Parent ─────────────────────────────────────────────────────────────
    print("\n── Parent details ──")
    parent_phone    = _phone(_ask("Parent's WhatsApp number"))
    parent_name     = _ask("Parent's full name", "Amma")
    parent_nickname = _ask("Nickname (used in messages)", parent_name)
    checkin_time    = _ask("Check-in time e.g. 08:00 or 07:30", "08:00")

    print("\nLanguage options:")
    for k, (name, code) in LANGUAGES.items():
        print(f"  {k}. {name}")
    lang_choice = _ask("Parent's language (number)", "1")
    lang_name, lang_code = LANGUAGES.get(lang_choice, ("Telugu", "te"))
    tts_voice = DEFAULT_VOICE.get(lang_code, "roopa")
    print(f"✓ Language: {lang_name} ({lang_code}), voice: {tts_voice}")

    # Check if parent already exists
    existing_parent = (
        db.table("parents").select("id").eq("phone", parent_phone).execute().data
    )

    if existing_parent:
        parent_id = existing_parent[0]["id"]
        print(f"✓ Found existing parent record {parent_id}")
    else:
        parent = db.table("parents").insert({
            "family_id":    family_id,
            "phone":        parent_phone,
            "name":         parent_name,
            "nickname":     parent_nickname,
            "language":     lang_code,
            "tts_voice":    tts_voice,
            "checkin_time": checkin_time,
            "is_active":    True,
        }).execute().data[0]
        parent_id = parent["id"]
        print(f"✓ Created parent record {parent_id}")

    # ── 3. Medicine routine ───────────────────────────────────────────────────
    print("\n── Medicine routine (optional) ──")
    print("Describe your parent's routine in plain English.")
    print("Example: She wakes at 6, takes a BP tablet before tea, Metformin after lunch, Atorva at night.")
    print("Press Enter to skip.\n")
    routine_text = input("Routine description: ").strip()

    if routine_text:
        print("⏳ Extracting medicine routine via Gemini...")
        try:
            from app.services.gemini import extract_routine
            from app.services.medicine import setup_medicines_from_routine
            routine = await extract_routine(routine_text, parent_nickname)
            setup_medicines_from_routine(parent_id, routine)
            print(f"✓ Medicines set up — {len(routine.medicines)} found")
        except Exception as e:
            print(f"⚠ Medicine setup failed: {e} (continuing without medicines)")

    # ── 4. Trigger test check-in ──────────────────────────────────────────────
    print("\n── Trigger test check-in? ──")
    trigger = _ask("Send a test check-in to the parent right now? (y/n)", "y")

    if trigger.lower() == "y":
        print(f"⏳ Starting daily conversation for {parent_nickname}...")

        # Load full parent row
        parent_row = (
            db.table("parents")
            .select("*, families(*)")
            .eq("id", parent_id)
            .execute()
            .data[0]
        )

        try:
            from app.services.conversation import start_daily_conversation
            ok = await start_daily_conversation(parent_id)
            if ok:
                print(f"\n✅ Check-in sent to {parent_phone}!")
                print(f"   Tell {parent_nickname} to reply with 1, 2, or 3.")
            else:
                print("⚠ start_daily_conversation returned False — check logs")
        except Exception as e:
            print(f"✗ Check-in failed: {e}")
            import traceback
            traceback.print_exc()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  Setup complete!")
    print(f"  Family ID:  {family_id}")
    print(f"  Child:      {child_name} ({child_phone})")
    print(f"  Parent:     {parent_nickname} ({parent_phone})")
    print(f"  Language:   {lang_name}, voice: {tts_voice}")
    print(f"  Check-in:   {checkin_time} IST daily")
    print("═" * 60)
    print("\nNext steps:")
    print("  1. Make sure APP_URL is publicly reachable (ngrok or Railway)")
    print("  2. Set the Twilio sandbox webhook to: <APP_URL>/webhook")
    print("  3. Have your parent send 1/2/3 in reply — watch the conversation flow")
    print("  4. At 8 PM you'll receive the daily report")
    print()


if __name__ == "__main__":
    asyncio.run(main())