"""Gemini AI service — the intelligence layer.

Sarvam handles language. Gemini handles understanding.
All Gemini calls return structured JSON.

Uses google-genai SDK (google-genai package, NOT google-generativeai).
model="gemini-2.5-flash-preview-04-17",
"""

import asyncio
import json
import logging
from google import genai
from app.config import settings
from app.models.schemas import HealthExtraction, RoutineExtraction

logger = logging.getLogger(__name__)

_client = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


def _generate_sync(prompt: str) -> str:
    """Synchronous Gemini call — returns response text."""
    client = _get_client()
    response = client.models.generate_content(
       model="gemini-2.5-flash-preview-04-17",
        contents=prompt,
    )
    return response.text


async def _generate(prompt: str) -> str:
    """Async wrapper — runs sync Gemini call in thread pool so it doesn't block FastAPI."""
    return await asyncio.get_event_loop().run_in_executor(None, _generate_sync, prompt)


def _parse_json(text: str) -> dict | list:
    """Safely parse JSON from Gemini response, handling markdown fences."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError as e:
        logger.error(f"Gemini JSON parse error: {e}\nRaw: {text[:500]}")
        return {}


# ═══════════════ PROFILE EXTRACTION ═══════════════

async def extract_routine(description: str, parent_nickname: str = "") -> RoutineExtraction:
    """Extract structured routine from child's natural language description."""
    prompt = f"""You are extracting a parent's daily routine from their child's description.
The parent's nickname is "{parent_nickname}".

Description from the child:
"{description}"

Extract and return ONLY valid JSON with this structure:
{{
    "wake_time": "6:00",
    "medicines": [
        {{
            "name": "actual medicine name if mentioned, else generic like 'gas tablet'",
            "display_name": "what the parent calls it, in simple terms like 'gas tablet', 'BP tablet'",
            "timing": "before_food | with_food | after_food | before_tea | after_dinner | night | as_needed",
            "instructions": "empty stomach | after food | with water | etc",
            "time_estimate": "6:00 | 8:30 | 21:00"
        }}
    ],
    "activities": ["watering plants", "temple visit", "morning walk", "watching TV"],
    "conditions": ["BP", "diabetes", "cholesterol"],
    "alone_during_day": true,
    "meal_times": {{
        "tea": "6:30",
        "tiffin": "8:30",
        "lunch": "13:00",
        "dinner": "20:00"
    }},
    "notes": "any other relevant info"
}}

Rules:
- If medicine timing is "before food" or "empty stomach", timing = "before_food"
- If "after tiffin" or "after breakfast" or "after food", timing = "after_food"
- If "at night" or "after dinner", timing = "after_dinner"
- If "when needed" or "if pain", timing = "as_needed"
- display_name should be simple, colloquial — "BP tablet" not "Telmisartan 40mg"
- If specific times aren't mentioned, estimate based on typical Indian household routine
- alone_during_day = true if the child mentions parent is alone, lives alone, etc.

Respond ONLY with JSON. No explanation."""

    try:
        data = _parse_json(await _generate(prompt))
        return RoutineExtraction(**data)
    except Exception as e:
        logger.error(f"Routine extraction failed: {e}")
        return RoutineExtraction()


# ═══════════════ HEALTH EXTRACTION ═══════════════

async def extract_health(
    text: str,
    context: dict | None = None,
) -> HealthExtraction:
    """Extract structured health data from parent's reply."""
    context_str = json.dumps(context) if context else "{}"

    prompt = f"""You are analyzing a daily health check-in response from an elderly parent in India.

Parent's response (translated to English): "{text}"

Current context: {context_str}

Extract and return ONLY valid JSON:
{{
    "mood": "good | okay | not_well | null",
    "concerns": ["knee pain", "headache", "can't sleep"],
    "medicine_mentioned": false,
    "severity": "mild | moderate | severe | null",
    "urgency_flag": false,
    "follow_up_needed": false,
    "food_eaten": true,
    "raw_summary": "one line English summary of what they said"
}}

Rules:
- "body hot" or "hot feeling" = possible fever, flag as concern
- "haven't eaten" = food_eaten: false
- "can't bear the pain" or "very painful" = severity: severe, urgency_flag: true
- "a little pain" or "mild pain" = severity: mild
- If they mention any specific body part + pain, add to concerns
- urgency_flag = true ONLY for severe pain, breathing difficulty, chest pain, fall/injury
- follow_up_needed = true if any concern is mentioned (even mild)
- If the response is just "good" or positive, mood=good, empty concerns, no flags

Respond ONLY with JSON."""

    try:
        data = _parse_json(await _generate(prompt))
        return HealthExtraction(**data)
    except Exception as e:
        logger.error(f"Health extraction failed: {e}")
        return HealthExtraction(raw_summary=text)


# ═══════════════ MESSAGE VARIATIONS ═══════════════

async def generate_variations(
    touchpoint: str,
    parent_nickname: str,
    parent_profile: dict,
    count: int = 5,
) -> list[str]:
    """Generate message variations for a touchpoint."""
    activities = parent_profile.get("activities", [])
    conditions = parent_profile.get("conditions", [])

    touchpoint_descriptions = {
        "morning_greeting": "Morning greeting asking how they are. Warm, like a child greeting their parent.",
        "food_check": "Asking if they ate their meal (tiffin/lunch/dinner). Casual, caring.",
        "medicine_before_food": "Reminding about medicine before food. Gentle, not nagging.",
        "medicine_after_food": "Asking if they took medicine after food. Simple confirmation.",
        "medicine_night": "Night medicine check. Brief, combined with dinner check.",
        "activity_check": f"Asking about their daily activities: {', '.join(activities)}. Interested, personal.",
        "evening_checkin": "Evening check — how was your day. Open-ended but with button options.",
        "anything_else": "Final question — anything else to share? Invites voice message.",
        "goodnight": "Sweet good night message. Uses child's warmth. Short, loving.",
    }

    desc = touchpoint_descriptions.get(touchpoint, "A caring check-in message.")

    # NOTE: {{nickname}} inside the f-string renders as {nickname} in the prompt text,
    # which is what we want Gemini to use as a literal placeholder instruction.
    prompt = f"""Generate {count} message variations for a WhatsApp caregiving bot.

The bot speaks AS the child to their elderly parent. It feels like the child themselves is checking in.
Parent's nickname: "{parent_nickname}"
Activities: {', '.join(activities) if activities else 'general household'}
Conditions: {', '.join(conditions) if conditions else 'none known'}

Touchpoint: {touchpoint}
Description: {desc}

VOICE RULES — this is critical:
- Write like a caring child texting their parent, NOT a health app
- Use spoken rhythm: short sentences, ellipsis for pauses ("{{nickname}}... how are you?")
- Always use the nickname — never say "you" without the name
- Add warmth: casual, loving, personal
- Vary daily — never repeat the same opening
- Under 25 words per message
- Reference their real life when possible (temple, plants, walk, TV)
- NEVER clinical: not "How is your health status" but "{{nickname}}... feeling tired today?"
- After fever: "Is the fever better today?" not "How is your fever today?"
- Morning: some start with "Good morning", some with their name, some with their activity
- Goodnight: warm, loving — "Rest well {{nickname}}..."
- CRITICAL: Write in CLEAN ENGLISH only. No romanized regional words (no "Subhodayam",
  "baagunnara", "maatra", etc.) — the system translates to the parent's language automatically.

Return ONLY a JSON array of strings. Use {{{{nickname}}}} placeholder:
[
    "Good morning {{{{nickname}}}}! Did you sleep well last night?",
    "{{{{nickname}}}}... how are you feeling today? I hope you are doing great!",
    ...
]"""

    try:
        data = _parse_json(await _generate(prompt))
        if isinstance(data, list):
            return data[:count]
        return []
    except Exception as e:
        logger.error(f"Variation generation failed: {e}")
        return [f"Good morning {{nickname}}! How are you today?"]


# ═══════════════ DAILY CONVERSATION PLANNER ═══════════════

async def plan_daily_conversation(
    parent_profile: dict,
    active_health_flows: list[dict],
    yesterday_context: dict | None,
    medicine_groups: list[dict],
    special_dates: list[dict],
    child_name: str = "",
) -> list[dict]:
    """Plan today's conversation touchpoints for a parent."""
    prompt = f"""You are planning today's conversation for an elderly parent care bot.

Parent profile: {json.dumps(parent_profile)}
Active health issues: {json.dumps(active_health_flows)}
Yesterday's context: {json.dumps(yesterday_context or {})}
Medicine groups: {json.dumps(medicine_groups)}
Special dates today: {json.dumps(special_dates)}
Child's name: {child_name}

Generate today's touchpoints. Return ONLY valid JSON array.

IMPORTANT — message_english must sound like a caring child texting their parent:
- Use ellipsis for natural pauses: "{{nickname}}... how are you today?"
- Short and warm, under 20 words
- Use {{nickname}} placeholder always
- Casual phrasing: "Did you have your breakfast?" not "Have you eaten?"
- Goodnight must mention child name warmly: "Rest well {{nickname}}... {child_name} misses you"
- CRITICAL: Write in CLEAN ENGLISH only. No romanized regional words (no "Subhodayam",
  "baagunnara", "tiffin ayyaka", etc.) — the system translates automatically.

Example touchpoints:
[
    {{
        "touchpoint_type": "morning_greeting",
        "time_slot": "08:00",
        "message_english": "Good morning {{nickname}}! Did you sleep well? How are you feeling today?",
        "button_options": [
            {{"emoji": "😊", "text_english": "Feeling good", "action": "mood_good"}},
            {{"emoji": "😐", "text_english": "Okay", "action": "mood_okay"}},
            {{"emoji": "😔", "text_english": "Not well", "action": "mood_bad"}}
        ],
        "include_voice_invite": false,
        "is_health_flow": false,
        "health_flow_id": null,
        "medicine_group_id": null
    }}
]

Rules:
- If health flow is active (e.g., fever), morning_greeting should ask about THAT condition
- Medicine touchpoints only for groups that exist in the schedule
- Before-food medicine comes BEFORE food_check
- After-food medicine comes AFTER food_check
- Always end with "anything_else" touchpoint (voice invite)
- Always include "goodnight" as last touchpoint
- Maximum 6-7 touchpoints per day
- Goodnight message should mention the child's name: "{child_name}"
- CRITICAL: touchpoint_type MUST be one of ONLY these exact values:
  morning_greeting, food_check, medicine_before_food, medicine_after_food,
  medicine_night, activity_check, evening_checkin, anything_else, goodnight
- Never use any other touchpoint_type value like "medicine_check" or "pain_check"

Respond ONLY with the JSON array."""

    try:
        data = _parse_json(await _generate(prompt))
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        logger.error(f"Conversation planning failed: {e}")
        return []


# ═══════════════ WEEKLY PATTERN ANALYSIS ═══════════════

async def analyze_weekly_patterns(
    checkins: list[dict],
    concerns: list[dict],
    parent_nickname: str,
) -> dict:
    """Analyze 7-14 days of data for patterns."""
    med_checkins = [c for c in checkins if c.get("touchpoint", "").startswith("medicine_")]
    med_total = len(med_checkins)
    med_taken = sum(
        1 for c in med_checkins
        if c.get("status") == "replied" and isinstance(c.get("medicine_taken"), dict)
        and c["medicine_taken"].get("taken")
    )
    med_missed = sum(1 for c in med_checkins if c.get("status") == "missed")
    med_adherence_pct = round(med_taken / med_total * 100) if med_total else None

    prompt = f"""Analyze this elderly parent's health check-in data from the past week.

Parent nickname: {parent_nickname}
Check-ins: {json.dumps(checkins)}
Concerns logged: {json.dumps(concerns)}
Medicine stats: {med_total} medicine check-ins, {med_taken} taken, {med_missed} missed, adherence={med_adherence_pct}%

Return ONLY valid JSON:
{{
    "summary": "One paragraph summary of the week",
    "mood_trend": "stable | improving | declining",
    "medicine_adherence_pct": {med_adherence_pct if med_adherence_pct is not None else 'null'},
    "concerns_flagged": ["knee pain mentioned 3 times", "skipped breakfast twice"],
    "recommendations": ["Consider doctor visit for knee pain", "Monitor breakfast habits"],
    "streak_info": "Responded 6 out of 7 days"
}}

Be caring, not clinical. Write like you're telling a family member, not a doctor."""

    try:
        return _parse_json(await _generate(prompt))
    except Exception as e:
        logger.error(f"Weekly analysis failed: {e}")
        return {"summary": "Unable to generate analysis", "mood_trend": "unknown"}


# ═══════════════ DAILY OBSERVATION (for reports) ═══════════════

async def generate_daily_observation(
    parent_nickname: str,
    mood: str | None,
    concerns: list[str],
    medicine_status: str,
    response_rate: str,
) -> str:
    """Generate a warm one-line AI observation for a parent's daily report."""
    prompt = f"""Write ONE warm, caring sentence about an elderly parent's day for their child.

Parent nickname: {parent_nickname}
Mood today: {mood or "unknown"}
Concerns: {", ".join(concerns) if concerns else "none"}
Medicine: {medicine_status}
Check-in responses: {response_rate}

Rules:
- ONE sentence only, max 25 words
- Warm and caring — like a family friend giving an update
- If mood is good: highlight the positive
- If mood is not_well: be empathetic but not alarming
- If concerns exist: mention them gently
- If they missed medicines: note it softly
- NEVER clinical or robotic
- Use {parent_nickname} in the sentence

Examples:
- "{parent_nickname} had a cheerful day — responded to every check-in with a smile"
- "{parent_nickname} mentioned some knee discomfort but otherwise had a steady day."
- "{parent_nickname} seems a bit tired today — might be good to give them a call."

Return ONLY the sentence, no quotes, no JSON."""

    try:
        result = (await _generate(prompt)).strip().strip('"').strip("'")
        if len(result) > 150:
            result = result[:147] + "..."
        return result
    except Exception as e:
        logger.warning(f"Daily observation generation failed: {e}")
        if mood == "good":
            return f"{parent_nickname} had a good day today 😊"
        elif mood == "not_well":
            return f"{parent_nickname} wasn't feeling great today — consider checking in."
        elif concerns:
            return f"{parent_nickname} mentioned some concerns today."
        return f"{parent_nickname}'s daily check-in is complete."