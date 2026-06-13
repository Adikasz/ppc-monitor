"""
Kampány insight (campaign_insights tábla) adathozzáférés.

A scheduler óránként ide írja a kampányok lekért metrikáit (lásd
src.monitoring.metrics). A `/campaign info` és a (későbbi) trend-detektálás
innen olvas.

Séma: supabase/migrations/0004_campaign_insights.sql
    id, campaign_id, impressions, clicks, spend, conversions, conversion_value,
    ctr, cpc, cpa, roas, fetched_at, created_at
    + unique (campaign_id, óra) → óránként egy sor kampányonként

Függvények:
    insert_campaign_insight(campaign_id, insights) — egy snapshot beszúrása
    get_latest_insight(campaign_id)                — legutóbbi snapshot
    prune_old_insights(days)                       — 1 hétnél régebbi sorok törlése
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

_TABLE = "campaign_insights"

log = get_logger(__name__)


def insert_campaign_insight(campaign_id: int, insights: dict[str, Any]) -> bool:
    """Egy metrika-snapshot beszúrása a campaign_insights táblába.

    Óránkénti egyediség: a (campaign_id, óra) unique index miatt egy kampányra
    óránként egy sor kerül be. Ha az adott órában már van sor (pl. a ciklus
    kétszer fut), a unique violation-t elnyeljük és False-t adunk.

    Visszatérés:
        True  — új sor beszúrva
        False — már volt sor ebben az órában, vagy (logolt) hiba történt
    """
    payload: dict[str, Any] = {
        "campaign_id": campaign_id,
        "impressions": insights.get("impressions"),
        "clicks": insights.get("clicks"),
        "spend": insights.get("spend"),
        "conversions": insights.get("conversions"),
        "conversion_value": insights.get("conversion_value"),
        "ctr": insights.get("ctr"),
        "cpc": insights.get("cpc"),
        "cpa": insights.get("cpa"),
        "roas": insights.get("roas"),
        "fetched_at": insights.get("fetched_at") or datetime.now(timezone.utc).isoformat(),
    }

    try:
        get_supabase().table(_TABLE).insert(payload).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "23505" in msg or "duplicate key" in msg:
            # unique (campaign_id, óra) — ebben az órában már van sor
            log.debug("Insight dedup — ebben az órában már van sor (campaign=%s)", campaign_id)
            return False
        log.error("Insight insert hiba (campaign=%s): %s", campaign_id, msg)
        return False


def get_latest_insight(campaign_id: int) -> dict[str, Any] | None:
    """A kampány legutóbb lekért metrika-sora (fetched_at szerint). None ha nincs."""
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("campaign_id", campaign_id)
        .order("fetched_at", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def prune_old_insights(days: int = 7) -> int:
    """A `days` napnál régebbi insight sorok törlése (history-karbantartás).

    Visszatér a törölt sorok számával.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    res = (
        get_supabase()
        .table(_TABLE)
        .delete()
        .lt("fetched_at", cutoff)
        .execute()
    )
    deleted = len(res.data or [])
    if deleted:
        log.info("Insight prune: %d régi sor törölve (>%d nap)", deleted, days)
    return deleted
