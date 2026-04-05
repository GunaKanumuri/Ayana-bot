"""seed_family.py — Add a test family to Supabase and trigger a live check-in.

Usage
─────
    python seed_family.py

The script walks you through:
  1. Child details (your WhatsApp number, name)
  2. Parent details (phone, name, nickname, language, check-in time)
  3. Parent's daily routine (wake time, meals, sleep, alone during day)
  4. Parent's activities and health conditions
  5. Parent's medicine routine (paste natural language description)
  6. Immediately triggers a test check-in message to the parent

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
    "1":  ("Telugu",    "te"),
    "2":  ("Hindi",     "hi"),
    "3":  ("Tamil",     "ta"),
    "4":  ("Kannada",   "kn"),
    "5":  ("Malayalam", "ml"),
    "6":  ("Bengali",   "bn"),
    "7":  ("Marathi",   "mr"),
    "8":  ("Gujarati",  "gu"),
    "9":  ("Punjabi",   "pa"),
    "10": ("English",   "en"),
}

DEFAULT_VOICE = {
    "te": "roopa",   "hi": "meera",    "ta": "pavithra", "kn": "suresh",
    "ml": "aparna",  "bn": "ananya",   "mr": "sumedha",  "gu": "nandita",
    "pa": "suresh",  "en": "anushka",
}

TIMING_TO_ANCHOR = {
    "before_food":    "before_food",
    "before_tea":     "wake",
    "after_food":     "after_food",
    "after_breakfast":"after_food",
    "afternoon":      "afternoon",
    "evening":        "evening",
    "dinner":         "dinner",
    "after_dinner":   "after_dinner",
    "night":          "night",
    "as_needed":      "after_food",
}

ANCHOR_DEFAULT_TIME = {
    "wake":         "06:30",
    "before_food":  "08:00",
    "after_food":   "09:00",
    "afternoon":    "13:30",
    "evening":      "17:00",
    "dinner":       "20:00",
    "after_dinner": "21:00",
    "night":        "21:30",
}

# Common activities to show as suggestions
ACTIVITY_SUGGESTIONS = [
    "morning walk", "yoga", "meditation", "prayer/pooja",
    "reading newspaper", "watching TV", "gardening", "cooking",
    "visiting temple", "socialising with neighbours", "afternoon nap",
]

# Common conditions to show as suggestions
CONDITION_SUGGESTIONS = [
    "diabetes", "hypertension / high BP", "arthritis", "heart condition",
    "thyroid", "asthma", "kidney issues", "Parkinson's", "dementia/memory issues",
]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ask(prompt: str, default: str = "") -> str:
    val = input(prompt + (f" [{default}]" if default else "") + ": ").strip()
    return val or default


def _ask_bool(prompt: str, default: bool = True) -> bool:
    default_str = "y" if default else "n"
    val = _ask(prompt + " (y/n)", default_str)
    return val.lower() in ("y", "yes", "1", "true")


def _phone(raw: str) -> str:
    """Normalise to E.164 — strips spaces/dashes, preserves all digits."""
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


def _parse_list(raw: str) -> list[str]:
    """Split a comma-separated string into a clean list."""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _collect_routine() -> dict:
    """Interactively collect the parent's daily routine."""
    print("\n── Daily routine ──")
    print("This helps AYANA plan check-ins at the right times of day.")
    print()

    wake_time      = _ask("Wake-up time (24h)", "06:30")
    breakfast_time = _ask("Breakfast time (24h)", "08:30")
    lunch_time     = _ask("Lunch time (24h)", "13:00")
    evening_time   = _ask("Evening tea/snack time (24h)", "17:00")
    dinner_time    = _ask("Dinner time (24h)", "20:00")
    sleep_time     = _ask("Sleep time (24h)", "22:00")

    return {
        "wake_time":      wake_time,
        "breakfast_time": breakfast_time,
        "lunch_time":     lunch_time,
        "evening_time":   evening_time,
        "dinner_time":    dinner_time,
        "sleep_time":     sleep_time,
    }


def _collect_activities() -> list[str]:
    """Interactively collect the parent's activities."""
    print("\n── Activities ──")
    print("Suggestions:", ", ".join(ACTIVITY_SUGGESTIONS))
    print("Enter activities as a comma-separated list (or press Enter to skip).")
    raw = input("Activities: ").strip()
    if not raw:
        return []
    return _parse_list(raw)


def _collect_conditions() -> list[str]:
    """Interactively collect the parent's health conditions."""
    print("\n── Health conditions ──")
    print("Suggestions:", ", ".join(CONDITION_SUGGESTIONS))
    print("Enter conditions as a comma-separated list (or press Enter to skip).")
    raw = input("Conditions: ").strip()
    if not raw:
        return []
    return _parse_list(raw)


def _collect_bio(nickname: str) -> str:
    """Collect a short free-text bio about the parent."""
    print(f"\n── About {nickname} ──")
    print("Write a few sentences about your parent — their personality, what they enjoy,")
    print("how they typically communicate, anything that helps AYANA feel personal.")
    print("Example: Amma is warm and talkative. She loves cooking and watching serials.")
    print("         She's a bit stubborn about taking medicines. Always asks about grandkids.")
    print("Press Enter to skip.\n")
    return input("Bio: ").strip()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

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
            "is_primary":  True,
            "report_time": "20:00",
        }).execute().data[0]
        child_id = child["id"]
        print(f"✓ Created family {family_id} and child record")

    # ── 2. Parent basic details ───────────────────────────────────────────────
    print("\n── Parent details ──")
    parent_phone    = _phone(_ask("Parent's WhatsApp number"))
    parent_name     = _ask("Parent's full name", "Amma")
    parent_nickname = _ask("Nickname (used in messages)", parent_name)
    checkin_time    = _ask("Morning check-in time (24h)", "08:00")

    print("\nLanguage options:")
    for k, (name, code) in LANGUAGES.items():
        print(f"  {k}. {name}")
    lang_choice = _ask("Parent's language (number)", "1")
    lang_name, lang_code = LANGUAGES.get(lang_choice, ("Telugu", "te"))
    tts_voice = DEFAULT_VOICE.get(lang_code, "roopa")
    print(f"✓ Language: {lang_name} ({lang_code}), voice: {tts_voice}")

    # ── 3. Rich profile ───────────────────────────────────────────────────────
    alone_during_day = _ask_bool(
        f"\nIs {parent_nickname} alone during the day (no one else at home)?",
        default=True,
    )

    # Daily routine
    routine = _collect_routine()

    # Activities
    activities = _collect_activities()
    if not activities:
        print("  (No activities entered — Gemini will use generic check-ins)")

    # Health conditions
    conditions = _collect_conditions()
    if not conditions:
        print("  (No conditions entered)")

    # Bio
    bio = _collect_bio(parent_nickname)

    # Tone preference
    print("\n── Tone preference ──")
    print("  1. Warm and affectionate (default)")
    print("  2. Cheerful and light")
    print("  3. Calm and gentle")
    tone_choice = _ask("Tone (number)", "1")
    tone_map = {"1": "warm", "2": "cheerful", "3": "calm"}
    tone = tone_map.get(tone_choice, "warm")

    # ── 4. Check if parent already exists ─────────────────────────────────────
    existing_parent = (
        db.table("parents").select("id").eq("phone", parent_phone).execute().data
    )

    if existing_parent:
        parent_id = existing_parent[0]["id"]
        print(f"\n✓ Found existing parent record {parent_id} — updating profile...")
        db.table("parents").update({
            "nickname":        parent_nickname,
            "language":        lang_code,
            "tts_voice":       tts_voice,
            "checkin_time":    checkin_time,
            "alone_during_day": alone_during_day,
            "routine":         routine,
            "activities":      activities,
            "conditions":      conditions,
            "bio":             bio,
            "tone":            tone,
            "is_active":       True,
        }).eq("id", parent_id).execute()
        print(f"✓ Parent profile updated")
    else:
        parent_row = db.table("parents").insert({
            "family_id":       family_id,
            "phone":           parent_phone,
            "name":            parent_name,
            "nickname":        parent_nickname,
            "language":        lang_code,
            "tts_voice":       tts_voice,
            "checkin_time":    checkin_time,
            "alone_during_day": alone_during_day,
            "routine":         routine,
            "activities":      activities,
            "conditions":      conditions,
            "bio":             bio,
            "tone":            tone,
            "is_active":       True,
        }).execute().data[0]
        parent_id = parent_row["id"]
        print(f"✓ Created parent record {parent_id}")

    # ── 5. Medicine routine ───────────────────────────────────────────────────
    print("\n── Medicine routine (optional) ──")
    print("Describe your parent's medicine routine in plain English.")
    print("Example: She takes a BP tablet before tea, Metformin 500mg after breakfast,")
    print("         Atorvastatin at night. Vitamin D on Sundays only.")
    print("Press Enter to skip.\n")
    routine_text = input("Routine description: ").strip()

    if routine_text:
        print("⏳ Extracting medicine routine via Gemini...")
        try:
            from app.services.gemini import extract_routine
            from app.services.medicine import setup_medicines_from_routine
            extracted = await extract_routine(routine_text, parent_nickname)
            setup_medicines_from_routine(parent_id, extracted)
            print(f"✓ Medicines set up — {len(extracted.medicines)} found")
        except Exception as e:
            print(f"⚠ Medicine setup failed: {e} (continuing without medicines)")

    # ── 6. Optional: add siblings ─────────────────────────────────────────────
    print("\n── Add siblings? ──")
    add_sibling = _ask_bool("Do you want to add another sibling who also gets reports?", default=False)
    while add_sibling:
        sib_phone = _phone(_ask("Sibling's WhatsApp number"))
        sib_name  = _ask("Sibling's name")
        try:
            db.table("children").insert({
                "family_id":  family_id,
                "phone":      sib_phone,
                "name":       sib_name,
                "is_primary": False,
                "report_time": "20:00",
            }).execute()
            print(f"✓ Added sibling {sib_name} ({sib_phone})")
        except Exception as e:
            print(f"⚠ Could not add sibling: {e}")
        add_sibling = _ask_bool("Add another sibling?", default=False)

    # ── 7. Trigger test check-in ──────────────────────────────────────────────
    print("\n── Trigger test check-in? ──")
    trigger = _ask_bool(f"Send a test check-in to {parent_nickname} right now?", default=True)

    if trigger:
        print(f"⏳ Starting daily conversation for {parent_nickname}...")

        # Clear any existing conversation_state for today so it replans fresh
        from datetime import date
        today = date.today().isoformat()
        try:
            db.table("conversation_state").delete().eq("parent_id", parent_id).eq("date", today).execute()
            print("✓ Cleared any existing conversation state for today")
        except Exception as e:
            print(f"  (conversation_state clear: {e})")

        try:
            from app.services.conversation import start_daily_conversation
            ok = await start_daily_conversation(parent_id)
            if ok:
                print(f"\n✅ Check-in sent to {parent_phone}!")
                print(f"   Tell {parent_nickname} to reply with 1, 2, or 3.")
                print(f"   (Or send a voice message 🎤)")
            else:
                print("⚠ start_daily_conversation returned False — check Railway logs")
        except Exception as e:
            print(f"✗ Check-in failed: {e}")
            import traceback
            traceback.print_exc()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  Setup complete!")
    print(f"  Family ID:       {family_id}")
    print(f"  Child:           {child_name} ({child_phone})")
    print(f"  Parent:          {parent_nickname} ({parent_phone})")
    print(f"  Language:        {lang_name}, voice: {tts_voice}")
    print(f"  Check-in:        {checkin_time} IST daily")
    print(f"  Alone daytime:   {alone_during_day}")
    print(f"  Activities:      {', '.join(activities) if activities else '—'}")
    print(f"  Conditions:      {', '.join(conditions) if conditions else '—'}")
    print(f"  Routine:         wake {routine['wake_time']}, sleep {routine['sleep_time']}")
    print(f"  Tone:            {tone}")
    print("═" * 60)
    print("\nNext steps:")
    print("  1. Make sure APP_URL is publicly reachable (ngrok or Railway)")
    print("  2. Set the Twilio sandbox webhook to: <APP_URL>/webhook")
    print("  3. Have your parent reply 1/2/3 — watch the conversation flow")
    print("  4. At 8 PM you'll receive the daily report")
    print()


if __name__ == "__main__":
    asyncio.run(main())