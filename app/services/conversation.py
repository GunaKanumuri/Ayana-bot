"""Parent conversation engine — the core of AYANA.

Every parent-facing message flows through this module:

  start_daily_conversation(parent_id)
    └─ Gemini plans the day's touchpoints
    └─ Stores plan in conversation_state
    └─ Fires the first touchpoint immediately

  send_touchpoint(parent, touchpoint_data)
    └─ Picks a message variation (rotates, never repeats back-to-back)
    └─ English → Sarvam translate → Sarvam TTS → audio URL
    └─ Translates buttons to parent's language in parallel
    └─ WhatsApp: audio + translated text + translated buttons
    └─ Creates check_in row (status=sent)

  handle_parent_response(parent, msg)
    └─ Button tap → parse action
    └─ Voice note → STT → translate → Gemini health extraction
    └─ Updates check_in (status=replied, mood, concerns, …)
    └─ Routes: pain → pain tree; health flow; normal → next touchpoint

  handle_pain_followup(parent, severity, state)
    └─ mild     → acknowledge, log concern
    └─ moderate → log, open health_flow, alert child
    └─ severe   → emergency alert to all children, open health_flow
"""

import asyncio
import logging
from datetime import date, datetime, timedelta

from app.config import settings
from app.db import get_db
from app.services import sarvam, whatsapp
from app.services.gemini import extract_health, plan_daily_conversation

logger = logging.getLogger(__name__)


# ─── Pain tree touchpoints (injected ahead of remaining plan) ────────────────

_PAIN_LOCATION_TP: dict = {
    "touchpoint_type": "pain_location",
    "message_english": "I'm sorry to hear that, {nickname}. Where are you feeling the discomfort?",
    "button_options": [
        {"emoji": "🤕", "text_english": "Head or chest", "action": "pain_head_chest"},
        {"emoji": "🦴", "text_english": "Joints or legs", "action": "pain_joints"},
        {"emoji": "🤢", "text_english": "Stomach", "action": "pain_stomach"},
    ],
    "include_voice_invite": True,
    "is_health_flow": True,
}

_PAIN_SEVERITY_TP: dict = {
    "touchpoint_type": "pain_severity",
    "message_english": "How bad is the pain, {nickname}?",
    "button_options": [
        {"emoji": "😐", "text_english": "Mild, bearable", "action": "severity_mild"},
        {"emoji": "😣", "text_english": "Moderate pain",  "action": "severity_moderate"},
        {"emoji": "😰", "text_english": "Severe, very bad", "action": "severity_severe"},
    ],
    "include_voice_invite": False,
    "is_health_flow": True,
}

# Map anchor_event → touchpoint_type for medicine groups
_ANCHOR_TO_TP: dict[str, str] = {
    "wake":        "medicine_before_food",
    "before_food": "medicine_before_food",
    "after_food":  "medicine_after_food",
    "afternoon":   "medicine_after_food",
    "evening":     "medicine_after_food",
    "dinner":      "medicine_after_food",
    "after_dinner":"medicine_night",
    "night":       "medicine_night",
}

# Button actions that map to moods
_ACTION_MOOD: dict[str, str] = {
    "mood_good": "good",
    "mood_okay": "okay",
    "mood_bad":  "not_well",
    "mood_great":"good",
}


# ═══════════════════════════════════════════════════════════════════════════════
# SEND TOUCHPOINT
# ═══════════════════════════════════════════════════════════════════════════════

async def send_touchpoint(parent: dict, touchpoint_data: dict) -> bool:
    """Send a single touchpoint through the full AYANA pipeline.

    Pipeline:
      1. Pick the least-recently-used message variation for this touchpoint type.
      2. Personalise: replace {nickname} placeholder.
      3. English → Sarvam translate + TTS → (audio_url, translated_text).
      4. Translate button texts to parent's language in parallel.
      5. Append voice invite note if requested.
      6. WhatsApp: send_audio_and_buttons(phone, audio_url, text, buttons).
      7. Upsert check_in row (status=sent, sent_at=now).
      8. Update variation.last_used_at.
      9. Update conversation_state (current_touchpoint, awaiting_response=True).

    Args:
        parent:         Full parent row from Supabase.
        touchpoint_data: Planned touchpoint dict
                         {touchpoint_type, message_english, button_options,
                          include_voice_invite, is_health_flow, medicine_group_id}.

    Returns:
        True if the message was dispatched successfully, False on error.
    """
    db = get_db()
    parent_id  = parent["id"]
    phone      = parent["phone"]
    language   = parent.get("language", "te")
    voice      = parent.get("tts_voice", "roopa")
    nickname   = parent.get("nickname", "")
    tp_type    = touchpoint_data.get("touchpoint_type", "")
    today      = date.today().isoformat()

    # ── 0. WA 24h session window — first message of day needs template ─────────
    is_first_message_today = False
    try:
        existing_today = (
            db.table("check_ins")
            .select("id")
            .eq("parent_id", parent_id)
            .eq("date", today)
            .limit(1)
            .execute()
            .data
        )
        is_first_message_today = not existing_today
    except Exception:
        pass

    if is_first_message_today:
        # Send template to open the 24h session window (Meta Cloud API requirement)
        try:
            await whatsapp.send_template(
                to=phone,
                template_name="ayana_morning_greeting",
                language=language,
                components=[{
                    "type": "body",
                    "parameters": [{"type": "text", "text": nickname}],
                }],
            )
            logger.info(f"WA template sent to open session for {phone}")
            await asyncio.sleep(1)  # Brief pause before follow-up message
        except Exception as e:
            logger.warning(f"Template send failed for {phone}, continuing with regular message: {e}")

    # ── 1. Pick variation ──────────────────────────────────────────────────────
    message_en, variation_id = _pick_variation(db, parent_id, tp_type, touchpoint_data)
    message_en = message_en.replace("{nickname}", nickname)

    # ── 2. Translate + TTS ─────────────────────────────────────────────────────
    try:
        audio_url, translated_text = await sarvam.english_to_parent_audio(
            message_en, language, voice, nickname
        )
    except Exception as e:
        logger.error(f"TTS pipeline failed for {phone}/{tp_type}: {e}")
        audio_url, translated_text = None, None

    if not translated_text:
        translated_text = message_en  # Plain English fallback

    # ── 3. Voice invite suffix ─────────────────────────────────────────────────
    if touchpoint_data.get("include_voice_invite"):
        invite_en = "\n(You can also reply with a voice message 🎤)"
        try:
            invite_tr = await sarvam.translate(invite_en, "en", language)
            translated_text += (invite_tr or invite_en)
        except Exception:
            translated_text += invite_en

    # ── 4. Translate buttons in parallel ───────────────────────────────────────
    raw_buttons = list(touchpoint_data.get("button_options", []))

    # Inject emergency button for mood/greeting touchpoints (WA max 3 buttons)
    # Cap regular buttons at 2, add emergency as 3rd
    _EMERGENCY_TOUCHPOINTS = {
        "morning_greeting", "evening_checkin", "food_check",
        "activity_check", "anything_else",
    }
    if tp_type in _EMERGENCY_TOUCHPOINTS and not any(
        b.get("action") == "emergency" for b in raw_buttons
    ):
        raw_buttons = raw_buttons[:2]  # WA limit: max 3 total
        raw_buttons.append({
            "emoji": "🆘", "text_english": "Emergency", "action": "emergency",
        })

    translated_buttons = await _translate_buttons(raw_buttons, language)

    # ── 5. Send ────────────────────────────────────────────────────────────────
    try:
        await whatsapp.send_audio_and_buttons(
            to=phone,
            audio_url=audio_url or "",
            text=translated_text,
            buttons=translated_buttons or None,
        )
    except Exception as e:
        logger.error(f"WhatsApp send failed for {phone}/{tp_type}: {e}", exc_info=True)
        return False

    # ── 6. Upsert check_in row ─────────────────────────────────────────────────
    try:
        db.table("check_ins").upsert(
            {
                "parent_id":    parent_id,
                "date":         today,
                "touchpoint":   tp_type,
                "status":       "sent",
                "sent_at":      datetime.utcnow().isoformat(),
                "variation_id": variation_id,
            },
            on_conflict="parent_id,date,touchpoint",
        ).execute()
    except Exception as e:
        logger.error(f"check_in upsert failed for {parent_id}/{tp_type}: {e}")

    # ── 7. Mark variation used ─────────────────────────────────────────────────
    if variation_id:
        try:
            db.table("message_variations").update(
                {"last_used_at": datetime.utcnow().isoformat()}
            ).eq("id", variation_id).execute()
        except Exception as e:
            logger.warning(f"Variation last_used_at update failed: {e}")

    # ── 8. Update conversation_state ───────────────────────────────────────────
    try:
        db.table("conversation_state").update(
            {
                "current_touchpoint": tp_type,
                "awaiting_response":  True,
                "pending_buttons":    translated_buttons,
                "updated_at":         datetime.utcnow().isoformat(),
            }
        ).eq("parent_id", parent_id).eq("date", today).execute()
    except Exception as e:
        logger.error(f"conversation_state update failed for {parent_id}: {e}")

    logger.info(f"Touchpoint '{tp_type}' → {phone} ({language}), audio={'yes' if audio_url else 'no'}")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# START DAILY CONVERSATION
# ═══════════════════════════════════════════════════════════════════════════════

async def start_daily_conversation(parent_id: str) -> bool:
    """Begin today's planned conversation for a parent.

    Steps:
      1. Load parent profile, medicines, active health flows, special dates.
      2. Build yesterday's context from check_ins.
      3. Ask Gemini to plan today's touchpoints (with fallback if Gemini fails).
      4. Ensure message variations exist for each planned touchpoint type.
      5. Upsert conversation_state with the full plan.
      6. Send the first touchpoint immediately.

    Args:
        parent_id: UUID of the parent row.

    Returns:
        True if conversation started (first message sent), False otherwise.
    """
    db  = get_db()
    today = date.today().isoformat()

    # ── Load parent ────────────────────────────────────────────────────────────
    try:
        rows = db.table("parents").select("*").eq("id", parent_id).execute().data
        if not rows:
            logger.error(f"Parent {parent_id} not found")
            return False
        parent = rows[0]
    except Exception as e:
        logger.error(f"Parent fetch failed ({parent_id}): {e}")
        return False

    if not parent.get("is_active"):
        return False

    paused_until = parent.get("paused_until")
    if paused_until and str(paused_until) >= today:
        logger.info(f"Parent {parent_id} paused until {paused_until}")
        return False

    # Already started today?
    try:
        existing = (
            db.table("conversation_state")
            .select("id")
            .eq("parent_id", parent_id)
            .eq("date", today)
            .execute()
            .data
        )
        if existing:
            logger.info(f"Conversation already started for {parent_id} today")
            return True
    except Exception as e:
        logger.warning(f"conversation_state check failed: {e}")

    # ── Load medicine groups ────────────────────────────────────────────────────
    try:
        med_groups = (
            db.table("medicine_groups")
            .select("*, medicines(*)")
            .eq("parent_id", parent_id)
            .order("sort_order")
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"Medicine groups fetch failed ({parent_id}): {e}")
        med_groups = []

    # ── Load active health flows ────────────────────────────────────────────────
    try:
        health_flows = (
            db.table("health_flows")
            .select("*")
            .eq("parent_id", parent_id)
            .neq("state", "resolved")
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"Health flows fetch failed ({parent_id}): {e}")
        health_flows = []

    # ── Yesterday's context ─────────────────────────────────────────────────────
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    try:
        yesterday_cis = (
            db.table("check_ins")
            .select("touchpoint, status, mood, concerns, ai_extraction")
            .eq("parent_id", parent_id)
            .eq("date", yesterday)
            .execute()
            .data or []
        )
        yesterday_ctx: dict = {
            "checkins":    yesterday_cis,
            "had_concerns": any(c.get("concerns") for c in yesterday_cis),
            "mood":        next(
                (c["mood"] for c in yesterday_cis if c.get("mood")), None
            ),
        }
    except Exception:
        yesterday_ctx = {}

    # ── Special dates today ─────────────────────────────────────────────────────
    try:
        all_dates = (
            db.table("special_dates")
            .select("*")
            .eq("parent_id", parent_id)
            .execute()
            .data or []
        )
        today_mmdd = f"{date.today().month:02d}-{date.today().day:02d}"
        special_dates_today = [
            s for s in all_dates
            if s.get("recurring") and str(s.get("date_value", ""))[-5:] == today_mmdd
        ]
    except Exception:
        special_dates_today = []

    # ── Primary child's name ────────────────────────────────────────────────────
    try:
        child_rows = (
            db.table("children")
            .select("name")
            .eq("family_id", parent["family_id"])
            .eq("is_primary", True)
            .execute()
            .data
        )
        child_name = child_rows[0]["name"] if child_rows else ""
    except Exception:
        child_name = ""

    # ── Plan via Gemini ─────────────────────────────────────────────────────────
    parent_profile = {
        "nickname":       parent["nickname"],
        "activities":     parent.get("activities") or [],
        "conditions":     parent.get("conditions") or [],
        "routine":        parent.get("routine") or {},
        "alone_during_day": parent.get("alone_during_day", False),
        "checkin_time":   str(parent.get("checkin_time", "08:00")),
    }

    try:
        touchpoints = await plan_daily_conversation(
            parent_profile=parent_profile,
            active_health_flows=health_flows,
            yesterday_context=yesterday_ctx,
            medicine_groups=med_groups,
            special_dates=special_dates_today,
            child_name=child_name,
        )
    except Exception as e:
        logger.error(f"Gemini planning failed for {parent_id}: {e}")
        touchpoints = []

    if not touchpoints:
        logger.warning(f"No touchpoints from Gemini for {parent_id} — using fallback")
        touchpoints = _fallback_touchpoints(parent["nickname"], bool(med_groups))

    # ── Sanitize: reject touchpoints with invalid types ────────────────────────
    _VALID_TOUCHPOINT_TYPES = {
        "morning_greeting", "food_check", "medicine_before_food",
        "medicine_after_food", "medicine_night", "activity_check",
        "evening_checkin", "anything_else", "goodnight",
        "pain_location", "pain_severity",
    }
    touchpoints = [
        tp for tp in touchpoints
        if tp.get("touchpoint_type") in _VALID_TOUCHPOINT_TYPES
    ]
    if not touchpoints:
        logger.warning(f"All Gemini touchpoints had invalid types for {parent_id} — using fallback")
        touchpoints = _fallback_touchpoints(parent["nickname"], bool(med_groups))

    # ── Ensure variations exist for each touchpoint type ───────────────────────
    await _ensure_variations_exist(db, parent, touchpoints)

    # ── Write conversation_state ────────────────────────────────────────────────
    first_tp   = touchpoints[0]
    remaining  = touchpoints[1:]

    try:
        db.table("conversation_state").upsert(
            {
                "parent_id":              parent_id,
                "date":                   today,
                "current_touchpoint":     first_tp["touchpoint_type"],
                "awaiting_response":      False,
                "touchpoints_completed":  [],
                "touchpoints_remaining":  remaining,
                "pending_buttons":        [],
                "context": {
                    "yesterday":     yesterday_ctx,
                    "health_flows":  [h["condition"] for h in health_flows],
                },
                "nudge_sent":  False,
                "updated_at":  datetime.utcnow().isoformat(),
            },
            on_conflict="parent_id,date",
        ).execute()
    except Exception as e:
        logger.error(f"conversation_state creation failed for {parent_id}: {e}")
        return False

    # ── Fire first touchpoint ───────────────────────────────────────────────────
    return await send_touchpoint(parent, first_tp)


# ═══════════════════════════════════════════════════════════════════════════════
# HANDLE PARENT RESPONSE
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_parent_response(parent: dict, msg: dict) -> None:
    """Process an incoming message from a parent and advance the conversation.

    Handles three message types:
      - Button reply: parse action string, derive mood.
      - Voice note:   download → STT → translate → Gemini health extraction.
      - Plain text:   treat as text reply, run health extraction.

    Then:
      - Updates check_in (status=replied, mood, concerns, ai_extraction).
      - Logs concerns to concern_log.
      - Creates alerts for urgent signals.
      - Routes: not_well mood → pain tree; pain_location → pain_severity;
        pain_severity → handle_pain_followup.
      - Advances to next touchpoint, or closes today's conversation.

    Args:
        parent: Full parent row (from webhook, may include families(*) join).
        msg:    Normalised message dict {phone, body, button_reply,
                media_url, is_voice_note, …}.
    """
    db         = get_db()
    parent_id  = parent["id"]
    phone      = parent["phone"]
    language   = parent.get("language", "te")
    today      = date.today().isoformat()

    # ── Load conversation state ─────────────────────────────────────────────────
    try:
        state_rows = (
            db.table("conversation_state")
            .select("*")
            .eq("parent_id", parent_id)
            .eq("date", today)
            .execute()
            .data
        )
    except Exception as e:
        logger.error(f"conversation_state fetch failed for {parent_id}: {e}")
        return

    if not state_rows:
        logger.info(f"No conversation state for {phone} today — spontaneous message")
        await _handle_spontaneous_message(parent, msg)
        return

    state = state_rows[0]

    if not state.get("awaiting_response"):
        logger.info(f"Parent {phone} responded but conversation not awaiting input")
        return

    current_tp = state.get("current_touchpoint", "")

    # ── Extract content from message ────────────────────────────────────────────
    is_voice   = msg.get("is_voice_note", False)
    raw_body   = (msg.get("button_reply") or msg.get("body") or "").strip()

    # ── Resolve "1" / "2" / "3" to button action (Twilio sandbox workaround) ───
    # When Twilio renders buttons as numbered text, the parent replies with a
    # digit. We map it back to the corresponding button action id here so the
    # rest of the engine never needs to know about sandbox limitations.
    if raw_body in ("1", "2", "3"):
        pending = state.get("pending_buttons") or []
        idx = int(raw_body) - 1
        if 0 <= idx < len(pending):
            raw_body = pending[idx].get("id", raw_body)
            logger.info(f"Resolved number '{msg.get('body')}' → action '{raw_body}'")

    action     = raw_body.lower()
    english_text = raw_body
    health_data: dict = {}
    mood: str | None  = None

    # ── Emergency button tap — immediate trigger, no AI needed ────────────────
    if action in ("emergency", "btn_emergency", "🆘"):
        from app.services.emergency import trigger_emergency
        await trigger_emergency(parent_id, {
            "raw_summary": "Parent pressed the emergency button",
            "severity": "severe",
            "trigger": "button",
        })
        return

    if is_voice and msg.get("media_url"):
        # ── Voice note: full STT → translate → Gemini pipeline ──────────────────
        try:
            audio_bytes = await whatsapp.download_voice_note(msg["media_url"])
            if audio_bytes:
                english_text = await sarvam.parent_voice_to_english(audio_bytes, language) or ""
                if english_text:
                    # Fast keyword check on ENGLISH text — fires before Gemini
                    from app.services.emergency import fast_keyword_check, trigger_emergency
                    if fast_keyword_check(english_text):
                        logger.warning(f"CRITICAL keyword detected for {phone}: {english_text[:100]}")
                        await trigger_emergency(parent_id, {
                            "raw_summary": english_text[:200],
                            "severity": "severe",
                            "trigger": "keyword_detection",
                        })
                        # Still run Gemini extraction below for logging

                    ctx = {
                        "active_health_flows": state.get("context", {}).get("health_flows", []),
                    }
                    extraction = await extract_health(english_text, ctx)
                    mood = extraction.mood
                    health_data = {
                        "mood":               extraction.mood,
                        "concerns":           extraction.concerns,
                        "medicine_mentioned": extraction.medicine_mentioned,
                        "severity":           extraction.severity,
                        "urgency_flag":       extraction.urgency_flag,
                        "follow_up_needed":   extraction.follow_up_needed,
                        "food_eaten":         extraction.food_eaten,
                        "raw_summary":        extraction.raw_summary,
                    }
                    action = f"voice_{extraction.mood or 'replied'}"
        except Exception as e:
            logger.error(f"Voice pipeline failed for {phone}: {e}", exc_info=True)

    elif raw_body:
        # ── Text or button reply ────────────────────────────────────────────────
        mood = _action_to_mood(action)
        # Run health extraction on any text reply (catches complaints in text)
        if len(raw_body) > 5 and not _is_button_action(action):
            try:
                extraction = await extract_health(raw_body, {})
                if extraction.concerns or extraction.urgency_flag:
                    health_data = {
                        "mood":          extraction.mood or mood,
                        "concerns":      extraction.concerns,
                        "severity":      extraction.severity,
                        "urgency_flag":  extraction.urgency_flag,
                        "raw_summary":   extraction.raw_summary,
                    }
                    mood = extraction.mood or mood
            except Exception as e:
                logger.warning(f"Text health extraction failed: {e}")

    # ── Update check_in record ──────────────────────────────────────────────────
    medicine_taken_field: dict = {}
    if current_tp.startswith("medicine_"):
        medicine_taken_field = {
            "taken":  action in ("medicine_taken", "taken"),
            "action": action,
        }

    try:
        db.table("check_ins").update(
            {
                "status":         "replied",
                "mood":           mood,
                "raw_reply":      (english_text or raw_body)[:500],
                "raw_audio_url":  msg.get("media_url", "") if is_voice else "",
                "concerns":       health_data.get("concerns", []),
                "medicine_taken": medicine_taken_field or {},
                "ai_extraction":  health_data,
                "replied_at":     datetime.utcnow().isoformat(),
            }
        ).eq("parent_id", parent_id).eq("date", today).eq("touchpoint", current_tp).execute()
    except Exception as e:
        logger.error(f"check_in update failed for {parent_id}/{current_tp}: {e}")

    # ── Log concerns ────────────────────────────────────────────────────────────
    if health_data.get("concerns"):
        await _log_concerns(db, parent_id, health_data["concerns"], health_data.get("severity"))

    # ── Emergency alert ─────────────────────────────────────────────────────────
    if health_data.get("urgency_flag"):
        await _create_urgent_alert(db, parent, health_data, english_text or raw_body)

    # ── Update completed list ───────────────────────────────────────────────────
    completed = list(state.get("touchpoints_completed") or [])
    if current_tp and current_tp not in completed:
        completed.append(current_tp)

    remaining = list(state.get("touchpoints_remaining") or [])

    # ── Conversation routing ────────────────────────────────────────────────────

    # Medicine retry/snooze — "Will take soon" schedules a 15-min retry
    if action in ("medicine_later", "medicine_not_yet", "will_take_soon") and current_tp.startswith("medicine_"):
        ctx = dict(state.get("context") or {})
        retry_count = ctx.get("medicine_retry_count", 0)
        max_retries = 2

        if retry_count < max_retries:
            retry_at = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
            ctx["medicine_retry_at"] = retry_at
            ctx["medicine_retry_count"] = retry_count + 1
            ctx["medicine_retry_group_id"] = (
                state.get("context", {}).get("medicine_group_id")
                or _get_medicine_group_from_tp(db, parent_id, current_tp)
            )
            try:
                db.table("conversation_state").update({"context": ctx}).eq(
                    "id", state["id"]
                ).execute()
            except Exception as e:
                logger.warning(f"medicine retry context update failed: {e}")
            logger.info(
                f"Medicine retry #{retry_count + 1} scheduled for {phone} at {retry_at}"
            )
        else:
            logger.info(f"Medicine max retries reached for {phone}, moving on")

    if _is_not_well(action, mood) and current_tp not in ("pain_location", "pain_severity"):
        # Inject pain tree before remaining plan
        remaining = [_PAIN_LOCATION_TP, _PAIN_SEVERITY_TP] + remaining

    elif current_tp == "pain_location":
        # User selected pain location — store in context
        ctx = dict(state.get("context") or {})
        ctx["pain_location"] = action.replace("pain_", "")
        try:
            db.table("conversation_state").update({"context": ctx}).eq(
                "id", state["id"]
            ).execute()
        except Exception as e:
            logger.warning(f"context update failed: {e}")
        # severity touchpoint is next in remaining (already injected above)

    elif current_tp == "pain_severity":
        severity = action.replace("severity_", "")
        await handle_pain_followup(parent, severity, state)

    # ── Advance conversation ────────────────────────────────────────────────────
    try:
        if remaining:
            next_tp = remaining[0]
            db.table("conversation_state").update(
                {
                    "current_touchpoint":    next_tp["touchpoint_type"],
                    "touchpoints_completed": completed,
                    "touchpoints_remaining": remaining[1:],
                    "awaiting_response":     False,
                    "updated_at":            datetime.utcnow().isoformat(),
                }
            ).eq("id", state["id"]).execute()

            await asyncio.sleep(1.5)  # Small pause between messages
            await send_touchpoint(parent, next_tp)

        else:
            # Today's conversation complete
            db.table("conversation_state").update(
                {
                    "current_touchpoint":    None,
                    "touchpoints_completed": completed,
                    "touchpoints_remaining": [],
                    "awaiting_response":     False,
                    "updated_at":            datetime.utcnow().isoformat(),
                }
            ).eq("id", state["id"]).execute()
            logger.info(f"Conversation complete for {phone} on {today}")

    except Exception as e:
        logger.error(f"Conversation advance failed for {parent_id}: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
# PAIN FOLLOW-UP TREE
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_pain_followup(parent: dict, severity: str, state: dict) -> None:
    """Respond to the pain severity selection and escalate as needed.

    Routing:
      mild     → warm acknowledgement + log concern
      moderate → acknowledge + open health_flow + alert child
      severe   → emergency alert to all children + open health_flow

    Args:
        parent:   Full parent row.
        severity: "mild" | "moderate" | "severe" (from button action).
        state:    Current conversation_state row (for context).
    """
    db        = get_db()
    parent_id = parent["id"]
    phone     = parent["phone"]
    language  = parent.get("language", "te")
    voice     = parent.get("tts_voice", "roopa")
    nickname  = parent.get("nickname", "")
    ctx       = state.get("context") or {}
    location  = ctx.get("pain_location", "body").replace("_", " ")

    if severity == "severe":
        msg_en = (
            f"{nickname}, I'm letting your family know right away. "
            f"Please sit down, stay calm, and don't move around. "
            f"Someone will contact you very soon."
        )
        audio_url, translated = await sarvam.english_to_parent_audio(
            msg_en, language, voice, nickname
        )
        await whatsapp.send_audio_and_buttons(
            to=phone, audio_url=audio_url or "",
            text=translated or msg_en,
        )
        await _create_pain_alert(db, parent, location, "severe")
        _upsert_health_flow(
            db, parent_id, f"severe_pain_{location}", "active",
            {"severity": "severe", "location": location},
        )

    elif severity == "moderate":
        msg_en = (
            f"I've noted that, {nickname}. "
            f"I'll let your family know and check in on this tomorrow. "
            f"If it gets worse, please message me."
        )
        audio_url, translated = await sarvam.english_to_parent_audio(
            msg_en, language, voice, nickname
        )
        await whatsapp.send_audio_and_buttons(
            to=phone, audio_url=audio_url or "",
            text=translated or msg_en,
        )
        await _create_pain_alert(db, parent, location, "moderate")
        _upsert_health_flow(
            db, parent_id, f"pain_{location}", "active",
            {"severity": "moderate", "location": location},
        )

    else:  # mild
        msg_en = (
            f"I see, {nickname}. I've noted the mild discomfort. "
            f"Do tell me if it gets worse."
        )
        audio_url, translated = await sarvam.english_to_parent_audio(
            msg_en, language, voice, nickname
        )
        await whatsapp.send_audio_and_buttons(
            to=phone, audio_url=audio_url or "",
            text=translated or msg_en,
        )

    # Always log concern
    try:
        _upsert_concern_sync(db, parent_id, f"{location} pain", "pain", severity)
    except Exception as e:
        logger.warning(f"concern_log upsert failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# MEDICINE REMINDER  (called directly by scheduler for standalone reminders)
# ═══════════════════════════════════════════════════════════════════════════════

async def send_medicine_reminder(parent: dict, medicine_group: dict) -> bool:
    """Send a standalone medicine reminder for a specific medicine group.

    Used by the scheduler when a medicine group's time_window fires and the
    touchpoint hasn't already been sent as part of the planned conversation.

    Args:
        parent:         Full parent row.
        medicine_group: medicine_groups row (may include medicines(*)).

    Returns:
        True if sent, False otherwise.
    """
    anchor   = medicine_group.get("anchor_event", "after_food")
    tp_type  = _ANCHOR_TO_TP.get(anchor, "medicine_after_food")
    label    = medicine_group.get("label", "your medicines")
    meds     = medicine_group.get("medicines") or []
    med_names = [m.get("display_name") or m.get("name", "") for m in meds if not m.get("is_as_needed")]

    if med_names:
        msg_en = f"{{nickname}}, time for {', '.join(med_names[:3])}."
    else:
        msg_en = f"{{nickname}}, time for {label}."

    touchpoint_data = {
        "touchpoint_type":  tp_type,
        "message_english":  msg_en,
        "button_options": [
            {"emoji": "✅", "text_english": "Taken",       "action": "medicine_taken"},
            {"emoji": "⏰", "text_english": "Will take soon", "action": "medicine_later"},
            {"emoji": "❌", "text_english": "Skipped",     "action": "medicine_skipped"},
        ],
        "include_voice_invite": False,
        "is_health_flow":       False,
        "medicine_group_id":    medicine_group.get("id"),
    }

    return await send_touchpoint(parent, touchpoint_data)


# ═══════════════════════════════════════════════════════════════════════════════
# NUDGE  (called by scheduler 3 h after unanswered check-in)
# ═══════════════════════════════════════════════════════════════════════════════

async def send_nudge(parent: dict) -> None:
    """Send a gentle follow-up if the parent hasn't responded to their morning greeting.

    Args:
        parent: Full parent row.
    """
    phone    = parent["phone"]
    language = parent.get("language", "te")
    voice    = parent.get("tts_voice", "roopa")
    nickname = parent.get("nickname", "")

    msg_en = (
        f"{nickname}, just checking in again. "
        f"Your family would love to hear from you today. "
        f"How are you feeling?"
    )
    buttons = [
        {"emoji": "😊", "text_english": "I'm fine",   "action": "mood_good"},
        {"emoji": "😐", "text_english": "Okay",        "action": "mood_okay"},
        {"emoji": "😔", "text_english": "Not well",    "action": "mood_bad"},
    ]

    try:
        audio_url, translated = await sarvam.english_to_parent_audio(
            msg_en, language, voice, nickname
        )
        translated_buttons = await _translate_buttons(buttons, language)
        await whatsapp.send_audio_and_buttons(
            to=phone,
            audio_url=audio_url or "",
            text=translated or msg_en,
            buttons=translated_buttons or None,
        )
        logger.info(f"Nudge sent to {phone}")
    except Exception as e:
        logger.error(f"Nudge send failed for {phone}: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# PRIVATE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _pick_variation(
    db,
    parent_id: str,
    touchpoint_type: str,
    touchpoint_data: dict,
) -> tuple[str, str | None]:
    """Return (message_text, variation_id) for the least-recently-used variation.

    Sorts by last_used_at ASC NULLS FIRST so never-used variations come first.
    Falls back to touchpoint_data.message_english if no variations stored yet.
    """
    try:
        rows = (
            db.table("message_variations")
            .select("id, message_text, last_used_at")
            .eq("parent_id", parent_id)
            .eq("touchpoint", touchpoint_type)
            .eq("is_selected", True)
            .order("last_used_at", desc=False, nullsfirst=True)
            .execute()
            .data or []
        )
        if rows:
            return rows[0]["message_text"], rows[0]["id"]
    except Exception as e:
        logger.warning(f"Variation fetch failed ({parent_id}/{touchpoint_type}): {e}")

    return touchpoint_data.get("message_english", ""), None


async def _translate_buttons(
    button_options: list[dict],
    language: str,
) -> list[dict]:
    """Translate button text to parent's language in parallel.

    Button format in:  {emoji, text_english, action}
    Button format out: {id, title, emoji}   (ready for whatsapp.send_message)

    WhatsApp imposes a 20-character limit on button titles.
    Emoji is preserved; only text_english is translated.

    Args:
        button_options: Raw button dicts from the touchpoint plan.
        language:       Target language code.

    Returns:
        Up to 3 translated button dicts.
    """
    if not button_options:
        return []

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
            "title": f"{title}"[:20],
            "emoji": emoji,
        }

    tasks = [_one(btn, i) for i, btn in enumerate(button_options[:3])]
    return list(await asyncio.gather(*tasks))


async def _ensure_variations_exist(
    db,
    parent: dict,
    touchpoints: list[dict],
) -> None:
    """Generate and store Gemini message variations for any touchpoint type
    that doesn't yet have them in message_variations.

    Args:
        db:          Supabase client.
        parent:      Full parent row.
        touchpoints: Planned touchpoints for today.
    """
    from app.services.gemini import generate_variations

    parent_id = parent["id"]
    profile   = {
        "nickname":   parent.get("nickname", ""),
        "activities": parent.get("activities") or [],
        "conditions": parent.get("conditions") or [],
    }

    for tp in touchpoints:
        tp_type = tp.get("touchpoint_type", "")
        if not tp_type:
            continue
        # Skip pain tree pseudo-touchpoints (no variations needed)
        if tp_type in ("pain_location", "pain_severity"):
            continue

        try:
            existing = (
                db.table("message_variations")
                .select("id")
                .eq("parent_id", parent_id)
                .eq("touchpoint", tp_type)
                .execute()
                .data
            )
            if existing:
                continue

            # Generate variations
            variations = await generate_variations(
                tp_type, parent.get("nickname", ""), profile, count=5
            )

            # Include the Gemini-planned message too
            all_msgs = list(dict.fromkeys(
                [tp.get("message_english", "")] + variations
            ))

            rows = [
                {
                    "parent_id":       parent_id,
                    "touchpoint":      tp_type,
                    "message_text":    m,
                    "is_ai_generated": True,
                    "is_selected":     True,
                }
                for m in all_msgs if m
            ]
            if rows:
                db.table("message_variations").insert(rows).execute()
                logger.info(f"Created {len(rows)} variations for {parent_id}/{tp_type}")

        except Exception as e:
            logger.warning(f"Variation generation failed for {tp_type}: {e}")
            # Ensure at least the planned message is stored
            msg_text = tp.get("message_english", "")
            if msg_text:
                try:
                    db.table("message_variations").insert(
                        {
                            "parent_id":       parent_id,
                            "touchpoint":      tp_type,
                            "message_text":    msg_text,
                            "is_ai_generated": False,
                            "is_selected":     True,
                        }
                    ).execute()
                except Exception:
                    pass


async def _handle_spontaneous_message(parent: dict, msg: dict) -> None:
    """Acknowledge a message received outside of an active conversation.

    Runs health extraction on voice notes and fires alerts for urgent signals.
    Sends a brief acknowledgement to the parent.

    Args:
        parent: Full parent row.
        msg:    Normalised message dict.
    """
    phone    = parent["phone"]
    language = parent.get("language", "te")
    voice    = parent.get("tts_voice", "roopa")
    nickname = parent.get("nickname", "")

    if msg.get("is_voice_note") and msg.get("media_url"):
        try:
            audio_bytes = await whatsapp.download_voice_note(msg["media_url"])
            if audio_bytes:
                english_text = await sarvam.parent_voice_to_english(audio_bytes, language)
                if english_text:
                    extraction = await extract_health(english_text)
                    if extraction.urgency_flag:
                        db = get_db()
                        await _create_urgent_alert(
                            db, parent,
                            {"urgency_flag": True, "raw_summary": extraction.raw_summary},
                            english_text,
                        )
        except Exception as e:
            logger.error(f"Spontaneous voice processing failed for {phone}: {e}")

    ack_en = f"Got your message, {nickname}. Your family will be updated."
    try:
        audio_url, translated = await sarvam.english_to_parent_audio(
            ack_en, language, voice, nickname
        )
        await whatsapp.send_audio_and_buttons(
            to=phone, audio_url=audio_url or "", text=translated or ack_en
        )
    except Exception as e:
        logger.error(f"Spontaneous ack send failed for {phone}: {e}")


def _fallback_touchpoints(nickname: str, has_medicines: bool) -> list[dict]:
    """Return a minimal hardcoded conversation plan when Gemini is unavailable."""
    tps = [
        {
            "touchpoint_type":   "morning_greeting",
            "message_english":   f"Good morning {{nickname}}! How are you feeling today?",
            "button_options": [
                {"emoji": "😊", "text_english": "Feeling good", "action": "mood_good"},
                {"emoji": "😐", "text_english": "Okay",         "action": "mood_okay"},
                {"emoji": "😔", "text_english": "Not well",     "action": "mood_bad"},
            ],
            "include_voice_invite": True,
            "is_health_flow":       False,
        }
    ]
    if has_medicines:
        tps.append(
            {
                "touchpoint_type": "medicine_after_food",
                "message_english": "Did you take your medicines after breakfast, {nickname}?",
                "button_options": [
                    {"emoji": "✅", "text_english": "Yes, taken",   "action": "medicine_taken"},
                    {"emoji": "⏰", "text_english": "Will take soon","action": "medicine_later"},
                ],
                "include_voice_invite": False,
                "is_health_flow":       False,
            }
        )
    tps.append(
        {
            "touchpoint_type": "goodnight",
            "message_english": "Good night {nickname}! Sleep well. Your family loves you.",
            "button_options": [
                {"emoji": "🌙", "text_english": "Good night", "action": "goodnight_ok"},
            ],
            "include_voice_invite": False,
            "is_health_flow":       False,
        }
    )
    return tps


async def _log_concerns(
    db,
    parent_id: str,
    concerns: list[str],
    severity: str | None,
) -> None:
    """Upsert concerns into concern_log, incrementing frequency for repeats."""
    today = date.today().isoformat()
    for concern in concerns:
        try:
            existing = (
                db.table("concern_log")
                .select("id, frequency")
                .eq("parent_id", parent_id)
                .eq("concern_text", concern)
                .eq("is_resolved", False)
                .execute()
                .data
            )
            if existing:
                db.table("concern_log").update(
                    {
                        "last_seen": today,
                        "frequency": existing[0]["frequency"] + 1,
                        "severity":  severity,
                    }
                ).eq("id", existing[0]["id"]).execute()
            else:
                db.table("concern_log").insert(
                    {
                        "parent_id":    parent_id,
                        "concern_text": concern,
                        "severity":     severity,
                        "first_seen":   today,
                        "last_seen":    today,
                        "frequency":    1,
                    }
                ).execute()
        except Exception as e:
            logger.warning(f"concern_log upsert failed for '{concern}': {e}")


async def _create_urgent_alert(
    db,
    parent: dict,
    health_data: dict,
    raw_text: str,
) -> None:
    """Create an emergency alert and notify all family children immediately.

    Args:
        db:          Supabase client.
        parent:      Full parent row (must have family_id or families).
        health_data: Extracted health dict (urgency_flag, raw_summary, …).
        raw_text:    English text that triggered the alert.
    """
    parent_id = parent["id"]
    family_id = parent.get("family_id") or (
        (parent.get("families") or {}).get("id")
    )
    nickname  = parent.get("nickname", "parent")

    if not family_id:
        logger.warning(f"No family_id for urgent alert — parent {parent_id}")
        return

    try:
        db.table("alerts").insert(
            {
                "family_id": family_id,
                "parent_id": parent_id,
                "type":      "emergency",
                "message":   f"URGENT: {nickname} may need immediate attention. {raw_text[:200]}",
                "context":   health_data,
            }
        ).execute()

        children = (
            db.table("children")
            .select("phone, name")
            .eq("family_id", family_id)
            .execute()
            .data or []
        )
        for child in children:
            await whatsapp.send_message(
                child["phone"],
                f"🚨 *AYANA URGENT ALERT — {nickname}*\n\n"
                f"{nickname} may need immediate attention.\n\n"
                f"What they said: _{raw_text[:200]}_\n\n"
                f"Please call them or check in right away.",
            )
        logger.warning(
            f"Emergency alert fired for parent {parent_id} → {len(children)} child(ren)"
        )
    except Exception as e:
        logger.error(f"Urgent alert creation failed for {parent_id}: {e}")


async def _create_pain_alert(
    db,
    parent: dict,
    pain_location: str,
    severity: str,
) -> None:
    """Create a pain-severity alert and notify children.

    Args:
        db:            Supabase client.
        parent:        Full parent row.
        pain_location: Body location string (e.g. "head_chest", "joints").
        severity:      "mild" | "moderate" | "severe".
    """
    parent_id = parent["id"]
    family_id = parent.get("family_id") or (
        (parent.get("families") or {}).get("id")
    )
    nickname  = parent.get("nickname", "parent")

    if not family_id:
        return

    alert_type = "severe_pain" if severity == "severe" else "concern_pattern"
    emoji      = "🚨" if severity == "severe" else "⚠️"

    try:
        db.table("alerts").insert(
            {
                "family_id": family_id,
                "parent_id": parent_id,
                "type":      alert_type,
                "message":   f"{nickname} reported {severity} {pain_location} pain",
                "context":   {"location": pain_location, "severity": severity},
            }
        ).execute()

        children = (
            db.table("children")
            .select("phone")
            .eq("family_id", family_id)
            .execute()
            .data or []
        )
        for child in children:
            follow_up = (
                "Please call them right away."
                if severity == "severe"
                else "I'll monitor and follow up tomorrow."
            )
            await whatsapp.send_message(
                child["phone"],
                f"{emoji} *{nickname}* reported *{severity}* "
                f"{pain_location.replace('_', ' ')} pain during today's check-in.\n\n"
                f"{follow_up}",
            )
    except Exception as e:
        logger.error(f"Pain alert failed for {parent_id}: {e}")


def _upsert_health_flow(
    db,
    parent_id: str,
    condition: str,
    state: str,
    details: dict,
) -> None:
    """Create or update a health_flow record."""
    try:
        existing = (
            db.table("health_flows")
            .select("id")
            .eq("parent_id", parent_id)
            .eq("condition", condition)
            .neq("state", "resolved")
            .execute()
            .data
        )
        if existing:
            db.table("health_flows").update(
                {"state": state, "details": details}
            ).eq("id", existing[0]["id"]).execute()
        else:
            db.table("health_flows").insert(
                {
                    "parent_id": parent_id,
                    "condition": condition,
                    "state":     state,
                    "details":   details,
                }
            ).execute()
    except Exception as e:
        logger.warning(f"health_flow upsert failed ({parent_id}/{condition}): {e}")


def _upsert_concern_sync(
    db,
    parent_id: str,
    concern_text: str,
    category: str,
    severity: str,
) -> None:
    """Synchronous version of concern upsert (called from sync context)."""
    today = date.today().isoformat()
    existing = (
        db.table("concern_log")
        .select("id, frequency")
        .eq("parent_id", parent_id)
        .eq("concern_text", concern_text)
        .eq("is_resolved", False)
        .execute()
        .data
    )
    if existing:
        db.table("concern_log").update(
            {
                "last_seen": today,
                "frequency": existing[0]["frequency"] + 1,
                "severity":  severity,
            }
        ).eq("id", existing[0]["id"]).execute()
    else:
        db.table("concern_log").insert(
            {
                "parent_id":    parent_id,
                "concern_text": concern_text,
                "category":     category,
                "severity":     severity,
                "first_seen":   today,
                "last_seen":    today,
                "frequency":    1,
            }
        ).execute()


def _action_to_mood(action: str) -> str | None:
    """Map a button action string or free text to a mood value."""
    for key, mood in _ACTION_MOOD.items():
        if key in action:
            return mood
    if any(w in action for w in ("not well", "unwell", "bad", "sick", "pain")):
        return "not_well"
    if any(w in action for w in ("fine", "great", "good", "well")):
        return "good"
    if "okay" in action or "ok" in action:
        return "okay"
    return None


def _is_not_well(action: str, mood: str | None) -> bool:
    """Return True if the response indicates the parent is unwell."""
    return mood == "not_well" or "not_well" in action or "mood_bad" in action


def _is_button_action(text: str) -> bool:
    """Return True if text looks like a known button action ID."""
    prefixes = (
        "mood_", "medicine_", "pain_", "severity_", "goodnight",
        "btn_", "voice_",
    )
    return any(text.startswith(p) for p in prefixes)


def _get_medicine_group_from_tp(db, parent_id: str, touchpoint_type: str) -> str | None:
    """Look up the medicine group ID associated with a touchpoint type.

    Maps touchpoint_type (e.g. 'medicine_after_food') back to the
    anchor_event and finds the matching medicine_group for this parent.

    Returns:
        UUID string of the medicine group, or None if not found.
    """
    tp_to_anchor = {v: k for k, v in _ANCHOR_TO_TP.items()}
    anchor = tp_to_anchor.get(touchpoint_type)
    if not anchor:
        return None
    try:
        rows = (
            db.table("medicine_groups")
            .select("id")
            .eq("parent_id", parent_id)
            .eq("anchor_event", anchor)
            .limit(1)
            .execute()
            .data
        )
        return rows[0]["id"] if rows else None
    except Exception:
        return None