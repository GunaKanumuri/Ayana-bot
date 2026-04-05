"""app/utils/token.py — Generate tamper-evident dashboard report tokens.

Tokens are base64url-encoded JSON payloads embedded in daily report links.
No cryptographic signing — security comes from the 7-day expiry and the fact
that family_id is a UUID (guessing one is effectively impossible).

The link points to the Next.js dashboard (DASHBOARD_URL), not the FastAPI
backend (APP_URL). Make sure to set DASHBOARD_URL in your Railway env vars.

Usage:
    from app.utils.token import make_report_token, make_report_url
    url = make_report_url(family_id)
    # → https://your-dashboard.vercel.app/report/eyJmYW1pbHlfaWQiOiJ...
"""

import base64
import json
from datetime import date, datetime, timedelta

from app.config import settings


def make_report_token(family_id: str, report_date: str | None = None) -> str:
    """Encode a report token for the dashboard URL.

    Args:
        family_id:   UUID of the family.
        report_date: ISO date string (default: today).

    Returns:
        URL-safe base64 string (no padding).
    """
    if not report_date:
        report_date = date.today().isoformat()

    payload = {
        "family_id": family_id,
        "date":      report_date,
        # expires in milliseconds (matches JS Date.now() in the dashboard)
        "expires":   int((datetime.utcnow() + timedelta(days=7)).timestamp() * 1000),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_report_url(family_id: str, report_date: str | None = None) -> str:
    """Return the full dashboard URL for embedding in WhatsApp reports.

    Uses DASHBOARD_URL (Vercel) — not APP_URL (Railway backend).

    Args:
        family_id:   UUID of the family.
        report_date: ISO date string (default: today).

    Returns:
        Full URL, e.g. https://ayana-dashboard.vercel.app/report/eyJ...
    """
    token = make_report_token(family_id, report_date)
    base  = settings.DASHBOARD_URL.rstrip("/")
    return f"{base}/report/{token}"