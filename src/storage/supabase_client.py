"""
Supabase kliens.

Egyetlen, megosztott kliens-példányt biztosít az egész alkalmazásnak.
A service_role kulcsot használja (backend oldali, teljes hozzáférés).
"""
from __future__ import annotations

from supabase import Client, create_client

from src.config import get_config

_client: Client | None = None


def get_supabase() -> Client:
    """Visszaadja a megosztott Supabase klienst (lazán inicializálva)."""
    global _client
    if _client is None:
        config = get_config()
        _client = create_client(
            config.supabase_url,
            config.supabase_service_key,
        )
    return _client
