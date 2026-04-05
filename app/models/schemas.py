"""Pydantic models for AYANA."""

from pydantic import BaseModel
from datetime import date, time, datetime
from typing import Optional


# ═══════════════ Onboarding ═══════════════

class OnboardingStart(BaseModel):
    child_phone: str
    child_name: str
    parent_phone: str
    parent_name: str
    parent_nickname: str
    language: str = "te"
    checkin_time: time = time(8, 0)


class RoutineDescription(BaseModel):
    parent_id: str
    description: str  # Natural language from child


# ═══════════════ Medicine ═══════════════

class MedicineGroupCreate(BaseModel):
    parent_id: str
    label: str
    anchor_event: str
    time_window: time


class MedicineCreate(BaseModel):
    group_id: str
    name: str
    display_name: str
    instructions: Optional[str] = None
    is_as_needed: bool = False
    trigger_symptom: Optional[str] = None


# ═══════════════ Check-in ═══════════════

class CheckInResponse(BaseModel):
    parent_phone: str
    touchpoint: str
    button_reply: Optional[str] = None
    voice_note_url: Optional[str] = None
    text_reply: Optional[str] = None


# ═══════════════ Health Extraction (Gemini output) ═══════════════

class HealthExtraction(BaseModel):
    mood: Optional[str] = None          # good, okay, not_well
    concerns: list[str] = []
    medicine_mentioned: bool = False
    severity: Optional[str] = None      # mild, moderate, severe
    urgency_flag: bool = False
    follow_up_needed: bool = False
    food_eaten: Optional[bool] = None
    raw_summary: str = ""


# ═══════════════ Routine Extraction (Gemini output) ═══════════════

class RoutineExtraction(BaseModel):
    wake_time: Optional[str] = None
    medicines: list[dict] = []          # [{name, display_name, timing, instructions, time_estimate}]
    activities: list[str] = []
    conditions: list[str] = []
    alone_during_day: bool = False
    meal_times: dict = {}               # {tea, tiffin/breakfast, lunch, dinner} from Gemini
    notes: str = ""


# ═══════════════ Conversation Touchpoint ═══════════════

class Touchpoint(BaseModel):
    touchpoint_type: str                # morning_greeting, food_check, etc.
    time_slot: time
    message_english: str
    button_options: list[dict]          # [{emoji, text_english, action}]
    include_voice_invite: bool = False
    is_health_flow: bool = False
    health_flow_id: Optional[str] = None
    medicine_group_id: Optional[str] = None


# ═══════════════ Report ═══════════════

class DailyReport(BaseModel):
    parent_name: str
    parent_nickname: str
    date: date
    mood: Optional[str] = None
    medicines_status: str = ""
    activities: str = ""
    concerns: list[str] = []
    patterns: list[str] = []
    checked_in: bool = False
    checkin_time: Optional[str] = None


class CombinedReport(BaseModel):
    date: date
    reports: list[DailyReport]
    overall_notes: list[str] = []