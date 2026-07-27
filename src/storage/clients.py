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


def find_client_by_name_ci(name: str) -> dict[str, Any] | None:
    """Egy ügyfél lekérdezése NÉV alapján, kis/nagybetű-FÜGGETLEN egyezéssel.

    A `clients.name` unique constraint case-SENSITIVE (`text unique`), tehát
    "stopvill" és "Stopvill" a DB szintjén nem ütközne — ez a függvény az
    APPLIKÁCIÓ szintjén véd a duplikátum ellen a `/account add` automatikus
    kliens-feloldásánál (a Meta/Google API fióknév kis/nagybetűzése eltérhet
    a DB-ben már rögzített klienstől).

    Az `ilike` mintaillesztő karaktereit (`%`, `_`) escapeljük, hogy PONTOS
    (nem részleges) egyezést kapjunk — ellentétben a `search_clients`-szel.
    """
    escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .ilike("name", escaped)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def create_client(
    name: str,
    *,
    contact_email: str | None = None,
    discord_channel_id: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Új ügyfél létrehozása. Visszaadja a beszúrt sort.

    A `name` egyedi a sémában — duplikátum esetén a Supabase 23505 hibát dob,
    a hívó (parancs-réteg) felelős a felhasználóbarát üzenetért.

    A `contact_email` opcionális — ide megy a CRITICAL ügyfél-email (lásd 0003
    migration + email router). `insights_enabled` szándékosan NINCS beállítva
    INSERT-kor: a DB DEFAULT true (0008 migration) gondoskodik róla, így a kód
    akkor is működik, ha a migráció még nem futott le.
    """
    payload: dict[str, Any] = {"name": name}
    if contact_email is not None:
        payload["contact_email"] = contact_email
    if discord_channel_id is not None:
        payload["discord_channel_id"] = discord_channel_id
    if notes is not None:
        payload["notes"] = notes

    res = get_supabase().table(_TABLE).insert(payload).execute()
    return res.data[0]


def set_client_active(client_id: int, active: bool) -> dict[str, Any] | None:
    """Az ügyfél `is_active` kapcsolójának állítása (offboard → false, reactivate → true).

    Visszaadja a frissített sort, vagy None-t, ha nincs ilyen ügyfél.
    """
    res = (
        get_supabase()
        .table(_TABLE)
        .update({"is_active": active})
        .eq("id", client_id)
        .execute()
    )
    return res.data[0] if res.data else None


def search_clients(query: str, *, active: bool | None = None, limit: int = 25) -> list[dict[str, Any]]:
    """Ügyfél autocomplete-hez: név ILIKE keresés, opcionális is_active szűréssel.

    `active=None` → mind; `True` → csak aktív; `False` → csak inaktív (reactivate-hez).
    Visszatérés elemei: {"id", "name", "is_active"} (név szerint rendezve).
    """
    q = (query or "").strip()
    qb = get_supabase().table(_TABLE).select("id, name, is_active").ilike("name", f"%{q}%")
    if active is not None:
        qb = qb.eq("is_active", active)
    return qb.order("name").limit(limit).execute().data or []


def set_insights_enabled(client_id: int, enabled: bool) -> dict[str, Any] | None:
    """Az ügyfél `insights_enabled` kapcsolójának frissítése.

    Visszaadja a frissített sort, vagy None-t, ha nincs ilyen ügyfél.
    Megjegyzés: a `insights_enabled` oszlopot a 0008 migration veszi fel — ha
    az még nem futott le, a Supabase hibát dob, amit a parancs-réteg kezel.
    """
    res = (
        get_supabase()
        .table(_TABLE)
        .update({"insights_enabled": enabled})
        .eq("id", client_id)
        .execute()
    )
    return res.data[0] if res.data else None
