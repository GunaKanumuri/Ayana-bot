"""AYANA report generation — daily and weekly summaries sent to children.

Public API
──────────
  generate_daily_report(family_id)
    Pulls today's check_ins for every active parent in the family, formats
    a structured WhatsApp message, and sends it to every child.
    Respects family.report_format: "combined" (one message) or "separate"
    (one message per parent).

  generate_weekly_report(family_id)
    Pulls the last 7 days of check_ins + concern_log, calls
    gemini.analyze_weekly_patterns() for AI insights, and formats a rich
    weekly digest.

Report format (daily)
─────────────────────
  📊 AYANA Daily Report — 31 Mar

  👴 Appa
  😊 Mood: Good
  💊 Medicines: Morning ✓  Night ✓
  🍽️ Food: Had meals
  ✅ Responded: 4 / 5 check-ins
  ⚠️ Concerns: None

  ━━━━━━━━━

  👵 Amma
  😐 Mood: Okay
  💊 Medicines: Not confirmed
  ⚠️ Concerns: Mild knee pain (2× this week)

  ━━━━━━━━━
  Next check-in tomorrow at 8:00 AM

Report format (weekly)
──────────────────────
  📊 AYANA Weekly Report — 25–31 Mar

  👴 Appa
  📈 Mood trend: Stable
  💊 Medicine adherence: 85%
  ✅ Response rate: 6 / 7 days
  ⚠️ Patterns: Morning stiffness on 3 days
  💡 Appa has been doing well overall…
  🩺 Recommendation: Consider check-up for knee pain

  ━━━━━━━━━
"""

import logging
from datetime import date, timedelta

from app.db import get_db
from app.services.whatsapp import send_message

logger = logging.getLogger(__name__)

# Mood display
_MOOD_EMOJI = {"good": "😊", "okay": "😐", "not_well": "😔"}
_MOOD_LABEL = {"good": "Good", "okay": "Okay", "not_well": "Not well"}

# Gender-based avatar emoji heuristic (fallback to neutral)
_AVATAR_DEFAULT = "👴"

# Divider used between parent blocks in a combined report
_DIVIDER = "━━━━━━━━━"


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC: DAILY REPORT
# ═══════════════════════════════════════════════════════════════════════════════

async def generate_daily_report(family_id: str) -> None:
    """Generate and send today's health report to all children in the family.

    Args:
        family_id: UUID of the families row.
    """
    db    = get_db()
    today = date.today()

    # ── Load family metadata ────────────────────────────────────────────────────
    try:
        fam_rows = (
            db.table("families").select("*").eq("id", family_id).execute().data
        )
        if not fam_rows:
            logger.error(f"Family {family_id} not found")
            return
        family        = fam_rows[0]
        report_format = family.get("report_format", "combined")
    except Exception as e:
        logger.error(f"Family fetch failed ({family_id}): {e}")
        return

    # ── Load active parents ─────────────────────────────────────────────────────
    try:
        parents = (
            db.table("parents")
            .select("id, name, nickname, checkin_time")
            .eq("family_id", family_id)
            .eq("is_active", True)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error(f"Parents fetch failed ({family_id}): {e}")
        return

    if not parents:
        return

    # ── Load children (report recipients) ──────────────────────────────────────
    try:
        children = (
            db.table("children")
            .select("phone, name")
            .eq("family_id", family_id)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error(f"Children fetch failed ({family_id}): {e}")
        return

    if not children:
        return

    # ── Build per-parent summaries ──────────────────────────────────────────────
    parent_blocks: list[str] = []

    for parent in parents:
        try:
            block = await _build_daily_parent_block(db, parent, today)
            parent_blocks.append(block)
        except Exception as e:
            logger.error(
                f"Daily block build failed for {parent.get('nickname')}: {e}",
                exc_info=True,
            )

    if not parent_blocks:
        return

    date_label = today.strftime("%-d %b").lstrip("0") if hasattr(today, "strftime") else str(today)

    # ── Format and send ─────────────────────────────────────────────────────────
    if report_format == "separate":
        # One message per parent, per child
        for block, parent in zip(parent_blocks, parents):
            header = f"📊 *AYANA Daily Report — {date_label}*\n\n"
            footer = _daily_footer(parent)
            message = header + block + "\n\n" + footer
            for child in children:
                await _safe_send(child["phone"], message)

    else:
        # Combined: all parents in one message
        header    = f"📊 *AYANA Daily Report — {date_label}*\n\n"
        body      = f"\n\n{_DIVIDER}\n\n".join(parent_blocks)
        # Footer uses the first parent's checkin_time as a proxy
        footer    = _daily_footer(parents[0]) if parents else ""
        message   = header + body + "\n\n" + _DIVIDER + "\n" + footer
        for child in children:
            await _safe_send(child["phone"], message)

    logger.info(
        f"Daily report sent for family {family_id} "
        f"({len(parents)} parent(s) → {len(children)} child(ren))"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC: WEEKLY REPORT
# ═══════════════════════════════════════════════════════════════════════════════

async def generate_weekly_report(family_id: str) -> None:
    """Generate and send a 7-day health digest with AI pattern analysis.

    Args:
        family_id: UUID of the families row.
    """
    from app.services.gemini import analyze_weekly_patterns

    db    = get_db()
    today = date.today()
    since = (today - timedelta(days=6)).isoformat()
    today_iso = today.isoformat()

    # ── Load parents ────────────────────────────────────────────────────────────
    try:
        parents = (
            db.table("parents")
            .select("id, name, nickname, checkin_time")
            .eq("family_id", family_id)
            .eq("is_active", True)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error(f"[weekly] Parents fetch failed ({family_id}): {e}")
        return

    # ── Load children ────────────────────────────────────────────────────────────
    try:
        children = (
            db.table("children")
            .select("phone")
            .eq("family_id", family_id)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error(f"[weekly] Children fetch failed ({family_id}): {e}")
        return

    if not parents or not children:
        return

    # ── Build per-parent weekly blocks ──────────────────────────────────────────
    parent_blocks: list[str] = []

    for parent in parents:
        try:
            block = await _build_weekly_parent_block(
                db, parent, since, today_iso, analyze_weekly_patterns
            )
            parent_blocks.append(block)
        except Exception as e:
            logger.error(
                f"[weekly] Block build failed for {parent.get('nickname')}: {e}",
                exc_info=True,
            )

    if not parent_blocks:
        return

    start_label = (today - timedelta(days=6)).strftime("%-d %b")
    end_label   = today.strftime("%-d %b")
    header      = f"📊 *AYANA Weekly Report — {start_label}–{end_label}*\n\n"
    body        = f"\n\n{_DIVIDER}\n\n".join(parent_blocks)
    message     = header + body + f"\n\n{_DIVIDER}\n_AYANA — weekly summary_"

    for child in children:
        await _safe_send(child["phone"], message)

    logger.info(
        f"Weekly report sent for family {family_id} "
        f"({len(parents)} parent(s) → {len(children)} child(ren))"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PRIVATE: BUILD DAILY PARENT BLOCK
# ═══════════════════════════════════════════════════════════════════════════════

async def _build_daily_parent_block(db, parent: dict, today: date) -> str:
    """Build the formatted text block for one parent's daily report.

    Pulls today's check_ins and concern_log for this parent and formats
    mood, medicine status, food, response rate, and active concerns.

    Args:
        db:     Supabase client.
        parent: Parent row (id, nickname, checkin_time).
        today:  Report date.

    Returns:
        Formatted multi-line string ready to embed in the report message.
    """
    parent_id = parent["id"]
    nickname  = parent["nickname"]
    today_iso = today.isoformat()

    # ── Check-ins ───────────────────────────────────────────────────────────────
    checkins = (
        db.table("check_ins")
        .select("touchpoint, status, mood, concerns, medicine_taken, ai_extraction")
        .eq("parent_id", parent_id)
        .eq("date", today_iso)
        .execute()
        .data or []
    )

    total    = len(checkins)
    replied  = [c for c in checkins if c["status"] == "replied"]
    n_replied = len(replied)

    # ── Mood ────────────────────────────────────────────────────────────────────
    moods = [c["mood"] for c in replied if c.get("mood")]
    latest_mood = moods[-1] if moods else None
    mood_line   = (
        f"{_MOOD_EMOJI.get(latest_mood, '❓')} Mood: {_MOOD_LABEL.get(latest_mood, 'Unknown')}"
        if latest_mood
        else "❓ Mood: No response yet"
    )

    # ── Medicine status ─────────────────────────────────────────────────────────
    med_checkins = [c for c in checkins if c["touchpoint"].startswith("medicine_")]
    medicine_line = _format_medicine_status(med_checkins)

    # ── Food status (from ai_extraction.food_eaten) ─────────────────────────────
    food_statuses = [
        c["ai_extraction"].get("food_eaten")
        for c in replied
        if isinstance(c.get("ai_extraction"), dict)
        and c["ai_extraction"].get("food_eaten") is not None
    ]
    if food_statuses:
        if all(food_statuses):
            food_line = "🍽️ Food: Had meals"
        elif not any(food_statuses):
            food_line = "🍽️ Food: ⚠️ Mentioned not eating"
        else:
            food_line = "🍽️ Food: Partial"
    else:
        food_line = ""

    # ── Response rate ───────────────────────────────────────────────────────────
    response_line = f"✅ Responded: {n_replied} / {total} check-ins"

    # ── Active concerns from today ──────────────────────────────────────────────
    all_concerns: list[str] = []
    for c in replied:
        raw = c.get("concerns") or []
        if isinstance(raw, list):
            all_concerns.extend(raw)

    # Recent concern_log (for repeat count)
    week_ago = (today - timedelta(days=7)).isoformat()
    concern_log_rows = (
        db.table("concern_log")
        .select("concern_text, frequency, severity")
        .eq("parent_id", parent_id)
        .eq("is_resolved", False)
        .gte("last_seen", week_ago)
        .execute()
        .data or []
    )
    concern_freq: dict[str, int] = {
        r["concern_text"]: r["frequency"] for r in concern_log_rows
    }

    if all_concerns:
        unique = list(dict.fromkeys(all_concerns))[:4]
        concern_parts = []
        for c in unique:
            freq = concern_freq.get(c, 1)
            suffix = f" ({freq}× this week)" if freq > 1 else ""
            concern_parts.append(f"  • {c}{suffix}")
        concern_line = "⚠️ Concerns:\n" + "\n".join(concern_parts)
    else:
        concern_line = "✅ Concerns: None today"

    # ── AI Observation (warm one-liner from Gemini) ─────────────────────────────
    ai_observation = ""
    try:
        from app.services.gemini import generate_daily_observation
        ai_observation = await generate_daily_observation(
            parent_nickname=nickname,
            mood=latest_mood,
            concerns=all_concerns,
            medicine_status=medicine_line,
            response_rate=f"{n_replied}/{total}",
        )
        if ai_observation:
            ai_observation = f"\n💡 _{ai_observation}_"
    except Exception as e:
        logger.warning("AI observation generation failed for %s: %s", nickname, e)

    # ── Assemble block ──────────────────────────────────────────────────────────
    lines = [f"*{nickname}*", mood_line, medicine_line, food_line, response_line, concern_line]
    block = "\n".join(line for line in lines if line)
    if ai_observation:
        block += ai_observation
    return block


# ═══════════════════════════════════════════════════════════════════════════════
# PRIVATE: BUILD WEEKLY PARENT BLOCK
# ═══════════════════════════════════════════════════════════════════════════════

async def _build_weekly_parent_block(
    db,
    parent: dict,
    since: str,
    today_iso: str,
    analyze_fn,
) -> str:
    """Build the formatted text block for one parent's weekly report.

    Loads 7 days of check_ins and concern_log, calls Gemini analysis, and
    formats the result into a WhatsApp-friendly block.

    Args:
        db:         Supabase client.
        parent:     Parent row.
        since:      ISO date string 7 days ago.
        today_iso:  ISO date string for today.
        analyze_fn: gemini.analyze_weekly_patterns callable.

    Returns:
        Formatted multi-line string.
    """
    parent_id = parent["id"]
    nickname  = parent["nickname"]

    # ── 7-day check-ins ─────────────────────────────────────────────────────────
    checkins = (
        db.table("check_ins")
        .select("date, touchpoint, status, mood, concerns, ai_extraction")
        .eq("parent_id", parent_id)
        .gte("date", since)
        .lte("date", today_iso)
        .execute()
        .data or []
    )

    # ── Concern log ─────────────────────────────────────────────────────────────
    concerns = (
        db.table("concern_log")
        .select("concern_text, severity, frequency, first_seen, last_seen")
        .eq("parent_id", parent_id)
        .eq("is_resolved", False)
        .gte("last_seen", since)
        .execute()
        .data or []
    )

    # ── Quick stats ─────────────────────────────────────────────────────────────
    days_with_reply = len({c["date"] for c in checkins if c["status"] == "replied"})
    total_days      = 7
    moods           = [c["mood"] for c in checkins if c.get("mood")]
    good_pct        = round(moods.count("good") / len(moods) * 100) if moods else 0

    med_checkins = [c for c in checkins if c["touchpoint"].startswith("medicine_")]
    med_replied  = [c for c in med_checkins if c["status"] == "replied"]
    med_taken    = [
        c for c in med_replied
        if isinstance(c.get("ai_extraction"), dict)
        and c["ai_extraction"].get("medicine_mentioned")
    ]
    med_pct = round(len(med_taken) / len(med_checkins) * 100) if med_checkins else None

    # ── Gemini weekly analysis ──────────────────────────────────────────────────
    analysis: dict = {}
    try:
        analysis = await analyze_fn(checkins, concerns, nickname)
    except Exception as e:
        logger.warning(f"[weekly] Gemini analysis failed for {nickname}: {e}")

    mood_trend   = analysis.get("mood_trend", "stable")
    summary      = analysis.get("summary", "")
    patterns     = analysis.get("concerns_flagged", [])[:3]
    recs         = analysis.get("recommendations", [])[:2]
    streak_info  = analysis.get("streak_info", f"Responded {days_with_reply} / {total_days} days")

    trend_emoji = {"stable": "📊", "improving": "📈", "declining": "📉"}.get(
        mood_trend, "📊"
    )

    # ── Assemble block ──────────────────────────────────────────────────────────
    lines = [f"*{nickname}*"]
    lines.append(f"{trend_emoji} Mood trend: {mood_trend.title()}")
    if med_pct is not None:
        lines.append(f"💊 Medicine adherence: {med_pct}%")
    lines.append(f"✅ {streak_info}")

    if patterns:
        lines.append("⚠️ Patterns:")
        for p in patterns:
            lines.append(f"  • {p}")

    if summary:
        lines.append(f"\n_{summary}_")

    if recs:
        lines.append("\n💡 Suggestions:")
        for r in recs:
            lines.append(f"  • {r}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# PRIVATE: HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _format_medicine_status(med_checkins: list[dict]) -> str:
    """Build the medicine status line from check_in rows.

    Displays ✓ (taken) or ✗ (skipped / missed) per touchpoint.
    Maps touchpoint → short label.

    Args:
        med_checkins: List of check_in rows whose touchpoint starts with "medicine_".

    Returns:
        Formatted string like "💊 Medicines: Morning ✓  Night ✗"
        or "💊 Medicines: Not confirmed" if no medicine check-ins exist.
    """
    if not med_checkins:
        return "💊 Medicines: Not confirmed"

    tp_labels = {
        "medicine_before_food": "Morning",
        "medicine_after_food":  "Afternoon",
        "medicine_night":       "Night",
    }

    parts: list[str] = []
    for ci in med_checkins:
        tp    = ci.get("touchpoint", "")
        label = tp_labels.get(tp, tp.replace("medicine_", "").title())

        status = ci.get("status", "sent")
        mt     = ci.get("medicine_taken") or {}

        if status == "missed":
            icon = "✗"
        elif isinstance(mt, dict) and mt.get("taken") is True:
            icon = "✓"
        elif isinstance(mt, dict) and mt.get("action") == "medicine_skipped":
            icon = "✗"
        elif status == "replied":
            icon = "✓"  # replied but no explicit taken flag → assume taken
        else:
            icon = "⏳"

        parts.append(f"{label} {icon}")

    return "💊 Medicines: " + "  ".join(parts)


def _daily_footer(parent: dict) -> str:
    """Return the footer line for a daily report.

    Shows the next check-in time for the first parent in the report.

    Args:
        parent: Parent row with checkin_time.

    Returns:
        Formatted footer string.
    """
    ct = str(parent.get("checkin_time", "08:00"))[:5]
    return f"_Next check-in: tomorrow at {ct}_"


async def _safe_send(phone: str, message: str) -> None:
    """Send a WhatsApp message, swallowing exceptions so one failed send
    doesn't prevent other children from receiving the report.

    Args:
        phone:   Recipient phone number.
        message: Report text.
    """
    try:
        await send_message(phone, message)
    except Exception as e:
        logger.error(f"[report] Send failed to {phone}: {e}")
