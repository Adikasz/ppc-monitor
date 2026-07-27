"""
Felhasználó (users tábla) adathozzáférés.

A Discord bot nem kér regisztrációt — a felhasználók automatikusan jönnek létre
az első interakciójukkor (pl. amikor valaki /assign parancsot ad ki).
Az azonosítás mindig Discord user ID alapján történik (string, mert a Discord
snowflake ID-k 64 bites egészek, Python int-ként nem biztonságos JSON-ban).
"""
from __future__ import annotations

from typing import Any

from src.storage.supabase_client import get_supabase

_TABLE = "users"


def get_user_by_discord_id(discord_user_id: str) -> dict[str, Any] | None:
    """Felhasználó lekérdezése Discord ID alapján. None ha nincs ilyen."""
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("discord_user_id", discord_user_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_user_by_alerts_channel(channel_id: str | int) -> dict[str, Any] | None:
    """A user, akinek az `alerts_channel_id`-ja a megadott csatorna. None ha nincs.

    A 22. lépés csatorna-szkópolt `/my` parancsai ezzel azonosítják az "aktuális
    OM"-et: melyik OM saját #alerts csatornájából fut a parancs.
    """
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("alerts_channel_id", str(channel_id))
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_user(user_id: int) -> dict[str, Any] | None:
    """Felhasználó lekérdezése belső ID alapján. None ha nincs ilyen."""
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_or_create_user(
    discord_user_id: str,
    display_name: str,
) -> tuple[dict[str, Any], bool]:
    """Visszaadja a felhasználót, vagy létrehozza, ha még nem létezik.

    Visszatérési érték: (user_dict, created)
        created=True  → most hozta létre
        created=False → már létezett

    A `display_name` csak létrehozáskor kerül a DB-be; meglévő rekordnál
    nem frissítjük automatikusan (a felhasználó megtarthatja a régi nevét).
    """
    existing = get_user_by_discord_id(discord_user_id)
    if existing:
        return existing, False

    res = (
        get_supabase()
        .table(_TABLE)
        .insert({"discord_user_id": discord_user_id, "display_name": display_name})
        .execute()
    )
    return res.data[0], True


def set_user_alerts_channel(discord_user_id: int, channel_id: str) -> bool:
    """Beállítja a felhasználó személyes alert csatornáját (Discord channel ID).

    A `discord_user_id` a Discord snowflake ID (int) — a DB-ben stringként
    tároljuk, ezért itt konvertáljuk. Csak meglévő felhasználót frissít; ha
    nincs ilyen, False-szal tér vissza (a hívó előbb hozza létre a usert).

    Visszatérés: True, ha volt mit frissíteni, különben False.
    """
    res = (
        get_supabase()
        .table(_TABLE)
        .update({"alerts_channel_id": str(channel_id)})
        .eq("discord_user_id", str(discord_user_id))
        .execute()
    )
    return bool(res.data)


def get_user_alerts_channel(user_id: int) -> str | None:
    """A felhasználó személyes alert csatornája (belső ID alapján). None ha nincs."""
    user = get_user(user_id)
    if not user:
        return None
    return user.get("alerts_channel_id")


def list_users(*, active_only: bool = True) -> list[dict[str, Any]]:
    """Felhasználók listázása display_name szerint rendezve."""
    query = get_supabase().table(_TABLE).select("*").order("display_name")
    if active_only:
        query = query.eq("is_active", True)
    return query.execute().data or []
