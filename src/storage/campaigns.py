"""
Kampány (campaigns tábla) adathozzáférés.

A kampányok ügyfelekhez tartoznak (client_id FK). Minden kampánynak van egy
lifecycle_state mezője (new / learning / mature / paused / ended), amely
meghatározza, hogy a monitoring-motor melyik anomáliákat ellenőrzi.

A „supporter" logika az assignments táblán keresztül van megvalósítva:
  - primary assignment: az elsődleges felelős OM
  - supporter assignment: helyettes OM, aki szintén kap értesítést

Függvények:
    list_campaigns(client_id, active_only)   — kampányok listázása ügyfél szerint
    get_campaign(campaign_id)                 — egy kampány lekérdezése ID alapján
    create_campaign(...)                      — új kampány létrehozása
    set_lifecycle_state(campaign_id, state, until)  — lifecycle állapot beállítása
    add_supporter(campaign_id, user_id)       — helyettes OM hozzáadása
    remove_supporter(campaign_id, user_id)    — helyettes OM eltávolítása
"""
from __future__ import annotations

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
    """Listázza egy ügyfél kampányait, név szerint rendezve.

    Ha active_only=True, csak a nem-'ended' kampányokat adja vissza.
    Ha active_only=False, az összes kampányt (archívumot is) visszaadja.
    """
    query = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("client_id", client_id)
        .order("name")
    )
    if active_only:
        query = query.neq("lifecycle_state", "ended")
    return query.execute().data or []


def get_campaign(campaign_id: int) -> dict[str, Any] | None:
    """Egy kampány lekérdezése ID alapján. None ha nem létezik."""
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("id", campaign_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


# ---------------------------------------------------------------------------
# Írás
# ---------------------------------------------------------------------------

def create_campaign(
    client_id: int,
    name: str,
    platform: str,
    *,
    account_id: str | None = None,
    campaign_type: str | None = None,
    lifecycle_state: str = "new",
) -> dict[str, Any]:
    """Új kampány létrehozása. Visszaadja a létrehozott sort.

    Paraméterek:
        client_id       — melyik ügyfélhez tartozik
        name            — kampány neve (ügyfélhez egyedi, de nem kényszerített DB-ben)
        platform        — 'meta' | 'google'
        account_id      — hirdetési fiók azonosítója (Meta: act_XXXXX, Google: XXXXX)
        campaign_type   — pl. 'meta_conversion', 'google_brand_search'
        lifecycle_state — induló lifecycle állapot (alapból: 'new')

    A name+client_id duplikátumot a hívó réteg kezeli (query ellenőrzéssel),
    mivel a DB séma ezt nem kényszeríti — az üzleti logika szintjén kezeljük.
    """
    payload: dict[str, Any] = {
        "client_id": client_id,
        "name": name.strip(),
        "platform": platform,
        "lifecycle_state": lifecycle_state,
    }
    if account_id is not None:
        payload["account_id"] = account_id
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
                       Ha None, a meglévő lifecycle_until mező NULL-ra kerül.

    Visszatérés: frissített kampány sor, vagy None ha nem találta.
    """
    payload: dict[str, Any] = {
        "lifecycle_state": state,
        "lifecycle_until": until,   # explicit None → NULL (until lejárta törlése)
    }

    res = (
        get_supabase()
        .table(_TABLE)
        .update(payload)
        .eq("id", campaign_id)
        .execute()
    )
    return res.data[0] if res.data else None


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
    # Duplikáció-ellenőrzés
    existing = (
        get_supabase()
        .table(_ASSIGNMENTS_TABLE)
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

    res = get_supabase().table(_ASSIGNMENTS_TABLE).insert(payload).execute()
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
