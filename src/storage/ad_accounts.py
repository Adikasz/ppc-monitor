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
