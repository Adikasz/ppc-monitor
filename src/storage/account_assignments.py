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

from src.storage import ad_accounts as ad_accounts_storage
from src.storage import assignments as assignments_storage
from src.storage import clients as clients_storage
from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

_TABLE = "account_assignments"

log = get_logger(__name__)


def _find_clients_ilike(token: str) -> list[dict[str, Any]]:
    """Aktív kliensek név szerinti, kis/nagybetű-független részleges keresése."""
    try:
        res = (
            get_supabase()
            .table("clients")
            .select("*")
            .ilike("name", f"%{token}%")
            .eq("is_active", True)
            .order("name")
            .execute()
        )
    except Exception:  # noqa: BLE001
        return []
    return res.data or []


def resolve_accounts(token: str) -> dict[str, Any]:
    """Feloldja az `/account assign` `account` paraméterét ad_account sorokká (23. lépés).

    Sorrend:
      a) szám → #id (ad_accounts.id) — backward compat
      b) szöveg: pontos external_account_id egyezés (act_ prefix toleráns)
      c) ha nincs: kliensnév ILIKE → a kliens(ek) ÖSSZES aktív fiókja
      d) 1 találat → "ok"; több → "ambiguous"; nincs → "not_found"

    Visszatérés:
        {"status": "ok"|"ambiguous"|"not_found",
         "accounts": [ad_account, ...], "token": str,
         "via": "id"|"external"|"client"|None,
         "client_name": str}   # csak egyértelmű kliens-egyezésnél
    """
    token = (token or "").strip()
    if not token:
        return {"status": "not_found", "accounts": [], "token": token, "via": None}

    # a) szám → #id (backward compat — aktivitástól függetlenül, mint eddig)
    if token.isdigit():
        acct = ad_accounts_storage.get_ad_account(int(token))
        if acct:
            return {"status": "ok", "accounts": [acct], "token": token, "via": "id"}
        return {"status": "not_found", "accounts": [], "token": token, "via": "id"}

    # b) pontos external_account_id egyezés (nyers + normalizált alakok)
    candidates = [
        token,
        ad_accounts_storage.normalize_external_account_id("meta", token),
        ad_accounts_storage.normalize_external_account_id("google", token),
    ]
    ext = [
        a for a in ad_accounts_storage.find_ad_accounts_by_external_id(candidates)
        if a.get("is_active", True)
    ]
    if ext:
        return {
            "status": "ok" if len(ext) == 1 else "ambiguous",
            "accounts": ext, "token": token, "via": "external",
        }

    # c) kliensnév ILIKE → a kliens(ek) összes aktív fiókja
    matched_clients = _find_clients_ilike(token)
    accounts: list[dict[str, Any]] = []
    for c in matched_clients:
        accounts.extend(
            ad_accounts_storage.get_ad_accounts_for_client(c["id"], active_only=True)
        )
    if not accounts:
        return {"status": "not_found", "accounts": [], "token": token, "via": "client"}

    return {
        "status": "ok" if len(accounts) == 1 else "ambiguous",
        "accounts": accounts,
        "token": token,
        "via": "client",
        "client_name": matched_clients[0]["name"] if len(matched_clients) == 1 else token,
    }


def search_account_choices(query: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Discord autocomplete-hez: kliensnév ILIKE → fiók-jelöltek listája.

    Két lekérdezés (nincs N+1): kliensek név szerint, majd EGY `in_` a fiókokra.
    Üres `query` az összesre illeszt (a hívó vágja 25-re). Visszatérés elemei:
        {"id", "client_name", "platform", "external_account_id"}
    kliensnév + #id szerint rendezve.
    """
    q = (query or "").strip()
    sb = get_supabase()
    clients = (
        sb.table("clients").select("id, name")
        .ilike("name", f"%{q}%").eq("is_active", True)
        .order("name").limit(15).execute().data
        or []
    )
    if not clients:
        return []
    names = {c["id"]: c["name"] for c in clients}
    accts = (
        sb.table("ad_accounts").select("id, platform, external_account_id, client_id")
        .in_("client_id", list(names)).eq("is_active", True).execute().data
        or []
    )
    out = [
        {
            "id": a["id"],
            "client_name": names.get(a["client_id"], "?"),
            "platform": a["platform"],
            "external_account_id": a["external_account_id"],
        }
        for a in accts
    ]
    out.sort(key=lambda r: (r["client_name"].lower(), r["id"]))
    return out[:limit]


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


def delete_account_assignments_for_accounts(account_ids: list[int]) -> int:
    """A megadott fiókok ÖSSZES OM-hozzárendelésének törlése (offboard).

    Védve: ha a tábla még nem létezik, 0. Visszatérés: a törölt sorok száma.
    """
    if not account_ids:
        return 0
    sb = get_supabase()
    try:
        existing = (
            sb.table(_TABLE).select("id").in_("ad_account_id", account_ids).execute().data or []
        )
        if existing:
            sb.table(_TABLE).delete().in_("ad_account_id", account_ids).execute()
        return len(existing)
    except Exception as exc:  # noqa: BLE001
        if _is_missing_relation(exc):
            return 0
        raise


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
