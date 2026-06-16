"""
Hozzárendelés (assignments tábla) adathozzáférés.

Az assignments tábla dönti el, KI KAP riasztást egy adott ügyfélről vagy
kampányról. Két szint létezik:
  - ügyfél-szintű: client_id kitöltve, campaign_id NULL  → az egész ügyfélért
  - kampány-szintű: campaign_id kitöltve                 → csak az adott kampányért

Egy felhasználónak egy ügyfélhez / kampányhoz csak EGY aktív hozzárendelése
lehet — a `create_assignment` ellenőrzi ezt, és duplikátum esetén a meglévőt
adja vissza (idempotens viselkedés).
"""
from __future__ import annotations

from typing import Any

from src.storage.supabase_client import get_supabase

_TABLE = "assignments"


# ---------------------------------------------------------------------------
# Lekérdezések
# ---------------------------------------------------------------------------

def get_assignments_for_client(client_id: int) -> list[dict[str, Any]]:
    """Egy ügyfélhez rendelt összes hozzárendelés (ügyfél-szintű sorok).

    Ez nem tartalmazza a kampány-szintű hozzárendeléseket — azokhoz
    `get_assignments_for_campaign` kell.
    """
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*, users(id, discord_user_id, display_name, alerts_channel_id)")
        .eq("client_id", client_id)
        .is_("campaign_id", "null")
        .execute()
    )
    return res.data or []


def get_assignments_for_campaign(campaign_id: int) -> list[dict[str, Any]]:
    """Egy kampányhoz rendelt összes hozzárendelés (kampány-szintű sorok)."""
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*, users(id, discord_user_id, display_name, alerts_channel_id)")
        .eq("campaign_id", campaign_id)
        .execute()
    )
    return res.data or []


def get_clients_for_user(user_id: int) -> list[dict[str, Any]]:
    """Egy felhasználóhoz rendelt összes ügyfél-szintű hozzárendelés.

    Visszaad egy listát, ahol minden elem egy assignment sor,
    a clients tábla adataival kibővítve (JOIN-olt lekérdezés).
    """
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*, clients(id, name, is_active, discord_channel_id)")
        .eq("user_id", user_id)
        .is_("campaign_id", "null")
        .execute()
    )
    return res.data or []


def get_campaign_ids_for_user(user_id: int) -> list[int]:
    """Egy felhasználóhoz tartozó ÖSSZES kampány ID (kampány- + ügyfél-szintű).

    - kampány-szintű hozzárendelés (campaign_id) → közvetlenül
    - ügyfél-szintű hozzárendelés (client_id)    → kibővítve az ügyfél
      összes kampányára (clients → ad_accounts → campaigns)

    A napi/heti összefoglaló ezt használja: "mely kampányokért felel a user".
    """
    sb = get_supabase()
    rows = (
        sb.table(_TABLE)
        .select("campaign_id, client_id")
        .eq("user_id", user_id)
        .execute()
        .data
        or []
    )

    campaign_ids: set[int] = set()
    client_ids: set[int] = set()
    for r in rows:
        if r.get("campaign_id") is not None:
            campaign_ids.add(r["campaign_id"])
        elif r.get("client_id") is not None:
            client_ids.add(r["client_id"])

    # Ügyfél-szintű hozzárendelések kibővítése kampányokra.
    if client_ids:
        accounts = (
            sb.table("ad_accounts")
            .select("id")
            .in_("client_id", list(client_ids))
            .execute()
            .data
            or []
        )
        account_ids = [a["id"] for a in accounts]
        if account_ids:
            camps = (
                sb.table("campaigns")
                .select("id")
                .in_("ad_account_id", account_ids)
                .execute()
                .data
                or []
            )
            for c in camps:
                campaign_ids.add(c["id"])

    return sorted(campaign_ids)


def get_assignment(
    user_id: int,
    *,
    client_id: int | None = None,
    campaign_id: int | None = None,
) -> dict[str, Any] | None:
    """Egy konkrét hozzárendelés lekérdezése. None ha nincs.

    Pontosan az egyik (client_id VAGY campaign_id) legyen megadva.
    """
    query = get_supabase().table(_TABLE).select("*").eq("user_id", user_id)
    if client_id is not None:
        query = query.eq("client_id", client_id).is_("campaign_id", "null")
    elif campaign_id is not None:
        query = query.eq("campaign_id", campaign_id)
    else:
        raise ValueError("client_id vagy campaign_id szükséges")

    res = query.limit(1).execute()
    return res.data[0] if res.data else None


# ---------------------------------------------------------------------------
# Írás
# ---------------------------------------------------------------------------

def create_assignment(
    user_id: int,
    *,
    client_id: int | None = None,
    campaign_id: int | None = None,
    role: str = "primary",
    created_by_discord_user_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Hozzárendelés létrehozása.

    Visszatérési érték: (assignment_dict, created)
        created=True  → most hozta létre (vagy a role-t frissítette)
        created=False → már létezett ugyanazzal a role-lal (idempotens)

    Pontosan az egyik (client_id VAGY campaign_id) legyen megadva.
    A `role` 'primary' vagy 'supporter'. Ha a hozzárendelés már létezik, de más
    role-lal, akkor a role frissül (created=True jelzi a változást).
    """
    if client_id is None and campaign_id is None:
        raise ValueError("client_id vagy campaign_id szükséges")

    # Duplikáció-ellenőrzés
    existing = get_assignment(user_id, client_id=client_id, campaign_id=campaign_id)
    if existing:
        if existing.get("role") == role:
            return existing, False
        # Role változott → frissítjük a meglévő sort.
        updated = (
            get_supabase()
            .table(_TABLE)
            .update({"role": role})
            .eq("id", existing["id"])
            .execute()
        )
        return (updated.data[0] if updated.data else {**existing, "role": role}), True

    payload: dict[str, Any] = {"user_id": user_id, "role": role}
    if client_id is not None:
        payload["client_id"] = client_id
    if campaign_id is not None:
        payload["campaign_id"] = campaign_id
    if created_by_discord_user_id is not None:
        payload["created_by_discord_user_id"] = created_by_discord_user_id

    res = get_supabase().table(_TABLE).insert(payload).execute()
    return res.data[0], True


def delete_assignment(
    user_id: int,
    *,
    client_id: int | None = None,
    campaign_id: int | None = None,
) -> bool:
    """Hozzárendelés törlése.

    Visszatér True-val ha volt mit törölni, False-szal ha nem létezett.
    Pontosan az egyik (client_id VAGY campaign_id) legyen megadva.
    """
    existing = get_assignment(user_id, client_id=client_id, campaign_id=campaign_id)
    if not existing:
        return False

    query = (
        get_supabase()
        .table(_TABLE)
        .delete()
        .eq("id", existing["id"])
    )
    query.execute()
    return True
