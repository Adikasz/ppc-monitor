"""
Fiók-szintű hozzárendelés (account_assignments tábla) adathozzáférés (21. lépés).

A kliens-szintű hozzárendelés fiók-szintű megfelelője: egy OM egy ad_account-hoz
rendelve (UNIQUE(ad_account_id, user_id)). A `/account assign` ide ír, majd
lecsorgatja a hozzárendelést a fiók kampányainak `assignments` soraiba
(inherited_from_account=true). Új kampány a discovery során örökli a fiók
hozzárendeléseit (`inherit_account_assignments_for_campaign`).

A táblát a 0010 migration hozza létre. A READ függvények védettek: ha a tábla
még nem létezik, üres lista a válasz (a megjelenítés nem dől el).
"""
from __future__ import annotations

from typing import Any

from src.storage import assignments as assignments_storage
from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

_TABLE = "account_assignments"

log = get_logger(__name__)


def _is_missing_relation(exc: Exception) -> bool:
    msg = str(exc).lower()
    return _TABLE in msg and any(
        s in msg for s in ("does not exist", "could not find", "pgrst205", "relation", "schema cache")
    )


def get_account_assignment(ad_account_id: int, user_id: int) -> dict[str, Any] | None:
    """Egy konkrét (fiók, user) hozzárendelés. None ha nincs."""
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("ad_account_id", ad_account_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_account_assignments(ad_account_id: int) -> list[dict[str, Any]]:
    """Egy fiók összes OM-hozzárendelése (user adatokkal). Üres ha nincs / nincs tábla."""
    try:
        res = (
            get_supabase()
            .table(_TABLE)
            .select("*, users(id, discord_user_id, display_name, alerts_channel_id)")
            .eq("ad_account_id", ad_account_id)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        if _is_missing_relation(exc):
            return []
        raise
    return res.data or []


def list_all_account_assignments() -> list[dict[str, Any]]:
    """Az ÖSSZES fiók-hozzárendelés (bulk, /account list + /client list).

    Védve: ha a tábla még nem létezik (0010 előtt), üres lista.
    """
    try:
        res = (
            get_supabase()
            .table(_TABLE)
            .select("ad_account_id, role, users(display_name, discord_user_id)")
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        if _is_missing_relation(exc):
            return []
        raise
    return res.data or []


def get_account_ids_for_user(user_id: int) -> set[int]:
    """Azon ad_account-ID-k halmaza, amelyek ehhez a userhez vannak rendelve.

    A `/my` parancsok jogosultság-ellenőrzéséhez. Védve: ha a tábla még nem
    létezik (0010 előtt), üres halmaz.
    """
    try:
        res = (
            get_supabase().table(_TABLE).select("ad_account_id").eq("user_id", user_id).execute()
        )
    except Exception as exc:  # noqa: BLE001
        if _is_missing_relation(exc):
            return set()
        raise
    return {r["ad_account_id"] for r in (res.data or [])}


def get_accounts_for_user(user_id: int) -> list[dict[str, Any]]:
    """A userhez rendelt hirdetési fiókok (ad_account sorok + szerep).

    Minden elem a teljes ad_account sor, kiegészítve `_role` kulccsal. A `/my
    accounts` használja. Védve: ha a tábla még nem létezik, üres lista.
    """
    try:
        res = (
            get_supabase()
            .table(_TABLE)
            .select("role, ad_accounts(*)")
            .eq("user_id", user_id)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        if _is_missing_relation(exc):
            return []
        raise
    out: list[dict[str, Any]] = []
    for r in (res.data or []):
        acc = r.get("ad_accounts")
        if acc:
            out.append({**acc, "_role": r.get("role")})
    return out


def upsert_account_assignment(
    ad_account_id: int,
    user_id: int,
    *,
    role: str = "primary",
) -> tuple[dict[str, Any], bool]:
    """Fiók-hozzárendelés létrehozása vagy role-frissítése.

    Visszatérés: (sor, created). created=True ha most jött létre; ha már létezett
    és a role változott, frissítjük és created=False (a hívó a role-t úgyis közli).
    """
    existing = get_account_assignment(ad_account_id, user_id)
    if existing:
        if existing.get("role") != role:
            updated = (
                get_supabase()
                .table(_TABLE)
                .update({"role": role})
                .eq("id", existing["id"])
                .execute()
            )
            return (updated.data[0] if updated.data else {**existing, "role": role}), False
        return existing, False

    res = (
        get_supabase()
        .table(_TABLE)
        .insert({"ad_account_id": ad_account_id, "user_id": user_id, "role": role})
        .execute()
    )
    return res.data[0], True


def delete_account_assignment(ad_account_id: int, user_id: int) -> bool:
    """Fiók-hozzárendelés törlése. True ha volt mit törölni."""
    existing = get_account_assignment(ad_account_id, user_id)
    if not existing:
        return False
    get_supabase().table(_TABLE).delete().eq("id", existing["id"]).execute()
    return True


def inherit_account_assignments_for_campaign(ad_account_id: int, campaign_id: int) -> int:
    """Egy ÚJ kampányra örökíti a fiók OM-hozzárendeléseit (discovery hívja).

    A fiók account_assignments sorait kampány-szintű, öröklött
    (inherited_from_account=true) `assignments` sorokká alakítja azon userekre,
    akiknek még nincs hozzárendelése a kampányon.

    Visszatérés: a létrehozott öröklött hozzárendelések száma.
    """
    rows = get_account_assignments(ad_account_id)
    if not rows:
        return 0
    source = [{"user_id": r["user_id"], "role": r.get("role") or "primary"} for r in rows]
    return assignments_storage.inherit_assignments_for_campaign(
        campaign_id, source, inherited_field="inherited_from_account"
    )
