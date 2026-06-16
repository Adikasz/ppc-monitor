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


# ---------------------------------------------------------------------------
# Kliens-szintű kaszkád (15. lépés)
# ---------------------------------------------------------------------------

def _chunks(seq: list[Any], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _insert_assignment_rows(rows: list[dict[str, Any]]) -> None:
    """Bulk INSERT, az inherited_from_client oszlop hiányát toleráló degradációval.

    Ha a 0009 migration még nem futott le, a flag nélkül próbáljuk újra — így a
    kliens-assign a migráció előtt is működik (csak jelöletlen sorokkal).
    """
    if not rows:
        return
    sb = get_supabase()
    try:
        sb.table(_TABLE).insert(rows).execute()
    except Exception as exc:  # noqa: BLE001
        if "inherited_from_client" in str(exc).lower():
            stripped = [
                {k: v for k, v in r.items() if k != "inherited_from_client"} for r in rows
            ]
            sb.table(_TABLE).insert(stripped).execute()
        else:
            raise


def bulk_assign_campaigns(
    user_id: int,
    campaign_ids: list[int],
    *,
    role: str = "primary",
    created_by_discord_user_id: str | None = None,
) -> dict[str, int]:
    """A user kampány-szintű hozzárendelése a megadott kampányokhoz (öröklött).

    Idempotens: meglévő (user, campaign) párnál csak a role-t igazítja, ha eltér;
    egyébként új, inherited_from_client=true sort szúr be (bulk, csonkolva).

    Visszatérés: {"created", "updated", "total"}.
    """
    result = {"created": 0, "updated": 0, "total": len(campaign_ids)}
    if not campaign_ids:
        return result

    sb = get_supabase()

    existing_by_cid: dict[int, dict[str, Any]] = {}
    for chunk in _chunks(campaign_ids, 200):
        rows = (
            sb.table(_TABLE)
            .select("id, campaign_id, role")
            .eq("user_id", user_id)
            .in_("campaign_id", chunk)
            .execute()
            .data
            or []
        )
        for r in rows:
            existing_by_cid[r["campaign_id"]] = r

    to_insert: list[dict[str, Any]] = []
    for cid in campaign_ids:
        row = existing_by_cid.get(cid)
        if row is None:
            payload: dict[str, Any] = {
                "user_id": user_id,
                "campaign_id": cid,
                "role": role,
                "inherited_from_client": True,
            }
            if created_by_discord_user_id is not None:
                payload["created_by_discord_user_id"] = created_by_discord_user_id
            to_insert.append(payload)
        elif row.get("role") != role:
            sb.table(_TABLE).update({"role": role}).eq("id", row["id"]).execute()
            result["updated"] += 1

    for chunk in _chunks(to_insert, 500):
        _insert_assignment_rows(chunk)
        result["created"] += len(chunk)

    return result


def bulk_unassign_campaigns(user_id: int, campaign_ids: list[int]) -> int:
    """A user összes kampány-szintű hozzárendelésének törlése a megadott kampányokról.

    Visszatérés: a törölt sorok száma.
    """
    if not campaign_ids:
        return 0
    sb = get_supabase()
    deleted = 0
    for chunk in _chunks(campaign_ids, 200):
        existing = (
            sb.table(_TABLE)
            .select("id")
            .eq("user_id", user_id)
            .in_("campaign_id", chunk)
            .execute()
            .data
            or []
        )
        if not existing:
            continue
        sb.table(_TABLE).delete().eq("user_id", user_id).in_("campaign_id", chunk).execute()
        deleted += len(existing)
    return deleted


def inherit_client_assignments_for_campaign(client_id: int, campaign_id: int) -> int:
    """Egy ÚJ kampányra örökíti a kliens-szintű hozzárendeléseket (discovery hívja).

    Minden userre, akinek van kliens-szintű (client_id, campaign_id=null)
    hozzárendelése ehhez a klienshez, létrehoz egy öröklött kampány-szintű sort,
    ha még nincs hozzárendelése a kampányon.

    Visszatérés: a létrehozott öröklött hozzárendelések száma.
    """
    client_assigns = get_assignments_for_client(client_id)
    if not client_assigns:
        return 0

    sb = get_supabase()
    existing = (
        sb.table(_TABLE)
        .select("user_id")
        .eq("campaign_id", campaign_id)
        .execute()
        .data
        or []
    )
    already = {r["user_id"] for r in existing}

    to_insert: list[dict[str, Any]] = []
    for a in client_assigns:
        uid = a["user_id"]
        if uid in already:
            continue
        to_insert.append({
            "user_id": uid,
            "campaign_id": campaign_id,
            "role": a.get("role") or "primary",
            "inherited_from_client": True,
        })
    _insert_assignment_rows(to_insert)
    return len(to_insert)
