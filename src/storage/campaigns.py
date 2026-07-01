"""
Kampány (campaigns tábla) adathozzáférés.

A kampányok a `campaigns` táblában vannak, és az `ad_accounts` tábla FK-ján
keresztül kötődnek ügyfelekhez (campaigns → ad_accounts → clients).

Az auto-discovery tölti fel a legtöbb rekordot; kézi létrehozás is lehetséges
(főleg teszteléshez vagy Google Ads kampányoknál).

A „supporter" logika az assignments táblán keresztül kezelt:
  - primary assignment: az elsődleges felelős OM (role='primary')
  - supporter assignment: helyettes OM (role='supporter')

Függvények — lekérdezések:
    list_campaigns(client_id, active_only)          — ügyfélt összes kampánya
    get_campaign(campaign_id)                        — ID alapján
    get_campaigns_by_ad_account(ad_account_id)       — egy fiók összes kampánya

Függvények — írás:
    create_campaign(ad_account_id, external_campaign_id, name, ...)
    set_lifecycle_state(campaign_id, state, until)   — lifecycle váltás
    update_campaign_status(campaign_id, status, last_seen_at)  — discovery frissítés
    soft_delete_campaign(campaign_id)                — is_monitored=false
    add_supporter(campaign_id, user_id)              — helyettes hozzáadása
    remove_supporter(campaign_id, user_id)           — helyettes eltávolítása
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

_TABLE = "campaigns"
_ASSIGNMENTS_TABLE = "assignments"

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Lekérdezések
# ---------------------------------------------------------------------------

def list_campaigns(
    client_id: int,
    *,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """Listázza egy ügyfél összes kampányát, név szerint rendezve.

    Mivel campaigns → ad_accounts → clients, először lekérjük az ügyfél
    fiók-ID-jait, majd az azokhoz tartozó kampányokat.

    Ha active_only=True, csak a nem-'ended' lifecycle_state-ű kampányokat
    adja vissza. Ha False, az archivált (ended) kampányokat is.
    """
    sb = get_supabase()

    # 1) Ügyfél ad_account ID-jainak lekérdezése
    accounts_res = (
        sb.table("ad_accounts")
        .select("id")
        .eq("client_id", client_id)
        .execute()
    )
    account_ids = [a["id"] for a in (accounts_res.data or [])]
    if not account_ids:
        return []

    # 2) Kampányok lekérdezése ezen fiók-ID-kra
    query = (
        sb.table(_TABLE)
        .select("*")
        .in_("ad_account_id", account_ids)
        .order("name")
    )
    if active_only:
        query = query.neq("lifecycle_state", "ended")
    return query.execute().data or []


def get_campaign(campaign_id: int) -> dict[str, Any] | None:
    """Egy kampány lekérdezése belső ID alapján. None ha nem létezik."""
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("id", campaign_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_campaigns_by_ad_account(ad_account_id: int) -> list[dict[str, Any]]:
    """Egy hirdetési fiók összes kampányának lekérdezése.

    Auto-discovery és fetcher használja — a monitored + nem-monitored
    kampányokat egyaránt visszaadja (a szűrést a hívó végzi).
    """
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("ad_account_id", ad_account_id)
        .order("name")
        .execute()
    )
    return res.data or []


# ---------------------------------------------------------------------------
# Írás
# ---------------------------------------------------------------------------

def create_campaign(
    ad_account_id: int,
    external_campaign_id: str,
    name: str,
    *,
    platform_status: str | None = None,
    campaign_type: str | None = None,
    lifecycle_state: str = "new",
    is_monitored: bool = True,
) -> dict[str, Any]:
    """Új kampány létrehozása (tipikusan az auto-discovery hívja).

    Paraméterek:
        ad_account_id        — hirdetési fiók belső DB ID-ja
        external_campaign_id — a platform saját kampány ID-ja (Meta: "123456789")
        name                 — kampánynév (a platformról jön)
        platform_status      — API státusz: ACTIVE | PAUSED | DELETED | ARCHIVED
        campaign_type        — opcionális típus-ímkézés (meta_conversion stb.)
        lifecycle_state      — induló lifecycle (alapból: 'new')
        is_monitored         — figyelje-e a rendszer (alapból: True)

    Duplikáció esetén (ad_account_id + external_campaign_id) a DB UNIQUE
    constraint 23505 hibát dob — a hívó réteg kezeli.
    """
    payload: dict[str, Any] = {
        "ad_account_id": ad_account_id,
        "external_campaign_id": external_campaign_id,
        "name": name.strip(),
        "lifecycle_state": lifecycle_state,
        "is_monitored": is_monitored,
    }
    if platform_status is not None:
        payload["platform_status"] = platform_status
    if campaign_type is not None:
        payload["campaign_type"] = campaign_type

    res = get_supabase().table(_TABLE).insert(payload).execute()
    return res.data[0]


def set_lifecycle_state(
    campaign_id: int,
    state: str,
    *,
    until: str | None = None,
) -> dict[str, Any] | None:
    """Lifecycle állapot frissítése.

    Paraméterek:
        campaign_id  — érintett kampány DB ID-ja
        state        — 'new' | 'learning' | 'mature' | 'paused' | 'ended'
        until        — opcionális ISO dátum/datetime string;
                       lejáratkor a scheduler automatikusan 'mature'-re vált.
                       Ha None → lifecycle_until NULL-ra kerül.

    Visszatérés: frissített kampány sor, None ha nem találta.
    """
    payload: dict[str, Any] = {
        "lifecycle_state": state,
        "lifecycle_until": until,
    }

    res = (
        get_supabase()
        .table(_TABLE)
        .update(payload)
        .eq("id", campaign_id)
        .execute()
    )
    return res.data[0] if res.data else None


def update_campaign_status(
    campaign_id: int,
    status: str,
    last_seen_at: datetime,
) -> dict[str, Any] | None:
    """Platform státusz és last_seen_at frissítése (discovery futtatja).

    Paraméterek:
        campaign_id   — belső DB ID
        status        — platform szerinti státusz (ACTIVE, PAUSED, stb.)
        last_seen_at  — mikor láttuk utoljára a Meta/Google API válaszban

    Visszatérés: frissített sor, None ha nem találta.
    """
    res = (
        get_supabase()
        .table(_TABLE)
        .update({
            "platform_status": status,
            "last_seen_at": last_seen_at.isoformat(),
        })
        .eq("id", campaign_id)
        .execute()
    )
    return res.data[0] if res.data else None


def soft_delete_campaign(campaign_id: int) -> bool:
    """Kampány soft-delete: is_monitored = false.

    Akkor kerül ide, ha a kampány már nem látható a platform API-ban
    (24+ óra óta nem láttuk). Fizikai törlés NEM történik, az adat megmarad.

    Visszatér True-val ha sikeres, False-szal ha nem találta.
    """
    existing = get_campaign(campaign_id)
    if not existing:
        return False

    (
        get_supabase()
        .table(_TABLE)
        .update({"is_monitored": False})
        .eq("id", campaign_id)
        .execute()
    )
    log.info("Kampány soft-deleted (is_monitored=false): #%s", campaign_id)
    return True


def add_supporter(
    campaign_id: int,
    user_id: int,
    *,
    created_by_discord_user_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Helyettes OM (supporter) hozzáadása kampányhoz.

    Az assignments táblán keresztül kezeli a relációt (role='supporter').
    Idempotens: ha már létezik a supporter hozzárendelés, a meglévőt adja vissza.

    Visszatérés: (assignment_dict, created)
        created=True  → most hozta létre
        created=False → már létezett
    """
    sb = get_supabase()

    existing = (
        sb.table(_ASSIGNMENTS_TABLE)
        .select("*")
        .eq("campaign_id", campaign_id)
        .eq("user_id", user_id)
        .eq("role", "supporter")
        .limit(1)
        .execute()
    )
    if existing.data:
        return existing.data[0], False

    payload: dict[str, Any] = {
        "campaign_id": campaign_id,
        "user_id": user_id,
        "role": "supporter",
    }
    if created_by_discord_user_id is not None:
        payload["created_by_discord_user_id"] = created_by_discord_user_id

    res = sb.table(_ASSIGNMENTS_TABLE).insert(payload).execute()
    return res.data[0], True


def remove_supporter(
    campaign_id: int,
    user_id: int,
) -> bool:
    """Helyettes OM (supporter) eltávolítása kampányról.

    Visszatér True-val ha volt mit törölni, False-szal ha nem létezett.
    Csak a 'supporter' role-ú sort törli — a 'primary' hozzárendelést nem érinti.
    """
    existing = (
        get_supabase()
        .table(_ASSIGNMENTS_TABLE)
        .select("id")
        .eq("campaign_id", campaign_id)
        .eq("user_id", user_id)
        .eq("role", "supporter")
        .limit(1)
        .execute()
    )
    if not existing.data:
        return False

    (
        get_supabase()
        .table(_ASSIGNMENTS_TABLE)
        .delete()
        .eq("id", existing.data[0]["id"])
        .execute()
    )
    return True


# ---------------------------------------------------------------------------
# Aggregáló segédfüggvények (overview / számlálás)
# ---------------------------------------------------------------------------

def list_ad_account_ids(*, active_only: bool = True) -> list[int]:
    """Minden kampány ad_account_id-ja (egy elem / kampány).

    A `/client list` overview ebből számol fiókonkénti / ügyfelenkénti
    kampányszámot (Counter), egyetlen lekérdezésből — nincs N+1 query.

    active_only=True esetén az 'ended' lifecycle-ű kampányokat kihagyja
    (ugyanaz a láthatóság, mint a `/campaign list`-nél).

    Lapozás (ugyanaz, mint a get_active_campaigns-ben): ez a `campaigns` tábláról
    olvas egy sort KAMPÁNYONKÉNT (~4000+), így a PostgREST 1000-es sorlimitje
    már most levágná — `range()`-dzsel addig lapozunk, amíg egy oldal 1000-nél
    kevesebbet ad vissza.
    """
    page = 1000
    start = 0
    out: list[int] = []
    while True:
        query = get_supabase().table(_TABLE).select("ad_account_id").order("id")
        if active_only:
            query = query.neq("lifecycle_state", "ended")
        rows = query.range(start, start + page - 1).execute().data or []
        out.extend(r["ad_account_id"] for r in rows)
        if len(rows) < page:
            break
        start += page
    return out


# ---------------------------------------------------------------------------
# Batch monitoring (16. lépés) — kampányok az ad_account adataival együtt
# ---------------------------------------------------------------------------

def get_active_campaigns() -> list[dict[str, Any]]:
    """Aktívan monitorozott kampányok, az ad_account adataival JOIN-olva.

    Szűrés: is_monitored = true ÉS lifecycle_state NOT IN ('paused', 'ended').
    Minden sor tartalmazza a beágyazott `ad_accounts` objektumot
    (id, external_account_id, platform, is_active) — ebből csoportosít a
    scheduler ad_account-onként (batch pull, 1 API hívás / fiók).

    A `!inner` join miatt csak azok a kampányok jönnek, amelyeknek van
    (létező) ad_account-juk.

    Lapozás: a PostgREST egy kérésre max 1000 sort ad vissza. ~4000+ aktív
    kampánynál ez csak az első 1000-et hozná (a scheduler a többit nem látná),
    ezért `range()`-dzsel addig lapozunk, amíg egy oldal 1000-nél kevesebbet ad.
    """
    page = 1000
    start = 0
    out: list[dict[str, Any]] = []
    while True:
        rows = (
            get_supabase()
            .table(_TABLE)
            .select("*, ad_accounts!inner(id, external_account_id, platform, is_active)")
            .eq("is_monitored", True)
            .neq("lifecycle_state", "paused")
            .neq("lifecycle_state", "ended")
            .order("id")
            .range(start, start + page - 1)
            .execute()
            .data
            or []
        )
        out.extend(rows)
        if len(rows) < page:
            break
        start += page
    return out


def group_campaigns_by_account(
    campaigns: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """Kampányok csoportosítása ad_account belső ID szerint (tiszta Python).

    A `get_active_campaigns` által visszaadott (beágyazott ad_accounts-os)
    sorokra számít. Visszatérés: {ad_account_db_id: [campaign, ...]}.
    """
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for c in campaigns:
        account = c.get("ad_accounts") or {}
        account_id = account.get("id") or c.get("ad_account_id")
        if account_id is None:
            continue
        grouped[account_id].append(c)
    return dict(grouped)


# ---------------------------------------------------------------------------
# Lifecycle tömeges műveletek (offboard / pause / resume / reactivate, 25. lépés)
# ---------------------------------------------------------------------------

def _chunks(seq: list[Any], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def set_campaigns_lifecycle(
    campaign_ids: list[int],
    state: str,
    *,
    is_monitored: bool | None = None,
    lifecycle_until: str | None = "__keep__",
) -> int:
    """Több kampány lifecycle_state-jét (és opcionálisan is_monitored / lifecycle_until)
    állítja egy körben (200-as kötegekben). Visszatérés: az érintett sorok száma.

    lifecycle_until: `"__keep__"` (alap) → nem nyúl hozzá; None → NULL-ra állítja;
    string → arra az értékre.
    """
    if not campaign_ids:
        return 0
    payload: dict[str, Any] = {"lifecycle_state": state}
    if is_monitored is not None:
        payload["is_monitored"] = is_monitored
    if lifecycle_until != "__keep__":
        payload["lifecycle_until"] = lifecycle_until

    sb = get_supabase()
    affected = 0
    for chunk in _chunks(campaign_ids, 200):
        res = sb.table(_TABLE).update(payload).in_("id", chunk).execute()
        affected += len(res.data or [])
    return affected


def resume_due_paused_campaigns() -> int:
    """Auto-resume: a lejárt szüneteltetésű kampányok visszaállítása.

    Azokat a `paused` kampányokat, amelyek `lifecycle_until`-ja a múltban van,
    `mature`-re állítja (is_monitored=true, lifecycle_until=NULL). A kézzel,
    határidő nélkül szüneteltetett kampányokat (lifecycle_until IS NULL) NEM
    érinti. A scheduler napi jobja hívja. Visszatérés: a visszaállított db.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    sb = get_supabase()
    rows = (
        sb.table(_TABLE).select("id")
        .eq("lifecycle_state", "paused")
        .lte("lifecycle_until", now_iso)
        .execute().data
        or []
    )
    ids = [r["id"] for r in rows]
    if not ids:
        return 0
    for chunk in _chunks(ids, 200):
        sb.table(_TABLE).update(
            {"lifecycle_state": "mature", "is_monitored": True, "lifecycle_until": None}
        ).in_("id", chunk).execute()
    log.info("Auto-resume: %d szüneteltetett kampány visszaállítva (mature)", len(ids))
    return len(ids)


def search_campaigns(query: str, *, limit: int = 25, include_ended: bool = False) -> list[dict[str, Any]]:
    """Kampány autocomplete-hez: szám → pontos id, szöveg → név ILIKE.

    Visszatérés elemei: {"id", "name", "lifecycle_state"} (név szerint rendezve).
    """
    q = (query or "").strip()
    qb = get_supabase().table(_TABLE).select("id, name, lifecycle_state")
    if not include_ended:
        qb = qb.neq("lifecycle_state", "ended")
    if q.isdigit():
        qb = qb.eq("id", int(q))
    elif q:
        qb = qb.ilike("name", f"%{q}%")
    return qb.order("name").limit(limit).execute().data or []


def search_campaign_choices(account_id: int, query: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Kampány autocomplete EGY fiókon belül: szám → pontos id, szöveg → név ILIKE.

    A `/my lifecycle` használja: az OM már kiválasztott egy fiókot, itt csak az
    abban lévő (nem-'ended') kampányokat kínáljuk fel. Visszatérés elemei:
    {"id", "name"} (név szerint rendezve).
    """
    q = (query or "").strip()
    qb = (
        get_supabase().table(_TABLE)
        .select("id, name")
        .eq("ad_account_id", account_id)
        .neq("lifecycle_state", "ended")
    )
    if q.isdigit():
        qb = qb.eq("id", int(q))
    elif q:
        qb = qb.ilike("name", f"%{q}%")
    return qb.order("name").limit(limit).execute().data or []


def search_campaign_choices_global(query: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Kampány autocomplete MINDEN fiókban, kliensnévvel (admin `/alert …`).

    A visszaadott sorok tartalmazzák a beágyazott `ad_accounts(clients(name))`
    objektumot, hogy a felkínált címke `Kliens / Kampány` formájú lehessen.
    Szám → pontos id, szöveg → név ILIKE; az 'ended' kampányokat kihagyja.
    """
    q = (query or "").strip()
    qb = (
        get_supabase().table(_TABLE)
        .select("id, name, ad_accounts(clients(name))")
        .neq("lifecycle_state", "ended")
    )
    if q.isdigit():
        qb = qb.eq("id", int(q))
    elif q:
        qb = qb.ilike("name", f"%{q}%")
    return qb.order("name").limit(limit).execute().data or []


def resolve_campaign(value: str, account_id: int | None = None) -> dict[str, Any] | None:
    """Kampány feloldása str értékből (autocomplete VAGY kézi bevitel).

    - szám (`"920"`)  → pontos belső ID egyezés (backward compat)
    - szöveg          → név ILIKE keresés (ha `account_id` adott, arra szűrve)

    Visszatérés:
        - kampány sor {"id", "name", "lifecycle_state", "ad_account_id"} — egy egyértelmű találat
        - {"ambiguous": True, "matches": [...]} — több névtalálat (max 5)
        - None — nincs találat
    """
    v = (value or "").strip()
    if not v:
        return None

    sb = get_supabase()
    cols = "id, name, lifecycle_state, ad_account_id"

    if v.isdigit():
        res = sb.table(_TABLE).select(cols).eq("id", int(v)).limit(1).execute()
        return res.data[0] if res.data else None

    qb = sb.table(_TABLE).select(cols).ilike("name", f"%{v}%")
    if account_id is not None:
        qb = qb.eq("ad_account_id", account_id)
    results = qb.order("name").limit(5).execute().data or []

    if len(results) == 1:
        return results[0]
    if len(results) > 1:
        return {"ambiguous": True, "matches": results}
    return None
