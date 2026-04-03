"""Child command handlers — all commands a caregiver child can send to AYANA.

Commands:
  menu / help           → show this command list
  status                → latest check-in status for all parents
  report [name] [Nd]    → health summary (e.g. "report amma 7days")
  ask [name] [text]     → queue a custom question for the next check-in
  pause [name] [Nd]     → pause check-ins while travelling
  resume [name]         → resume paused check-ins
  letter                → write a letter to deliver on a special day
  note                  → send a note in today's check-in
  add parent            → start multi-step parent onboarding flow
  add [phone]           → add a sibling / co-caregiver to the family
  settings              → view current settings

Multi-step flows (add parent, letter, note) use an in-memory state dict keyed
by child phone. For multi-worker deployments, swap _child_state for Redis.
"""

import logging
import re
from datetime import date, timedelta

from fastapi import APIRouter

from app.db import get_db
from app.services.whatsapp import send_message

logger = logging.getLogger(__name__)
router = APIRouter()

# ─── In-memory multi-step flow state ─────────────────────────────────────────
_child_state: dict[str, dict] = {}

# ─── Supported languages ──────────────────────────────────────────────────────
_LANGUAGES: dict[str, str] = {
    "Telugu": "te",
    "Hindi": "hi",
    "Tamil": "ta",
    "Kannada": "kn",
    "Malayalam": "ml",
    "Bengali": "bn",
    "Marathi": "mr",
    "Gujarati": "gu",
    "Punjabi": "pa",
    "English": "en",
}
_LANGUAGE_LIST = list(_LANGUAGES.keys())

_LANGUAGE_MENU = (
    "Which language does your parent speak?\n\n"
    + "\n".join(f"*{i + 1}.* {lang}" for i, lang in enumerate(_LANGUAGE_LIST))
    + "\n\nReply with the number (e.g. *1* for Telugu)"
)

# ─── Default TTS voice per language ──────────────────────────────────────────
_DEFAULT_VOICE: dict[str, str] = {
    "te": "roopa",
    "hi": "meera",
    "ta": "pavithra",
    "kn": "suresh",
    "ml": "aparna",
    "bn": "ananya",
    "mr": "sumedha",
    "gu": "nandita",
    "pa": "suresh",
    "en": "amelia",
}

# ─── Anchor event → default time ─────────────────────────────────────────────
_ANCHOR_DEFAULT_TIME: dict[str, str] = {
    "wake": "06:30",
    "before_food": "08:00",
    "after_food": "09:00",
    "afternoon": "13:30",
    "evening": "17:00",
    "dinner": "20:00",
    "after_dinner": "21:00",
    "night": "21:30",
}

_ANCHOR_LABEL: dict[str, str] = {
    "wake": "Morning (empty stomach)",
    "before_food": "Before food",
    "after_food": "After meals",
    "afternoon": "Afternoon",
    "evening": "Evening",
    "dinner": "With dinner",
    "after_dinner": "After dinner",
    "night": "Night medicines",
}

_TIMING_TO_ANCHOR: dict[str, str] = {
    "before_food": "before_food",
    "before_tea": "wake",
    "after_food": "after_food",
    "after_breakfast": "after_food",
    "afternoon": "afternoon",
    "evening": "evening",
    "dinner": "dinner",
    "after_dinner": "after_dinner",
    "night": "night",
    "as_needed": "after_food",
}

COMMAND_MENU = """\
*AYANA Commands* 🌟

📊 *status*               — Check how your parents are doing today
📋 *report*               — Get a health summary (1-day default)
📋 *report amma 7days*    — 7-day report for a specific parent
❓ *ask amma [question]*  — Queue a question for the next check-in
⏸️  *pause amma 3days*    — Pause check-ins while travelling
▶️  *resume amma*         — Resume paused check-ins
✈️  *travel amma 3days*   — Switch to travel-mode messages
💌 *letter*               — Write a letter to deliver on a special day
📝 *note*                 — Send a note in today's check-in
🎂 *special*              — Add a birthday, anniversary, or festival
📝 *bio*                  — Update a parent's daily routine
➕ *add parent*           — Register a new parent
👥 *add +91XXXXXXXXXX*   — Add a sibling / co-caregiver
⚙️  *settings*             — View your current settings
📖 *menu*                 — Show this list

_Example: ask Amma did you take your BP tablet?_\
"""


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_child_message(child: dict, msg: dict) -> None:
    """Route a child's incoming message to the correct command handler."""
    phone = child["phone"]
    body = (msg.get("body") or "").strip()
    text = body.lower()

    # Ongoing multi-step flow takes priority
    if phone in _child_state:
        await _handle_flow_step(child, msg)
        return

    if text in ("menu", "help", "hi", "hello", "start", ""):
        await _cmd_menu(child)

    elif text == "status":
        await _cmd_status(child)

    elif text.startswith("report"):
        await _cmd_report(child, body)

    elif text.startswith("ask "):
        await _cmd_ask(child, body)

    elif text.startswith("pause"):
        await _cmd_pause(child, body)

    elif text.startswith("resume"):
        await _cmd_resume(child, body)

    elif text.startswith("travel"):
        await _cmd_travel(child, body)

    elif text == "special" or text.startswith("special "):
        await _cmd_special_date_start(child)

    elif text == "bio" or text.startswith("bio "):
        await _cmd_bio_start(child)

    elif text == "letter":
        await _cmd_letter_start(child)

    elif text == "note":
        await _cmd_note_start(child)

    elif text == "add parent":
        await _cmd_add_parent_start(child)

    elif text.startswith("add ") and len(text) > 4:
        token = text[4:].strip()
        if token.lstrip("+").isdigit():
            await _cmd_add_sibling(child, token)
        else:
            await send_message(
                phone,
                "To add a sibling, send: *add +91XXXXXXXXXX*\n"
                "To add a parent, send: *add parent*",
            )

    elif text == "settings":
        await _cmd_settings(child)

    else:
        await send_message(
            phone,
            f"I didn't understand *{body[:40]}*.\n\nSend *menu* to see all commands.",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# SIMPLE COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

async def _cmd_menu(child: dict) -> None:
    await send_message(child["phone"], COMMAND_MENU)


async def _cmd_status(child: dict) -> None:
    """Show today's check-in status for every parent in the family."""
    db = get_db()
    phone = child["phone"]
    family_id = child.get("family_id")

    if not family_id:
        await send_message(phone, "No parents set up yet. Send *add parent* to get started.")
        return

    try:
        parents = (
            db.table("parents")
            .select("id, nickname, is_active")
            .eq("family_id", family_id)
            .eq("is_active", True)
            .execute()
            .data or []
        )

        if not parents:
            await send_message(phone, "No active parents found in your family.")
            return

        today = date.today().isoformat()
        lines = [f"*Status — {today}*\n"]

        for parent in parents:
            pid = parent["id"]
            nick = parent["nickname"]

            checkins = (
                db.table("check_ins")
                .select("status, mood")
                .eq("parent_id", pid)
                .eq("date", today)
                .execute()
                .data or []
            )

            if not checkins:
                lines.append(f"*{nick}* — No check-ins scheduled yet")
                continue

            replied = [c for c in checkins if c["status"] == "replied"]
            moods = [c["mood"] for c in replied if c.get("mood")]
            latest_mood = moods[-1] if moods else None
            mood_emoji = {"good": "😊", "okay": "😐", "not_well": "😔"}.get(
                latest_mood or "", "❓"
            )

            lines.append(
                f"*{nick}* {mood_emoji} — "
                f"{len(replied)}/{len(checkins)} responded"
                + (f" · mood: {latest_mood}" if latest_mood else "")
            )

        await send_message(phone, "\n".join(lines))

    except Exception as e:
        logger.error(f"Status command error for {phone}: {e}", exc_info=True)
        await send_message(phone, "Could not fetch status right now. Please try again.")


async def _cmd_report(child: dict, body: str) -> None:
    """Generate a health summary report.

    Usage: report | report amma | report amma 7days | report 7days
    """
    db = get_db()
    phone = child["phone"]
    family_id = child.get("family_id")

    if not family_id:
        await send_message(phone, "No parents set up yet. Send *add parent* to get started.")
        return

    tokens = body.lower().split()[1:]
    parent_filter: str | None = None
    days = 1

    for tok in tokens:
        days_match = re.match(r"^(\d+)\s*days?$", tok)
        if days_match:
            days = min(int(days_match.group(1)), 30)
        else:
            parent_filter = tok

    try:
        q = (
            db.table("parents")
            .select("id, nickname, name")
            .eq("family_id", family_id)
            .eq("is_active", True)
        )
        if parent_filter:
            q = q.ilike("nickname", f"%{parent_filter}%")

        parents = q.execute().data or []

        if not parents:
            hint = f" matching '{parent_filter}'" if parent_filter else ""
            await send_message(phone, f"No parent found{hint}.")
            return

        since = (date.today() - timedelta(days=days - 1)).isoformat()
        period_label = f"Last {days} day{'s' if days > 1 else ''}"
        report_parts: list[str] = [f"*AYANA Report — {period_label}*\n"]

        for parent in parents:
            pid = parent["id"]
            nick = parent["nickname"]

            checkins = (
                db.table("check_ins")
                .select("date, status, mood, concerns, ai_extraction")
                .eq("parent_id", pid)
                .gte("date", since)
                .order("date", desc=True)
                .execute()
                .data or []
            )

            total = len(checkins)
            replied = sum(1 for c in checkins if c["status"] == "replied")
            moods = [c["mood"] for c in checkins if c.get("mood")]
            mood_counts = {
                "good": moods.count("good"),
                "okay": moods.count("okay"),
                "not_well": moods.count("not_well"),
            }

            all_concerns: list[str] = []
            for c in checkins:
                raw = c.get("concerns") or []
                if isinstance(raw, list):
                    all_concerns.extend(raw)
            unique_concerns = list(dict.fromkeys(all_concerns))[:6]

            block = [
                f"━━━ *{nick}* ━━━",
                f"Response rate: {replied}/{total}",
                f"Mood: 😊 {mood_counts['good']} good  "
                f"😐 {mood_counts['okay']} okay  "
                f"😔 {mood_counts['not_well']} unwell",
            ]
            if unique_concerns:
                block.append(f"Concerns: {', '.join(unique_concerns)}")

            report_parts.append("\n".join(block))

        await send_message(phone, "\n\n".join(report_parts))

    except Exception as e:
        logger.error(f"Report command error for {phone}: {e}", exc_info=True)
        await send_message(phone, "Could not generate report right now. Please try again.")


async def _cmd_ask(child: dict, body: str) -> None:
    """Queue a custom question for a parent's next check-in.

    Usage: ask amma did you take your BP tablet?
    """
    phone = child["phone"]
    family_id = child.get("family_id")

    parts = body.split(None, 2)
    if len(parts) < 3:
        await send_message(
            phone,
            "Usage: *ask [parent name] [question]*\n"
            "Example: _ask Amma did you eat your tiffin?_",
        )
        return

    parent_name_token = parts[1]
    question = parts[2].strip()

    if not family_id:
        await send_message(phone, "No family set up yet. Send *add parent* first.")
        return

    try:
        db = get_db()

        parents = (
            db.table("parents")
            .select("id, nickname")
            .eq("family_id", family_id)
            .ilike("nickname", f"%{parent_name_token}%")
            .execute()
            .data or []
        )

        if not parents:
            await send_message(phone, f"No parent found matching '{parent_name_token}'.")
            return

        parent = parents[0]
        today = date.today().isoformat()

        existing = (
            db.table("conversation_state")
            .select("id, context")
            .eq("parent_id", parent["id"])
            .eq("date", today)
            .execute()
            .data
        )

        entry = {"from": child.get("name", "your child"), "question": question}

        if existing:
            ctx = existing[0].get("context") or {}
            queued = ctx.get("queued_questions", [])
            queued.append(entry)
            ctx["queued_questions"] = queued
            db.table("conversation_state").update({"context": ctx}).eq(
                "id", existing[0]["id"]
            ).execute()
        else:
            db.table("conversation_state").insert(
                {
                    "parent_id": parent["id"],
                    "date": today,
                    "context": {"queued_questions": [entry]},
                }
            ).execute()

        await send_message(
            phone,
            f"Question queued for *{parent['nickname']}* ✓\n"
            f"I'll ask them at the next check-in.",
        )

    except Exception as e:
        logger.error(f"Ask command error for {phone}: {e}", exc_info=True)
        await send_message(phone, "Could not queue the question. Please try again.")


async def _cmd_settings(child: dict) -> None:
    """Show the child's current settings and registered parents."""
    db = get_db()
    phone = child["phone"]
    family_id = child.get("family_id")

    lines = [
        "*Your AYANA Settings*\n",
        f"Name: {child.get('name', '—')}",
        f"Phone: {phone}",
        f"Report time: {child.get('report_time', '20:00')}",
    ]

    if family_id:
        try:
            parents = (
                db.table("parents")
                .select("nickname, language, checkin_time, is_active, paused_until")
                .eq("family_id", family_id)
                .execute()
                .data or []
            )
            if parents:
                lines.append("\n*Registered parents:*")
                for p in parents:
                    status = "active" if p["is_active"] else "paused"
                    pause_note = (
                        f" until {p['paused_until']}"
                        if p.get("paused_until")
                        else ""
                    )
                    lines.append(
                        f"  • {p['nickname']} ({p['language']}) "
                        f"@ {p['checkin_time']} — {status}{pause_note}"
                    )
        except Exception as e:
            logger.error(f"Settings fetch error for {phone}: {e}")

    lines.append("\nTo change settings, contact your AYANA admin.")
    await send_message(phone, "\n".join(lines))


async def _cmd_add_sibling(child: dict, raw_phone: str) -> None:
    """Add a sibling / co-caregiver to the same family."""
    db = get_db()
    phone = child["phone"]
    family_id = child.get("family_id")

    if not family_id:
        await send_message(phone, "No family set up yet. Send *add parent* first.")
        return

    sibling_phone = raw_phone.strip()
    if not sibling_phone.startswith("+"):
        sibling_phone = f"+{sibling_phone}"

    try:
        existing = (
            db.table("children").select("id").eq("phone", sibling_phone).execute().data
        )
        if existing:
            await send_message(phone, f"{sibling_phone} is already registered in AYANA.")
            return

        db.table("children").insert(
            {
                "family_id": family_id,
                "phone": sibling_phone,
                "name": "Family Member",
                "is_primary": False,
            }
        ).execute()

        await send_message(
            phone,
            f"Added *{sibling_phone}* to your family ✓\n"
            f"They can now send commands to AYANA.",
        )
        await send_message(
            sibling_phone,
            f"You've been added to AYANA by *{child.get('name', 'a family member')}*.\n\n"
            f"Send *menu* to see available commands.",
        )

    except Exception as e:
        logger.error(f"Add sibling error for {phone}: {e}", exc_info=True)
        await send_message(phone, f"Could not add {sibling_phone}. Please try again.")


# ═══════════════════════════════════════════════════════════════════════════════
# PAUSE / RESUME
# ═══════════════════════════════════════════════════════════════════════════════

async def _cmd_pause(child: dict, body: str) -> None:
    """Pause a parent's check-ins for N days.

    Usage: pause | pause amma | pause amma 3days | pause amma 7
    """
    db = get_db()
    phone = child["phone"]
    family_id = child.get("family_id")

    if not family_id:
        await send_message(phone, "No parents set up yet.")
        return

    tokens = body.lower().split()[1:]  # drop "pause"
    parent_filter = None
    days = 1

    for tok in tokens:
        m = re.match(r"^(\d+)\s*days?$", tok)
        if m:
            days = min(int(m.group(1)), 30)
        elif tok.isdigit():
            days = min(int(tok), 30)
        else:
            parent_filter = tok

    until_date = (date.today() + timedelta(days=days)).isoformat()

    try:
        q = db.table("parents").select("id, nickname").eq("family_id", family_id)
        if parent_filter:
            q = q.ilike("nickname", f"%{parent_filter}%")
        parents = q.execute().data or []

        if not parents:
            hint = f" matching '{parent_filter}'" if parent_filter else ""
            await send_message(phone, f"No parent found{hint}.")
            return

        paused_names = []
        for p in parents:
            db.table("parents").update({"paused_until": until_date}).eq(
                "id", p["id"]
            ).execute()
            paused_names.append(p["nickname"])

        names = " and ".join(paused_names)
        await send_message(
            phone,
            f"⏸️ Paused check-ins for *{names}* until *{until_date}* "
            f"({days} day{'s' if days > 1 else ''}).\n\n"
            f"Send *resume {paused_names[0].lower()}* to restart early.",
        )

    except Exception as e:
        logger.error(f"Pause command error for {phone}: {e}", exc_info=True)
        await send_message(phone, "Could not pause. Please try again.")


async def _cmd_resume(child: dict, body: str) -> None:
    """Resume a paused parent's check-ins immediately.

    Usage: resume | resume amma
    """
    db = get_db()
    phone = child["phone"]
    family_id = child.get("family_id")

    if not family_id:
        await send_message(phone, "No parents set up yet.")
        return

    tokens = body.lower().split()[1:]
    parent_filter = tokens[0] if tokens else None

    try:
        q = db.table("parents").select("id, nickname").eq("family_id", family_id)
        if parent_filter:
            q = q.ilike("nickname", f"%{parent_filter}%")
        parents = q.execute().data or []

        if not parents:
            hint = f" matching '{parent_filter}'" if parent_filter else ""
            await send_message(phone, f"No parent found{hint}.")
            return

        resumed = []
        for p in parents:
            db.table("parents").update({"paused_until": None}).eq(
                "id", p["id"]
            ).execute()
            resumed.append(p["nickname"])

        names = " and ".join(resumed)
        await send_message(
            phone,
            f"▶️ Resumed check-ins for *{names}*.\n"
            f"They'll get the next check-in at their scheduled time.",
        )

    except Exception as e:
        logger.error(f"Resume command error for {phone}: {e}", exc_info=True)
        await send_message(phone, "Could not resume. Please try again.")


# ═══════════════════════════════════════════════════════════════════════════════
# TRAVEL MODE
# ═══════════════════════════════════════════════════════════════════════════════

async def _cmd_travel(child: dict, body: str) -> None:
    """Mark a parent as travelling — switches to travel-mode messages.

    Usage: travel | travel amma | travel amma 3days
    Travel mode adjusts touchpoints: "Safe journey!", "Did you reach?", etc.
    """
    db = get_db()
    phone = child["phone"]
    family_id = child.get("family_id")

    if not family_id:
        await send_message(phone, "No parents set up yet.")
        return

    tokens = body.lower().split()[1:]
    parent_filter = None
    days = 1

    for tok in tokens:
        m = re.match(r"^(\d+)\s*days?$", tok)
        if m:
            days = min(int(m.group(1)), 14)
        elif tok.isdigit():
            days = min(int(tok), 14)
        else:
            parent_filter = tok

    try:
        q = db.table("parents").select("id, nickname").eq("family_id", family_id)
        if parent_filter:
            q = q.ilike("nickname", f"%{parent_filter}%")
        parents = q.execute().data or []

        if not parents:
            await send_message(phone, f"No parent found{f' matching {parent_filter}' if parent_filter else ''}.")
            return

        until_date = (date.today() + timedelta(days=days)).isoformat()
        travel_names = []
        for p in parents:
            # Store travel mode in parent routine JSON
            current_routine = db.table("parents").select("routine").eq("id", p["id"]).execute().data
            routine = (current_routine[0].get("routine") or {}) if current_routine else {}
            routine["travel_mode"] = True
            routine["travel_until"] = until_date
            db.table("parents").update({"routine": routine}).eq("id", p["id"]).execute()
            travel_names.append(p["nickname"])

        names = " and ".join(travel_names)
        await send_message(
            phone,
            f"✈️ Travel mode activated for *{names}* until *{until_date}*.\n\n"
            f"I'll adjust messages to travel check-ins: safe journey, did you reach, etc.\n\n"
            f"Send *resume {travel_names[0].lower()}* to switch back early.",
        )

    except Exception as e:
        logger.error(f"Travel command error for {phone}: {e}", exc_info=True)
        await send_message(phone, "Could not set travel mode. Please try again.")


# ═══════════════════════════════════════════════════════════════════════════════
# SPECIAL DATES WIZARD
# ═══════════════════════════════════════════════════════════════════════════════

async def _cmd_special_date_start(child: dict) -> None:
    """Start the special date wizard — add birthdays, anniversaries, etc."""
    phone = child["phone"]
    _child_state[phone] = {
        "flow": "special_date",
        "step": "waiting_recipient",
        "data": {},
    }
    await send_message(
        phone,
        "🎂 *Special date wizard*\n\n"
        "Whose special date is this?\n\n"
        "Reply: *Amma*, *Nanna*, or *Both*\n\n"
        "_Send *cancel* to stop._",
    )


async def _special_date_flow(child: dict, body: str, state: dict) -> None:
    """Drive the special date wizard forward."""
    phone = child["phone"]
    step = state["step"]
    data = state["data"]

    if step == "waiting_recipient":
        body_l = body.lower()
        if any(w in body_l for w in ("amma", "mom", "mother")):
            data["recipient"] = "amma"
        elif any(w in body_l for w in ("nanna", "dad", "father", "appa")):
            data["recipient"] = "nanna"
        elif "both" in body_l:
            data["recipient"] = "both"
        else:
            await send_message(phone, "Please reply with *Amma*, *Nanna*, or *Both*.")
            return

        state["step"] = "waiting_type"
        await send_message(
            phone,
            f"What type of special date?\n\n"
            f"1️⃣ Birthday\n2️⃣ Anniversary\n3️⃣ Festival\n4️⃣ Custom",
        )

    elif step == "waiting_type":
        type_map = {"1": "birthday", "birthday": "birthday", "2": "anniversary", "anniversary": "anniversary",
                     "3": "festival", "festival": "festival", "4": "custom", "custom": "custom"}
        date_type = type_map.get(body.lower().strip())
        if not date_type:
            await send_message(phone, "Reply with *1*, *2*, *3*, or *4*.")
            return
        data["date_type"] = date_type
        state["step"] = "waiting_date"
        await send_message(phone, "What date? Reply in *DD-MM* format (e.g. *15-08*)")

    elif step == "waiting_date":
        m = re.match(r"^(\d{1,2})[/-](\d{1,2})$", body.strip())
        if not m:
            await send_message(phone, "Please use DD-MM format (e.g. *25-12*).")
            return
        day_n, month_n = int(m.group(1)), int(m.group(2))
        try:
            date(2024, month_n, day_n)  # validate
        except ValueError:
            await send_message(phone, "Invalid date. Try again (e.g. *25-12*).")
            return
        data["day"] = day_n
        data["month"] = month_n
        state["step"] = "waiting_label"
        await send_message(phone, "Give this date a name (e.g. *Amma's birthday*, *Wedding anniversary*)")

    elif step == "waiting_label":
        if len(body.strip()) < 3:
            await send_message(phone, "Please enter a short name for this date.")
            return
        data["label"] = body.strip()

        # Save to DB
        try:
            db = get_db()
            family_id = child.get("family_id")
            parents = db.table("parents").select("id, nickname").eq("family_id", family_id).eq("is_active", True).execute().data or []

            if data["recipient"] != "both":
                parents = [p for p in parents if data["recipient"] in p["nickname"].lower()]

            year = date.today().year
            date_value = date(year, data["month"], data["day"]).isoformat()

            for p in parents:
                db.table("special_dates").insert({
                    "parent_id": p["id"],
                    "date_type": data["date_type"],
                    "label": data["label"],
                    "date_value": date_value,
                    "recurring": True,
                }).execute()

            names = ", ".join(p["nickname"] for p in parents)
            await send_message(
                phone,
                f"✅ Special date saved!\n\n"
                f"*{data['label']}* on *{data['day']:02d}-{data['month']:02d}* for *{names}*.\n\n"
                f"I'll send a special message on that day.",
            )
        except Exception as e:
            logger.error(f"Special date save failed: {e}", exc_info=True)
            await send_message(phone, "Could not save the date. Please try again.")

        if phone in _child_state:
            del _child_state[phone]


# ═══════════════════════════════════════════════════════════════════════════════
# BIO / ROUTINE EDITOR
# ═══════════════════════════════════════════════════════════════════════════════

async def _cmd_bio_start(child: dict) -> None:
    """Edit a parent's daily routine / bio post-onboarding."""
    phone = child["phone"]
    _child_state[phone] = {
        "flow": "bio_edit",
        "step": "waiting_parent",
        "data": {},
    }
    await send_message(
        phone,
        "📝 *Edit parent routine*\n\n"
        "Which parent's routine do you want to update?\n\n"
        "Reply: *Amma*, *Nanna*, or the parent's name.\n\n"
        "_Send *cancel* to stop._",
    )


async def _bio_edit_flow(child: dict, body: str, state: dict) -> None:
    """Drive the bio/routine editor."""
    phone = child["phone"]
    step = state["step"]
    data = state["data"]
    db = get_db()
    family_id = child.get("family_id")

    if step == "waiting_parent":
        parents = db.table("parents").select("id, nickname").eq("family_id", family_id).eq("is_active", True).execute().data or []
        matched = [p for p in parents if body.lower() in p["nickname"].lower()]
        if not matched:
            await send_message(phone, f"No parent found matching '{body}'. Try again.")
            return
        data["parent_id"] = matched[0]["id"]
        data["parent_name"] = matched[0]["nickname"]
        state["step"] = "waiting_description"
        await send_message(
            phone,
            f"Tell me about *{matched[0]['nickname']}'s* updated daily routine:\n\n"
            f"• Wake time, activities, meal times\n"
            f"• Any new medicines or conditions\n"
            f"• Hobbies, interests, daily schedule\n\n"
            f"_Write naturally — I'll extract the details._",
        )

    elif step == "waiting_description":
        if len(body.strip()) < 20:
            await send_message(phone, "Please share a bit more detail about their routine.")
            return

        try:
            from app.services.gemini import extract_routine
            routine = await extract_routine(body, data["parent_name"])

            parent_id = data["parent_id"]
            update_data = {
                "activities": routine.activities,
                "conditions": routine.conditions,
                "alone_during_day": routine.alone_during_day,
                "routine": {
                    "wake_time": routine.wake_time,
                    "notes": routine.notes,
                },
            }
            db.table("parents").update(update_data).eq("id", parent_id).execute()

            # Update medicines if new ones mentioned
            if routine.medicines:
                _create_medicine_groups(db, parent_id, routine.medicines)

            summary_parts = []
            if routine.activities:
                summary_parts.append(f"Activities: {', '.join(routine.activities[:5])}")
            if routine.conditions:
                summary_parts.append(f"Conditions: {', '.join(routine.conditions[:5])}")
            if routine.medicines:
                summary_parts.append(f"Medicines: {len(routine.medicines)} found")

            await send_message(
                phone,
                f"✅ Updated *{data['parent_name']}'s* routine!\n\n"
                + "\n".join(f"• {s}" for s in summary_parts)
                + "\n\nThis will be reflected in tomorrow's check-in messages.",
            )

        except Exception as e:
            logger.error(f"Bio edit failed: {e}", exc_info=True)
            await send_message(phone, "Could not update. Please try again.")

        if phone in _child_state:
            del _child_state[phone]


# ═══════════════════════════════════════════════════════════════════════════════
# LETTER / NOTE WIZARDS
# ═══════════════════════════════════════════════════════════════════════════════

async def _cmd_letter_start(child: dict) -> None:
    """Start the letter wizard — write a letter for a special date."""
    phone = child["phone"]
    _child_state[phone] = {
        "flow": "letter",
        "step": "waiting_recipient",
        "data": {},
    }
    await send_message(
        phone,
        "💌 *Letter wizard*\n\n"
        "Who is this letter for?\n\n"
        "Reply: *Amma*, *Nanna*, or *Both*\n\n"
        "_Send *cancel* to stop._",
    )


async def _cmd_note_start(child: dict) -> None:
    """Start the note wizard — send a note in today's check-in."""
    phone = child["phone"]
    _child_state[phone] = {
        "flow": "note",
        "step": "waiting_recipient",
        "data": {"is_note": True},
    }
    await send_message(
        phone,
        "📝 *Quick note*\n\nWho is this note for?\n\n"
        "Reply: *Amma*, *Nanna*, or *Both*\n\n"
        "_Send *cancel* to stop._",
    )


async def _letter_note_flow(child: dict, body: str, state: dict) -> None:
    """Drive the letter and note wizards forward one step."""
    phone = child["phone"]
    step = state["step"]
    data = state["data"]
    is_note = data.get("is_note", False)

    # ── Step 1: recipient ─────────────────────────────────────
    if step == "waiting_recipient":
        body_l = body.lower()
        if any(w in body_l for w in ("amma", "mom", "mother", "maa")):
            data["recipient"] = "amma"
            label = "Amma"
        elif any(w in body_l for w in ("nanna", "dad", "father", "appa")):
            data["recipient"] = "nanna"
            label = "Nanna"
        elif "both" in body_l:
            data["recipient"] = "both"
            label = "both"
        else:
            await send_message(phone, "Please reply with *Amma*, *Nanna*, or *Both*.")
            return

        data["recipient_label"] = label
        state["step"] = "waiting_delivery"

        if is_note:
            await send_message(
                phone,
                f"Got it — note for *{label}* ✓\n\n"
                f"When should I deliver it?\n\n"
                f"1️⃣ Morning check-in\n2️⃣ Evening check-in\n3️⃣ Now",
            )
        else:
            await send_message(
                phone,
                f"Got it — letter for *{label}* ✓\n\n"
                f"When should I deliver it?\n\n"
                f"1️⃣ Birthday\n2️⃣ Anniversary\n3️⃣ Custom date (reply with DD-MM)\n4️⃣ Send now",
            )

    # ── Step 2: delivery timing ───────────────────────────────
    elif step == "waiting_delivery":
        body_l = body.lower().strip()

        if is_note:
            slot_map = {
                "1": "morning_greeting",
                "morning": "morning_greeting",
                "2": "evening_checkin",
                "evening": "evening_checkin",
                "3": "now",
                "now": "now",
            }
            slot = slot_map.get(body_l, "now")
            data["delivery_slot"] = slot
            state["step"] = "waiting_content"
            slot_label = {
                "morning_greeting": "morning",
                "evening_checkin": "evening",
                "now": "immediately",
            }.get(slot, slot)
            await send_message(
                phone,
                f"Delivering *{slot_label}* ✓\n\n"
                f"Write your note for {data['recipient_label']}:",
            )

        else:
            if body_l in ("1", "birthday"):
                data["deliver_type"] = "birthday"
                data["deliver_date"] = "birthday"
            elif body_l in ("2", "anniversary"):
                data["deliver_type"] = "anniversary"
                data["deliver_date"] = "anniversary"
            elif body_l in ("4", "now", "send now"):
                data["deliver_type"] = "now"
                data["deliver_date"] = date.today().isoformat()
            else:
                m = re.match(r"^(\d{1,2})[/-](\d{1,2})$", body.strip())
                if m:
                    day_n, month_n = int(m.group(1)), int(m.group(2))
                    year = date.today().year
                    try:
                        d = date(year, month_n, day_n)
                        if d < date.today():
                            d = date(year + 1, month_n, day_n)
                        data["deliver_type"] = "custom"
                        data["deliver_date"] = d.isoformat()
                    except ValueError:
                        await send_message(phone, "That date doesn't look valid. Try *15-08* format.")
                        return
                else:
                    await send_message(
                        phone,
                        "Please reply *1* (Birthday), *2* (Anniversary), "
                        "*4* (Send now), or a date like *15-08*.",
                    )
                    return

            state["step"] = "waiting_content"
            date_label = data.get("deliver_date", "the chosen date")
            await send_message(
                phone,
                f"Perfect — delivering on *{date_label}* ✓\n\n"
                f"Write your letter for {data['recipient_label']}:\n\n"
                f"_Write freely — I'll preserve your words exactly._",
            )

    # ── Step 3: content ───────────────────────────────────────
    elif step == "waiting_content":
        if len(body.strip()) < 5:
            await send_message(phone, "Please write something for your parent. Even a few words.")
            return

        data["content"] = body.strip()
        state["step"] = "confirming"

        preview = body.strip()[:120] + ("..." if len(body.strip()) > 120 else "")
        await send_message(
            phone,
            f"*Preview:*\n\n_{preview}_\n\n"
            f"To: *{data['recipient_label']}*\n"
            f"Deliver: *{data.get('deliver_date') or data.get('delivery_slot', 'now')}*\n\n"
            f"Reply *YES* to send or *EDIT* to rewrite.",
        )

    # ── Step 4: confirm ───────────────────────────────────────
    elif step == "confirming":
        body_l = body.lower()
        if body_l in ("edit", "rewrite", "change"):
            state["step"] = "waiting_content"
            await send_message(phone, "Write it again:")
        elif body_l in ("yes", "y", "send", "ok", "confirm"):
            await _save_and_deliver_letter(child, data)
            if phone in _child_state:
                del _child_state[phone]
        else:
            await send_message(phone, "Reply *YES* to send or *EDIT* to rewrite.")


async def _save_and_deliver_letter(child: dict, data: dict) -> None:
    """Save the letter/note to DB and deliver immediately if requested."""
    phone = child["phone"]
    family_id = child.get("family_id")
    db = get_db()
    is_note = data.get("is_note", False)

    if not family_id:
        await send_message(phone, "No family found. Cannot save.")
        return

    try:
        recipient = data.get("recipient", "both")
        parents = (
            db.table("parents")
            .select("id, nickname, phone, language, tts_voice")
            .eq("family_id", family_id)
            .eq("is_active", True)
            .execute()
            .data or []
        )

        if recipient != "both":
            parents = [p for p in parents if recipient in p["nickname"].lower()]

        if not parents:
            await send_message(phone, "Could not find the parent to deliver to.")
            return

        deliver_now = (
            data.get("deliver_type") == "now"
            or data.get("delivery_slot") == "now"
        )
        child_name = child.get("name", "your child")

        for parent in parents:
            db.table("letters").insert({
                "family_id":      family_id,
                "from_child_id":  child["id"],
                "to_parent_id":   parent["id"],
                "content":        data["content"],
                "deliver_date":   data.get("deliver_date") or date.today().isoformat(),
                "deliver_slot":   data.get("delivery_slot", "morning_greeting"),
                "letter_type":    "note" if is_note else "letter",
                "status":         "delivered" if deliver_now else "pending",
            }).execute()

            if deliver_now:
                from app.services import sarvam, whatsapp
                msg_en = f"{child_name} sent this for you:\n\n{data['content']}"
                try:
                    audio_url, translated = await sarvam.english_to_parent_audio(
                        msg_en, parent["language"], parent["tts_voice"], parent["nickname"]
                    )
                    await whatsapp.send_audio_and_buttons(
                        to=parent["phone"],
                        audio_url=audio_url or "",
                        text=translated or msg_en,
                    )
                except Exception as e:
                    logger.error(f"Letter audio delivery failed: {e}")
                    from app.services.whatsapp import send_message as _send
                    await _send(parent["phone"], f"{child_name} sent this for you:\n\n{data['content']}")

        item_type = "note" if is_note else "letter"
        delivered_label = (
            "delivered now"
            if deliver_now
            else f"scheduled for {data.get('deliver_date', 'the chosen date')}"
        )
        await send_message(
            phone,
            f"✅ {item_type.title()} {delivered_label} for "
            f"*{', '.join(p['nickname'] for p in parents)}*.",
        )

    except Exception as e:
        logger.error(f"Letter save failed for {phone}: {e}", exc_info=True)
        if "letters" in str(e).lower():
            await send_message(
                phone,
                "⚠️ Letters table not set up yet. Run supabase_letters.sql first.\n\n"
                f"Your message: _{data.get('content', '')[:300]}_",
            )
        else:
            await send_message(phone, "Could not save. Please try again.")


# ═══════════════════════════════════════════════════════════════════════════════
# MULTI-STEP FLOW ROUTER
# ═══════════════════════════════════════════════════════════════════════════════

async def _handle_flow_step(child: dict, msg: dict) -> None:
    """Continue an active multi-step flow. Handles universal cancel."""
    phone = child["phone"]
    state = _child_state.get(phone)
    if not state:
        return

    body = (msg.get("body") or "").strip()

    if body.lower() in ("cancel", "quit", "exit", "stop"):
        del _child_state[phone]
        await send_message(phone, "Cancelled. Send *menu* for available commands.")
        return

    flow = state.get("flow")
    if flow == "add_parent":
        await _add_parent_flow(child, body, state)
    elif flow in ("letter", "note"):
        await _letter_note_flow(child, body, state)
    elif flow == "special_date":
        await _special_date_flow(child, body, state)
    elif flow == "bio_edit":
        await _bio_edit_flow(child, body, state)


# ═══════════════════════════════════════════════════════════════════════════════
# ADD PARENT — MULTI-STEP ONBOARDING FLOW
# ═══════════════════════════════════════════════════════════════════════════════

async def _cmd_add_parent_start(child: dict) -> None:
    """Start the add-parent onboarding flow."""
    phone = child["phone"]
    _child_state[phone] = {"flow": "add_parent", "step": "waiting_name", "data": {}}

    await send_message(
        phone,
        "Let's add your parent to AYANA.\n\n"
        "*What is your parent's name?*\n"
        "_(The name you call them — Amma, Nanna, Appa, etc.)_\n\n"
        "Send *cancel* at any time to stop.",
    )


async def _add_parent_flow(child: dict, body: str, state: dict) -> None:
    """Drive the add-parent conversation forward one step."""
    phone = child["phone"]
    step = state["step"]
    data = state["data"]

    if step == "waiting_name":
        if len(body) < 2:
            await send_message(phone, "Please enter a valid name (e.g. Amma, Nanna).")
            return

        data["name"] = body
        data["nickname"] = body
        state["step"] = "waiting_phone"

        await send_message(
            phone,
            f"Got it — *{body}*.\n\n"
            f"What is {body}'s WhatsApp phone number?\n"
            f"Include country code (e.g. *+919876543210*)",
        )

    elif step == "waiting_phone":
        p = body.strip()
        if not p.startswith("+"):
            p = f"+{p}"
        if not re.match(r"^\+\d{8,15}$", p):
            await send_message(
                phone,
                "Please enter a valid phone number with country code.\n"
                "Example: *+919876543210*",
            )
            return

        db = get_db()
        if db.table("parents").select("id").eq("phone", p).execute().data:
            await send_message(phone, f"{p} is already registered as a parent in AYANA.")
            del _child_state[phone]
            return

        data["phone"] = p
        state["step"] = "waiting_language"
        await send_message(phone, _LANGUAGE_MENU)

    elif step == "waiting_language":
        try:
            idx = int(body.strip()) - 1
            if 0 <= idx < len(_LANGUAGE_LIST):
                lang_name = _LANGUAGE_LIST[idx]
                data["language"] = _LANGUAGES[lang_name]
                data["language_name"] = lang_name
                state["step"] = "waiting_time"
                await send_message(
                    phone,
                    f"Great — *{lang_name}* ✓\n\n"
                    f"What time should I send the morning greeting?\n"
                    f"Reply in 24h format (e.g. *08:00* for 8 AM, *07:30* for 7:30 AM)",
                )
            else:
                await send_message(
                    phone,
                    f"Please reply with a number between 1 and {len(_LANGUAGE_LIST)}.",
                )
        except ValueError:
            await send_message(phone, "Please reply with a number (e.g. *1* for Telugu).")

    elif step == "waiting_time":
        m = re.match(r"^(\d{1,2}):(\d{2})$", body.strip())
        if not m:
            await send_message(
                phone,
                "Please enter time in HH:MM format.\n"
                "Examples: *08:00*  *07:30*  *09:00*",
            )
            return

        hour, minute = int(m.group(1)), int(m.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            await send_message(phone, "Invalid time. Please use HH:MM (e.g. *08:00*).")
            return

        data["checkin_time"] = f"{hour:02d}:{minute:02d}"
        state["step"] = "waiting_routine"

        await send_message(
            phone,
            f"Almost done!\n\n"
            f"Tell me about *{data['name']}'s daily routine* in a few sentences:\n\n"
            f"• When do they wake up?\n"
            f"• What medicines do they take (and when)?\n"
            f"• Daily activities (walk, temple, garden, etc.)?\n"
            f"• Any health conditions (BP, diabetes, etc.)?\n"
            f"• Are they home alone during the day?\n\n"
            f"_Write naturally — I'll figure out the details._",
        )

    elif step == "waiting_routine":
        if len(body) < 20:
            await send_message(
                phone,
                "Please share a little more — just a few sentences about "
                "their daily routine and medicines.",
            )
            return

        data["routine_description"] = body
        state["step"] = "confirming"

        summary = (
            f"*Here's what I'll set up:*\n\n"
            f"Parent name: *{data['name']}*\n"
            f"Phone: *{data['phone']}*\n"
            f"Language: *{data['language_name']}*\n"
            f"Morning check-in: *{data['checkin_time']}*\n\n"
            f"I'll also extract medicines and activities from the routine you described.\n\n"
            f"Reply *YES* to confirm or *NO* to cancel."
        )
        await send_message(phone, summary)

    elif step == "confirming":
        if body.lower() in ("yes", "y", "confirm", "ok", "okay"):
            await _create_parent_records(child, data)
        elif body.lower() in ("no", "n", "cancel"):
            del _child_state[phone]
            await send_message(phone, "Cancelled. Send *add parent* to try again.")
        else:
            await send_message(phone, "Reply *YES* to confirm or *NO* to cancel.")


# ═══════════════════════════════════════════════════════════════════════════════
# RECORD CREATION
# ═══════════════════════════════════════════════════════════════════════════════

async def _create_parent_records(child: dict, data: dict) -> None:
    """Create all Supabase records for a new parent after onboarding."""
    phone = child["phone"]
    db = get_db()

    try:
        family_id = child.get("family_id")

        if not family_id:
            fam = db.table("families").insert({"plan": "trial"}).execute()
            family_id = fam.data[0]["id"]
            db.table("children").update({"family_id": family_id}).eq(
                "id", child["id"]
            ).execute()
            logger.info(f"Created family {family_id} for child {phone}")

        voice = _DEFAULT_VOICE.get(data["language"], "roopa")
        parent_resp = db.table("parents").insert(
            {
                "family_id":    family_id,
                "phone":        data["phone"],
                "name":         data["name"],
                "nickname":     data["nickname"],
                "language":     data["language"],
                "tts_voice":    voice,
                "checkin_time": data["checkin_time"],
            }
        ).execute()
        parent_id = parent_resp.data[0]["id"]
        logger.info(f"Created parent {parent_id} ({data['name']}) for family {family_id}")

        routine_desc = data.get("routine_description", "")
        if routine_desc:
            try:
                from app.services.gemini import extract_routine
                routine = await extract_routine(routine_desc, data["name"])

                db.table("parents").update(
                    {
                        "activities":     routine.activities,
                        "conditions":     routine.conditions,
                        "alone_during_day": routine.alone_during_day,
                        "routine": {
                            "wake_time": routine.wake_time,
                            "notes":     routine.notes,
                        },
                    }
                ).eq("id", parent_id).execute()

                if routine.medicines:
                    _create_medicine_groups(db, parent_id, routine.medicines)

                logger.info(
                    f"Routine extracted for parent {parent_id}: {len(routine.medicines)} med(s)"
                )

            except Exception as e:
                logger.error(
                    f"Routine extraction failed for parent {parent_id}: {e}",
                    exc_info=True,
                )

        await send_message(
            phone,
            f"*{data['name']} is now set up in AYANA!* ✅\n\n"
            f"I'll send them a morning greeting at *{data['checkin_time']}* every day.\n\n"
            f"*Important:* Ask {data['name']} to save this WhatsApp number "
            f"and send any message to me first — this is required to receive messages "
            f"(Twilio sandbox requirement).",
        )

        await send_message(
            data["phone"],
            f"Namaste! I'm *AYANA*, your daily care companion 🙏\n\n"
            f"Your family has set me up to check in with you each day.\n"
            f"I'll say good morning at *{data['checkin_time']}* every day — "
            f"in *{data['language_name']}*.\n\n"
            f"Looking forward to our daily chats!",
        )

    except Exception as e:
        logger.error(f"Parent record creation failed for {phone}: {e}", exc_info=True)
        await send_message(
            phone,
            "Something went wrong while setting up the parent. Please try again.",
        )

    finally:
        if phone in _child_state:
            del _child_state[phone]


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _create_medicine_groups(db, parent_id: str, medicines: list[dict]) -> None:
    """Create medicine_groups and medicines from Gemini routine extraction."""
    groups: dict[str, list[dict]] = {}
    for med in medicines:
        timing = med.get("timing", "after_food")
        anchor = _TIMING_TO_ANCHOR.get(timing, "after_food")
        groups.setdefault(anchor, []).append(med)

    for sort_order, (anchor, meds) in enumerate(groups.items()):
        try:
            raw_time = meds[0].get(
                "time_estimate", _ANCHOR_DEFAULT_TIME.get(anchor, "08:00")
            )
            if not re.match(r"^\d{1,2}:\d{2}$", str(raw_time)):
                raw_time = _ANCHOR_DEFAULT_TIME.get(anchor, "08:00")

            grp = db.table("medicine_groups").insert(
                {
                    "parent_id":    parent_id,
                    "label":        _ANCHOR_LABEL.get(anchor, anchor.replace("_", " ").title()),
                    "anchor_event": anchor,
                    "time_window":  raw_time,
                    "sort_order":   sort_order,
                }
            ).execute()

            group_id = grp.data[0]["id"]

            for med in meds:
                db.table("medicines").insert(
                    {
                        "group_id":        group_id,
                        "name":            med.get("name", "medicine"),
                        "display_name":    med.get("display_name") or med.get("name", "medicine"),
                        "instructions":    med.get("instructions", ""),
                        "is_as_needed":    med.get("timing") == "as_needed",
                        "trigger_symptom": med.get("trigger_symptom"),
                    }
                ).execute()

        except Exception as e:
            logger.error(
                f"Failed to create medicine group '{anchor}' for parent {parent_id}: {e}"
            )