"""Gemini AI service — the intelligence layer.

Sarvam handles language. Gemini handles understanding.
All Gemini calls return structured JSON.
"""

import json
import logging
import google.generativeai as genai
from app.config import settings
from app.models.schemas import HealthExtraction, RoutineExtraction

logger = logging.getLogger(__name__)

_model = None


def _get_model():
    global _model
    if _model is None:
        genai.configure(api_key=settings.GEMINI_API_KEY)
        _model = genai.GenerativeModel("gemini-1.5-flash")
    return _model


def _parse_json(text: str) -> dict:
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
    """Extract structured routine from child's natural language description.
    
    Input: "She wakes up around 6, takes a gas tablet before tea..."
    Output: Structured routine with medicines, activities, conditions.
    """
    model = _get_model()
    
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
            "display_name": "what the parent calls it, in simple terms like 'gas maatra', 'BP tablet'",
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
        response = model.generate_content(prompt)
        data = _parse_json(response.text)
        return RoutineExtraction(**data)
    except Exception as e:
        logger.error(f"Routine extraction failed: {e}")
        return RoutineExtraction()


# ═══════════════ HEALTH EXTRACTION ═══════════════

async def extract_health(
    text: str,
    context: dict | None = None,
) -> HealthExtraction:
    """Extract structured health data from parent's reply (transcribed voice note or button text).
    
    Args:
        text: English text (already translated from native language)
        context: Optional context {active_health_flows, recent_concerns, etc.}
    """
    model = _get_model()
    
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
        response = model.generate_content(prompt)
        data = _parse_json(response.text)
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
    """Generate message variations for a touchpoint.
    
    Returns list of English messages. Each will be translated at send time.
    """
    model = _get_model()
    
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
    
    prompt = f"""Generate {count} message variations for a WhatsApp caregiving bot.

The bot talks to an elderly parent. The child set this up. The parent's nickname is "{parent_nickname}".
The parent's activities include: {', '.join(activities) if activities else 'general household'}
The parent's conditions include: {', '.join(conditions) if conditions else 'none known'}

Touchpoint: {touchpoint}
Description: {desc}

Rules:
- Write in English (will be translated to parent's language later)
- Use {{nickname}} as placeholder for the parent's name
- Sound like a caring child, NOT a robot or a health system
- Keep each message under 30 words
- Vary the wording significantly between variations
- For morning_greeting: some ask about sleep, some about mood, some are just warm hellos
- For goodnight: some say "child misses you", some wish sweet dreams, some are playful
- Reference their actual activities when relevant (plants, temple, walk, shop, etc.)
- NEVER sound clinical. "How are you?" not "How is your health status?"

Return ONLY a JSON array of strings:
[
    "Good morning {{nickname}}! How did you sleep?",
    "{{nickname}}, hope you had your tea! How are you feeling today?",
    ...
]"""

    try:
        response = model.generate_content(prompt)
        data = _parse_json(response.text)
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
    """Plan today's conversation touchpoints for a parent.
    
    Returns ordered list of touchpoints with timing and content guidance.
    """
    model = _get_model()
    
    prompt = f"""You are planning today's conversation for an elderly parent care bot.

Parent profile: {json.dumps(parent_profile)}
Active health issues: {json.dumps(active_health_flows)}
Yesterday's context: {json.dumps(yesterday_context or {})}
Medicine groups: {json.dumps(medicine_groups)}
Special dates today: {json.dumps(special_dates)}
Child's name: {child_name}

Generate today's touchpoints. Return ONLY valid JSON array:
[
    {{
        "touchpoint_type": "morning_greeting",
        "time_slot": "08:00",
        "message_english": "Good morning {{nickname}}! How are you feeling today?",
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
- If health flow is active (e.g., fever), morning_greeting should ask about THAT condition, not generic mood
- Medicine touchpoints only for groups that exist in the schedule
- Before-food medicine comes BEFORE food_check
- After-food medicine comes AFTER food_check (and only if parent confirms eating)
- Evening check-in should ask about their day naturally, with pain option
- Always end with "anything_else" touchpoint (voice invite)
- Always include "goodnight" as last touchpoint
- If yesterday they mentioned pain, today should follow up on it
- If there's a special date (birthday, festival), weave it into the greeting
- Maximum 6-7 touchpoints per day (don't over-message)
- Goodnight message should mention the child's name: "{child_name}"

Respond ONLY with the JSON array."""

    try:
        response = model.generate_content(prompt)
        data = _parse_json(response.text)
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
    """Analyze 7-14 days of data for patterns.
    
    Returns summary with patterns, recommendations, stats.
    """
    model = _get_model()
    
    prompt = f"""Analyze this elderly parent's health check-in data from the past week.

Parent nickname: {parent_nickname}
Check-ins: {json.dumps(checkins)}
Concerns logged: {json.dumps(concerns)}

Return ONLY valid JSON:
{{
    "summary": "One paragraph summary of the week",
    "mood_trend": "stable | improving | declining",
    "medicine_adherence_pct": 85,
    "concerns_flagged": ["knee pain mentioned 3 times", "skipped breakfast twice"],
    "recommendations": ["Consider doctor visit for knee pain", "Monitor breakfast habits"],
    "streak_info": "Responded 6 out of 7 days"
}}

Be caring, not clinical. Write like you're telling a family member, not a doctor."""

    try:
        response = model.generate_content(prompt)
        return _parse_json(response.text)
    except Exception as e:
        logger.error(f"Weekly analysis failed: {e}")
        return {"summary": "Unable to generate analysis", "mood_trend": "unknown"}
