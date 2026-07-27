"""
Hirdetési fiók (ad_accounts tábla) adathozzáférés.

Az ad_accounts tábla köti össze az ügyfeleket a Meta/Google hirdetési fiókokkal.
Egy ügyfélnek több fiókja is lehet (pl. Meta + Google, vagy több Meta fiók).

Az auto-discovery innen indul: a discovery modul lekéri az összes aktív
ad_account-ot, majd ezekre futtatja a Meta/Google API hívásokat.

Függvények:
    get_ad_accounts_for_client(client_id, platform, active_only)
    get_ad_account(ad_account_id)
    get_ad_account_by_external_id(platform, external_account_id)
    get_or_create_ad_account(client_id, platform, external_account_id, account_name)
"""
from __future__ import annotations

from typing import Any

from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

_TABLE = "ad_accounts"

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Lekérdezések
# ---------------------------------------------------------------------------

def get_ad_accounts_for_client(
    client_id: int,
    *,
    platform: str | None = None,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """Egy ügyfél összes hirdetési fiókja.

    Paraméterek:
        client_id   — ügyfél DB ID
        platform    — szűrés platform szerint ('meta' | 'google'), ha None: mindkettő
        active_only — csak aktív fiókok (is_active = true)
    """
    query = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("client_id", client_id)
        .order("platform")
    )
    if platform is not None:
        query = query.eq("platform", platform)
    if active_only:
        query = query.eq("is_active", True)
    return query.execute().data or []


def get_ad_account(ad_account_id: int) -> dict[str, Any] | None:
    """Egy hirdetési fiók lekérdezése belső ID alapján."""
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("id", ad_account_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_ad_account_by_external_id(
    platform: str,
    external_account_id: str,
) -> dict[str, Any] | None:
    """Egy hirdetési fiók lekérdezése platform + külső fiók ID alapján.

    Pl.: get_ad_account_by_external_id('meta', 'act_123456789')
    """
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("platform", platform)
        .eq("external_account_id", external_account_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


# ---------------------------------------------------------------------------
# Írás
# ---------------------------------------------------------------------------

def get_or_create_ad_account(
    client_id: int,
    platform: str,
    external_account_id: str,
    *,
    account_name: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Hirdetési fiók lekérdezése, vagy létrehozása ha még nincs.

    Idempotens: ha a (platform, external_account_id) pár már létezik,
    visszaadja a meglévő sort (created=False).

    Visszatérés: (ad_account_dict, created)
        created=True  → most hozta létre
        created=False → már létezett
    """
    existing = get_ad_account_by_external_id(platform, external_account_id)
    if existing:
        return existing, False

    payload: dict[str, Any] = {
        "client_id": client_id,
        "platform": platform,
        "external_account_id": external_account_id,
        "is_active": True,
    }
    if account_name:
        payload["account_name"] = account_name

    res = get_supabase().table(_TABLE).insert(payload).execute()
    row = res.data[0]

    log.info(
        "Hirdetési fiók létrehozva: %s / %s (db_id=%s)",
        platform, external_account_id, row["id"],
    )
    return row, True


def list_ad_accounts(*, active_only: bool = False) -> list[dict[str, Any]]:
    """Az összes hirdetési fiók (minden ügyfélé), client_id szerint rendezve.

    A `/client list` overview használja: egy lekérdezéssel beolvassa az összes
    fiókot, majd a parancs-réteg ügyfelenként aggregálja (nincs N+1 query).
    """
    query = get_supabase().table(_TABLE).select("*").order("client_id")
    if active_only:
        query = query.eq("is_active", True)
    return query.execute().data or []


def set_ad_account_active(ad_account_id: int, is_active: bool) -> dict[str, Any] | None:
    """Hirdetési fiók aktív/inaktív állapotának állítása (soft delete).

    A `/adaccount remove` ezt használja `is_active=False`-szal — fizikai törlés
    NEM történik, az előzmény-adat (kampányok, insightok) megmarad.

    Visszaadja a frissített sort, vagy None-t, ha nincs ilyen fiók.
    """
    res = (
        get_supabase()
        .table(_TABLE)
        .update({"is_active": is_active})
        .eq("id", ad_account_id)
        .execute()
    )
    return res.data[0] if res.data else None


def set_account_name(ad_account_id: int, account_name: str) -> dict[str, Any] | None:
    """Fiók megjelenítési nevének (account_name) beállítása/frissítése.

    A `scripts/backfill_account_names.py` használja a régebbi (account_name
    IS NULL) fiókok visszamenőleges kitöltésére, API-ból lekért névvel.

    Visszaadja a frissített sort, vagy None-t, ha nincs ilyen fiók.
    """
    res = (
        get_supabase()
        .table(_TABLE)
        .update({"account_name": account_name})
        .eq("id", ad_account_id)
        .execute()
    )
    return res.data[0] if res.data else None


def find_ad_accounts_by_external_id(candidates: list[str]) -> list[dict[str, Any]]:
    """Hirdetési fiók(ok) keresése külső azonosító alapján, platform NÉLKÜL.

    A `/adaccount remove account_id:<>` csak a külső azonosítót kapja meg
    (platform nélkül), és a felhasználó többféleképp is beírhatja
    (act_ prefixszel/anélkül, Google customer ID kötőjelekkel/anélkül).
    Ezért a hívó több normalizált változatot ad át, és bármelyikre illeszkedő
    sort visszaadunk. Általában 0 vagy 1 találat (a (platform, external) pár
    egyedi, de két platform elvileg oszthat azonos stringet).
    """
    if not candidates:
        return []
    # Duplikátumok kiszűrése a lekérdezés előtt.
    uniq = list(dict.fromkeys(c for c in candidates if c))
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .in_("external_account_id", uniq)
        .execute()
    )
    return res.data or []


def normalize_external_account_id(platform: str, raw: str) -> str:
    """Külső fiók-azonosító normalizálása platformonként.

    - meta:   biztosítja az `act_` prefixet (act_123456789). Ha a felhasználó
              prefix nélkül (123456789) vagy prefixszel írta, egységes lesz.
    - google: kötőjelek eltávolítása (123-456-7890 → 1234567890).
    - egyéb:  csak whitespace-trim.

    Ez a kanonikus tárolási forma — így a discovery és a duplikáció-ellenőrzés
    konzisztens marad, függetlenül attól, hogy a felhasználó hogyan gépelte be.
    """
    value = (raw or "").strip()
    if platform == "meta":
        digits = value[4:] if value.lower().startswith("act_") else value
        return f"act_{digits}"
    if platform == "google":
        return value.replace("-", "")
    return value
