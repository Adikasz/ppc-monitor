"""
Ügyfél (clients tábla) adathozzáférés.

Egyszerű, modul-szintű függvények a Supabase kliens körül. Dict-eket adnak
vissza úgy, ahogy a `supabase-py` is teszi — nincs külön modell-réteg, így
később ha bevezetünk pydantic modelleket, központilag lehet bekötni.
"""
from __future__ import annotations

from typing import Any

from src.storage.supabase_client import get_supabase

# A tábla neve egy helyen, hogy ne legyen elgépelési hibás kis lekérdezés.
_TABLE = "clients"


def list_clients(*, active_only: bool = True) -> list[dict[str, Any]]:
    """Listázza az ügyfeleket név szerint rendezve.

    Alapból csak az aktívakat (is_active = true), mert a UI tipikusan ezeket
    mutatja. Adminisztratív listához érdemes `active_only=False`-szal hívni.
    """
    query = get_supabase().table(_TABLE).select("*").order("name")
    if active_only:
        query = query.eq("is_active", True)
    return query.execute().data or []


def get_client(client_id: int) -> dict[str, Any] | None:
    """Egy ügyfél lekérdezése ID alapján. None ha nincs ilyen."""
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("id", client_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_client_by_name(name: str) -> dict[str, Any] | None:
    """Egy ügyfél lekérdezése pontos név alapján. Duplikáció-ellenőrzéshez."""
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("name", name)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def create_client(
    name: str,
    *,
    discord_channel_id: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Új ügyfél létrehozása. Visszaadja a beszúrt sort.

    A `name` egyedi a sémában — duplikátum esetén a Supabase 23505 hibát dob,
    a hívó (parancs-réteg) felelős a felhasználóbarát üzenetért.
    """
    payload: dict[str, Any] = {"name": name}
    if discord_channel_id is not None:
        payload["discord_channel_id"] = discord_channel_id
    if notes is not None:
        payload["notes"] = notes

    res = get_supabase().table(_TABLE).insert(payload).execute()
    return res.data[0]
