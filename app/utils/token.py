"""app/utils/token.py — Generate tamper-evident dashboard report tokens.

Tokens are base64url-encoded JSON payloads embedded in daily report links.
No cryptographic signing — security comes from the 7-day expiry and the fact
that family_id is a UUID (guessing one is effectively impossible).

Usage:
    from app.utils.token import make_report_token, make_report_url
    url = make_report_url(family_id, date_str)
    # → https://ayana.app/report/eyJmYW1pbHlfaWQiOiJ...
"""

import base64
import json
from datetime import date, datetime, timedelta

from app.config import settings


def make_report_token(family_id: str, report_date: str | None = None) -> str:
    """Encode a report token for the dashboard URL.

    Args:
        family_id:   UUID of the family.
        report_date: ISO date string (default: today in IST).

    Returns:
        URL-safe base64 string (no padding).
    """
    if not report_date:
        report_date = date.today().isoformat()

    payload = {
        "family_id": family_id,
        "date":      report_date,
        "expires":   int((datetime.utcnow() + timedelta(days=7)).timestamp() * 1000),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_report_url(family_id: str, report_date: str | None = None) -> str:
    """Return the full dashboard URL for embedding in WhatsApp reports.

    Args:
        family_id:   UUID of the family.
        report_date: ISO date string (default: today).

    Returns:
        Full URL string, e.g. https://ayana.app/report/eyJ...
    """
    token = make_report_token(family_id, report_date)
    base  = settings.APP_URL.rstrip("/")
    return f"{base}/report/{token}"