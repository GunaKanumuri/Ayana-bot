"""engine/state.py — Shim for backward-compat with integration prompt imports.

The kit's integration prompt imports from engine/state, engine/health_flow,
engine/handle_reply. All logic lives in services/conversation.py.
These shims re-export so any import works regardless of path used.
"""

from app.services.conversation import (
    start_daily_conversation,
    send_touchpoint,
    handle_parent_response,
    handle_pain_followup,
    send_medicine_reminder,
    send_nudge,
)

__all__ = [
    "start_daily_conversation",
    "send_touchpoint",
    "handle_parent_response",
    "handle_pain_followup",
    "send_medicine_reminder",
    "send_nudge",
]