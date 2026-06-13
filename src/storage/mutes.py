"""
Némítás (mutes tábla) adathozzáférés.

A némítás kampányonként X ideig letiltja a riasztásokat (pl. tanulási fázis,
ismert kamp-leállás). A detektor és az alert-router is ezt nézi: muted kampányra
nem keletkezik / nem megy ki riasztás.

FONTOS — séma: a `mutes` tábla a 0001_initial_schema.sql-ben már létezik, és
`muted_until` + `is_active` + `created_by_discord_user_id` oszlopokat használ
(NEM `until`/`created_by`-t). Ez a modul ehhez a valós sémához igazodik; nem
kell külön migráció.

Aktív némítás = is_active = true ÉS muted_until > now().

Függvények:
    mute_campaign(campaign_id, until, ...)  — némítás beállítása
    unmute_campaign(campaign_id)            — korai feloldás (is_active = false)
    is_muted(campaign_id)                   — van-e aktív, le nem járt némítás
    get_active_mute(campaign_id)            — az aktív némítás sora (vagy None)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

_TABLE = "mutes"

log = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mute_campaign(
    campaign_id: int,
    until: datetime,
    *,
    reason: str | None = None,
    created_by_discord_user_id: str | None = None,
) -> dict[str, Any]:
    """Kampány némítása `until` időpontig.

    A meglévő aktív némítás(oka)t lezárja (is_active=false), majd új aktív
    némítást szúr be — így mindig egyetlen aktív némítás van kampányonként.

    Visszatérés: a beszúrt mute sor.
    """
    sb = get_supabase()

    # Meglévő aktív némítások lezárása (egyetlen aktív legyen)
    sb.table(_TABLE).update({"is_active": False}).eq("campaign_id", campaign_id).eq(
        "is_active", True
    ).execute()

    payload: dict[str, Any] = {
        "campaign_id": campaign_id,
        "muted_until": until.isoformat() if hasattr(until, "isoformat") else str(until),
        "is_active": True,
    }
    if reason:
        payload["reason"] = reason
    if created_by_discord_user_id:
        payload["created_by_discord_user_id"] = str(created_by_discord_user_id)

    res = sb.table(_TABLE).insert(payload).execute()
    row = res.data[0]
    log.info(
        "Kampány némítva: #%s eddig=%s (mute #%s)",
        campaign_id, payload["muted_until"], row["id"],
    )
    return row


def unmute_campaign(campaign_id: int) -> bool:
    """Aktív némítás(ok) feloldása (is_active=false).

    Visszatér True-val ha volt mit feloldani, False-szal ha nem volt aktív némítás.
    """
    sb = get_supabase()
    existing = (
        sb.table(_TABLE)
        .select("id")
        .eq("campaign_id", campaign_id)
        .eq("is_active", True)
        .execute()
    )
    if not existing.data:
        return False

    sb.table(_TABLE).update({"is_active": False}).eq("campaign_id", campaign_id).eq(
        "is_active", True
    ).execute()
    log.info("Kampány némítása feloldva: #%s", campaign_id)
    return True


def is_muted(campaign_id: int) -> bool:
    """Van-e aktív, le nem járt némítás erre a kampányra."""
    res = (
        get_supabase()
        .table(_TABLE)
        .select("id")
        .eq("campaign_id", campaign_id)
        .eq("is_active", True)
        .gt("muted_until", _now_iso())
        .limit(1)
        .execute()
    )
    return bool(res.data)


def get_active_mute(campaign_id: int) -> dict[str, Any] | None:
    """Az aktív (le nem járt) némítás sora, vagy None."""
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("campaign_id", campaign_id)
        .eq("is_active", True)
        .gt("muted_until", _now_iso())
        .order("muted_until", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None
