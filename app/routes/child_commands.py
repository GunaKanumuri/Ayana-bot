"""Child command handlers — all commands a caregiver child can send to AYANA.

Commands:
  menu / help        → show this command list
  status             → latest check-in status for all parents
  report [name] [Nd] → health summary (e.g. "report amma 7days")
  ask [name] [text]  → queue a custom question for the next check-in
  add parent         → start multi-step parent onboarding flow
  add [phone]        → add a sibling / co-caregiver to the family
  settings           → view current settings

Multi-step flows (add parent) use an in-memory state dict keyed by child phone.
For a multi-worker / multi-instance deployment, swap _child_state for Redis.
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
# { child_phone: { "flow": str, "step": str, "data": dict } }
# NOTE: reset on server restart — acceptable for low-traffic single-instance.
_child_state: dict[str, dict] = {}

# ─── Supported languages for parent onboarding ────────────────────────────────
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
_LANGUAGE_LIST = list(_LANGUAGES.keys())  # ordered for numbered menu

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
    "en": "maya",
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

# ─── Timing string → anchor event ────────────────────────────────────────────
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
➕ *add parent*           — Register a new parent
👥 *add +91XXXXXXXXXX*   — Add a sibling / co-caregiver
⚙️  *settings*             — View your current settings
📖 *menu*                 — Show this list

_Example: ask Amma did you take your BP tablet?_\
"""


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT — called from webhook.py background task
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_child_message(child: dict, msg: dict) -> None:
    """Route a child's incoming message to the correct command handler.

    If the child is in an active multi-step flow, continue that flow instead
    of parsing for a new command.

    Args:
        child: Row from the children table (includes id, phone, family_id, name).
        msg:   Normalised message dict from the WhatsApp service
               {phone, body, button_reply, is_voice_note, ...}.
    """
    phone = child["phone"]
    body = (msg.get("body") or "").strip()
    text = body.lower()

    # ── Ongoing multi-step flow takes priority ────────────────
    if phone in _child_state:
        await _handle_flow_step(child, msg)
        return

    # ── Route by command keyword ──────────────────────────────
    if text in ("menu", "help", "hi", "hello", "start", ""):
        await _cmd_menu(child)

    elif text == "status":
        await _cmd_status(child)

    elif text.startswith("report"):
        await _cmd_report(child, body)

    elif text.startswith("ask "):
        await _cmd_ask(child, body)

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
# COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _cmd_menu(child: dict) -> None:
    """Show the AYANA command list."""
    await send_message(child["phone"], COMMAND_MENU)


async def _cmd_status(child: dict) -> None:
    """Show today's check-in status for every parent in the family.

    For each parent shows: # touchpoints replied / sent, and latest mood.
    """
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

    Usage:
      report                → all parents, today
      report amma           → parent whose nickname matches "amma", today
      report amma 7days     → "amma", last 7 days
      report 7days          → all parents, last 7 days

    Args:
        child: Child record.
        body:  Full command string (e.g. "report amma 7days").
    """
    db = get_db()
    phone = child["phone"]
    family_id = child.get("family_id")

    if not family_id:
        await send_message(phone, "No parents set up yet. Send *add parent* to get started.")
        return

    # ── Parse tokens after "report" ───────────────────────────
    tokens = body.lower().split()[1:]  # drop "report"
    parent_filter: str | None = None
    days = 1

    for tok in tokens:
        days_match = re.match(r"^(\d+)\s*days?$", tok)
        if days_match:
            days = min(int(days_match.group(1)), 30)
        else:
            parent_filter = tok  # treat as parent nickname

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

            # Collect unique concerns
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

    The question is stored in the parent's conversation_state.context under
    "queued_questions" and will be sent as an extra touchpoint.

    Usage: ask amma did you take your BP tablet?

    Args:
        child: Child record (must have name field).
        body:  Full command string.
    """
    phone = child["phone"]
    family_id = child.get("family_id")

    # Expect: ["ask", "<parent_name>", "<question...>"]
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

        # Upsert into conversation_state.context
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
    """Add a sibling / co-caregiver to the same family.

    Creates a children row for the new phone and sends them an invite message.

    Args:
        child:     The requesting child (must have family_id).
        raw_phone: Phone number string (may or may not include '+').
    """
    db = get_db()
    phone = child["phone"]
    family_id = child.get("family_id")

    if not family_id:
        await send_message(phone, "No family set up yet. Send *add parent* first.")
        return

    # Normalise to E.164
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
        # Invite the new sibling
        await send_message(
            sibling_phone,
            f"You've been added to AYANA by *{child.get('name', 'a family member')}*.\n\n"
            f"Send *menu* to see available commands.",
        )

    except Exception as e:
        logger.error(f"Add sibling error for {phone}: {e}", exc_info=True)
        await send_message(phone, f"Could not add {sibling_phone}. Please try again.")


# ═══════════════════════════════════════════════════════════════════════════════
# ADD PARENT — MULTI-STEP ONBOARDING FLOW
# ═══════════════════════════════════════════════════════════════════════════════

async def _cmd_add_parent_start(child: dict) -> None:
    """Start the add-parent onboarding flow.

    Steps:
      waiting_name      → waiting_phone → waiting_language
      → waiting_time    → waiting_routine → confirming
      → (creates records)
    """
    phone = child["phone"]
    _child_state[phone] = {"flow": "add_parent", "step": "waiting_name", "data": {}}

    await send_message(
        phone,
        "Let's add your parent to AYANA.\n\n"
        "*What is your parent's name?*\n"
        "_(The name you call them — Amma, Nanna, Appa, etc.)_\n\n"
        "Send *cancel* at any time to stop.",
    )


async def _handle_flow_step(child: dict, msg: dict) -> None:
    """Continue an active multi-step flow.

    Dispatches to the correct flow handler based on state["flow"].
    Handles the universal "cancel" command.
    """
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


async def _add_parent_flow(child: dict, body: str, state: dict) -> None:
    """Drive the add-parent conversation forward one step.

    Args:
        child: Child record.
        body:  Raw text of the child's reply.
        state: Current flow state dict (mutated in place).
    """
    phone = child["phone"]
    step = state["step"]
    data = state["data"]

    # ── Step 1: collect parent name ───────────────────────────
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

    # ── Step 2: collect parent phone ──────────────────────────
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

    # ── Step 3: select language ───────────────────────────────
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
            await send_message(
                phone, f"Please reply with a number (e.g. *1* for Telugu)."
            )

    # ── Step 4: collect check-in time ─────────────────────────
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

    # ── Step 5: collect routine description ───────────────────
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

    # ── Step 6: final confirmation ────────────────────────────
    elif step == "confirming":
        if body.lower() in ("yes", "y", "confirm", "ok", "okay"):
            await _create_parent_records(child, data)
        elif body.lower() in ("no", "n", "cancel"):
            del _child_state[phone]
            await send_message(phone, "Cancelled. Send *add parent* to try again.")
        else:
            await send_message(phone, "Reply *YES* to confirm or *NO* to cancel.")


# ═══════════════════════════════════════════════════════════════════════════════
# RECORD CREATION  (called after onboarding confirmation)
# ═══════════════════════════════════════════════════════════════════════════════

async def _create_parent_records(child: dict, data: dict) -> None:
    """Create all Supabase records for a new parent after onboarding.

    Creates / updates:
      - families row (if child has no family yet)
      - parents row
      - medicine_groups + medicines (from Gemini routine extraction)

    Then sends a confirmation to the child and a welcome to the parent.

    Args:
        child: Child record (may have family_id = None if first parent).
        data:  Collected onboarding data dict.
    """
    phone = child["phone"]
    db = get_db()

    try:
        family_id = child.get("family_id")

        # ── Ensure family exists ──────────────────────────────
        if not family_id:
            fam = db.table("families").insert({"plan": "trial"}).execute()
            family_id = fam.data[0]["id"]
            db.table("children").update({"family_id": family_id}).eq(
                "id", child["id"]
            ).execute()
            logger.info(f"Created family {family_id} for child {phone}")

        # ── Create parent ─────────────────────────────────────
        voice = _DEFAULT_VOICE.get(data["language"], "roopa")
        parent_resp = db.table("parents").insert(
            {
                "family_id": family_id,
                "phone": data["phone"],
                "name": data["name"],
                "nickname": data["nickname"],
                "language": data["language"],
                "tts_voice": voice,
                "checkin_time": data["checkin_time"],
            }
        ).execute()
        parent_id = parent_resp.data[0]["id"]
        logger.info(f"Created parent {parent_id} ({data['name']}) for family {family_id}")

        # ── Extract routine with Gemini ───────────────────────
        routine_desc = data.get("routine_description", "")
        if routine_desc:
            try:
                from app.services.gemini import extract_routine
                routine = await extract_routine(routine_desc, data["name"])

                db.table("parents").update(
                    {
                        "activities": routine.activities,
                        "conditions": routine.conditions,
                        "alone_during_day": routine.alone_during_day,
                        "routine": {
                            "wake_time": routine.wake_time,
                            "notes": routine.notes,
                        },
                    }
                ).eq("id", parent_id).execute()

                if routine.medicines:
                    _create_medicine_groups(db, parent_id, routine.medicines)

                logger.info(f"Routine extracted for parent {parent_id}: {len(routine.medicines)} med(s)")

            except Exception as e:
                logger.error(
                    f"Routine extraction failed for parent {parent_id}: {e}",
                    exc_info=True,
                )
                # Non-fatal — parent record still created

        # ── Confirmation to child ─────────────────────────────
        await send_message(
            phone,
            f"*{data['name']} is now set up in AYANA!* ✅\n\n"
            f"I'll send them a morning greeting at *{data['checkin_time']}* every day.\n\n"
            f"*Important:* Ask {data['name']} to save this WhatsApp number "
            f"and send any message to me first — this is required to receive messages "
            f"(Twilio sandbox requirement).",
        )

        # ── Welcome message to parent ─────────────────────────
        await send_message(
            data["phone"],
            f"Namaste! I'm *AYANA*, your daily care companion 🙏\n\n"
            f"Your family has set me up to check in with you each day.\n"
            f"I'll say good morning at *{data['checkin_time']}* every day — "
            f"in *{data['language_name']}*.\n\n"
            f"Looking forward to our daily chats!",
        )

    except Exception as e:
        logger.error(
            f"Parent record creation failed for {phone}: {e}", exc_info=True
        )
        await send_message(
            phone,
            "Something went wrong while setting up the parent. Please try again.",
        )

    finally:
        # Always clean up flow state
        if phone in _child_state:
            del _child_state[phone]


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _create_medicine_groups(db, parent_id: str, medicines: list[dict]) -> None:
    """Create medicine_groups and medicines rows from Gemini's routine extraction.

    Groups medicines by their anchor event (before_food, after_food, etc.)
    and inserts one medicine_group per anchor with its medicines as children.

    Args:
        db:         Supabase client.
        parent_id:  UUID of the parent record.
        medicines:  List of medicine dicts from RoutineExtraction.medicines.
    """
    # Group by anchor event
    groups: dict[str, list[dict]] = {}
    for med in medicines:
        timing = med.get("timing", "after_food")
        anchor = _TIMING_TO_ANCHOR.get(timing, "after_food")
        groups.setdefault(anchor, []).append(med)

    for sort_order, (anchor, meds) in enumerate(groups.items()):
        try:
            # Use the first medicine's time_estimate, or the anchor default
            raw_time = meds[0].get("time_estimate", _ANCHOR_DEFAULT_TIME.get(anchor, "08:00"))
            # Ensure HH:MM format
            if not re.match(r"^\d{1,2}:\d{2}$", str(raw_time)):
                raw_time = _ANCHOR_DEFAULT_TIME.get(anchor, "08:00")

            grp = db.table("medicine_groups").insert(
                {
                    "parent_id": parent_id,
                    "label": _ANCHOR_LABEL.get(anchor, anchor.replace("_", " ").title()),
                    "anchor_event": anchor,
                    "time_window": raw_time,
                    "sort_order": sort_order,
                }
            ).execute()

            group_id = grp.data[0]["id"]

            for med in meds:
                db.table("medicines").insert(
                    {
                        "group_id": group_id,
                        "name": med.get("name", "medicine"),
                        "display_name": med.get("display_name") or med.get("name", "medicine"),
                        "instructions": med.get("instructions", ""),
                        "is_as_needed": med.get("timing") == "as_needed",
                        "trigger_symptom": med.get("trigger_symptom"),
                    }
                ).execute()

        except Exception as e:
            logger.error(
                f"Failed to create medicine group '{anchor}' for parent {parent_id}: {e}"
            )
