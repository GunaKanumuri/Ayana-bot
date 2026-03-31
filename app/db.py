"""Supabase client singleton."""

from supabase import create_client, Client
from app.config import settings

_client: Client | None = None


def get_db() -> Client:
    """Get Supabase client. Uses service key for full access."""
    global _client
    if _client is None:
        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    return _client
