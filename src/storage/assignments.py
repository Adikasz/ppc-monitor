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
        msg = str(exc).lower()
        if "inherited_from_client" in msg or "inherited_from_account" in msg:
            # 0009/0010 migration még nem futott — a flag(ek) nélkül újrapróbáljuk.
            stripped = [
                {k: v for k, v in r.items()
                 if k not in ("inherited_from_client", "inherited_from_account")}
                for r in rows
            ]
            sb.table(_TABLE).insert(stripped).execute()
        else:
            raise


def bulk_assign_campaigns(
    user_id: int,
    campaign_ids: list[int],
    *,
    role: str = "primary",
    inherited_field: str = "inherited_from_client",
    created_by_discord_user_id: str | None = None,
) -> dict[str, int]:
    """A user kampány-szintű hozzárendelése a megadott kampányokhoz (öröklött).

    Idempotens: meglévő (user, campaign) párnál csak a role-t igazítja, ha eltér;
    egyébként új, öröklött sort szúr be (bulk, csonkolva). Az `inherited_field`
    jelzi a forrást: 'inherited_from_client' vagy 'inherited_from_account'.

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
                inherited_field: True,
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


def inherit_assignments_for_campaign(
    campaign_id: int,
    source_user_roles: list[dict[str, Any]],
    *,
    inherited_field: str = "inherited_from_client",
) -> int:
    """Öröklött kampány-szintű hozzárendelések létrehozása egy ÚJ kampányra.

    `source_user_roles`: [{"user_id": int, "role": str}, …] — a forrás (kliens-
    vagy fiók-szintű) hozzárendelések. Csak azokra a userekre szúr be, akiknek
    még NINCS hozzárendelése a kampányon. Az `inherited_field`
    ('inherited_from_client' | 'inherited_from_account') jelzi a forrást.

    Visszatérés: a létrehozott öröklött hozzárendelések száma.
    """
    if not source_user_roles:
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
    for src in source_user_roles:
        uid = src["user_id"]
        if uid in already:
            continue
        to_insert.append({
            "user_id": uid,
            "campaign_id": campaign_id,
            "role": src.get("role") or "primary",
            inherited_field: True,
        })
    _insert_assignment_rows(to_insert)
    return len(to_insert)


def inherit_client_assignments_for_campaign(client_id: int, campaign_id: int) -> int:
    """Kliens-szintű hozzárendelések öröklése egy új kampányra (visszafelé kompat.).

    A fiók-szintű öröklés a `storage.account_assignments`-ban van — a discovery
    mindkettőt meghívja az új kampányra.
    """
    client_assigns = get_assignments_for_client(client_id)
    source = [{"user_id": a["user_id"], "role": a.get("role")} for a in client_assigns]
    return inherit_assignments_for_campaign(
        campaign_id, source, inherited_field="inherited_from_client"
    )


def list_client_level_assignments() -> list[dict[str, Any]]:
    """Az ÖSSZES ügyfél-szintű hozzárendelés (campaign_id IS NULL), user adatokkal.

    A `/client list` bulk OM-megjelenítéséhez: egyetlen lekérdezés, a parancs
    réteg client_id szerint csoportosítja. (Az ilyen sorokban a client_id a
    sémakényszer miatt mindig kitöltött.)
    """
    res = (
        get_supabase()
        .table(_TABLE)
        .select("client_id, role, users(display_name, discord_user_id)")
        .is_("campaign_id", "null")
        .execute()
    )
    return res.data or []
