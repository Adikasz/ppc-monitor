"""
Monitoring scheduler — óránkénti anomália-ciklus (APScheduler).

A bot event loopjában futó AsyncIOScheduler óránként (minden óra :00-kor)
végigfut az aktívan monitorozott kampányokon:

    1. Lekéri a kampány aktuális metrikáit (get_campaign_metrics — STUB a 9.
       lépésig, ott jön a valódi Meta/Google API hívás).
    2. A detektorral kiértékeli az anomáliákat.
    3. A talált anomáliákat alertként beszúrja (deduplikációval).

Indítás:
    A botban (src.bot.main on_ready) a `start_scheduler()` hívja meg, egyszer.

Hibatűrés:
    Egy kampány hibája nem állítja le a ciklust — logoljuk és megyünk tovább.
"""
from __future__ import annotations

import asyncio
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.config import get_config
from src.monitoring.detector import detect_anomalies_for_campaign
from src.monitoring.metrics import get_campaign_metrics
from src.monitoring.router import route_alert
from src.storage.alerts import insert_alert
from src.storage.insights import insert_campaign_insight, prune_old_insights
from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

log = get_logger(__name__)

_scheduler: AsyncIOScheduler | None = None


# ---------------------------------------------------------------------------
# Óránkénti ciklus
# ---------------------------------------------------------------------------

def _fetch_monitored_campaigns() -> list[dict[str, Any]]:
    """Aktívan monitorozott kampányok (is_monitored, nem paused/ended)."""
    return (
        get_supabase()
        .table("campaigns")
        .select("*")
        .eq("is_monitored", True)
        .neq("lifecycle_state", "paused")
        .neq("lifecycle_state", "ended")
        .execute()
        .data
        or []
    )


async def hourly_monitoring() -> None:
    """Egy monitoring ciklus: minden aktív kampány kiértékelése + alertek."""
    log.info("Monitoring ciklus indítva…")

    try:
        campaigns = await asyncio.to_thread(_fetch_monitored_campaigns)
    except Exception:
        log.exception("Monitoring: a kampánylista lekérése sikertelen — ciklus kihagyva")
        return

    new_alerts = 0
    fetched = 0
    for campaign in campaigns:
        cid = campaign.get("id")
        try:
            # 1) Valódi metrikák (Meta/Google). None → nem elérhető, skip.
            insights = await get_campaign_metrics(campaign)
            if not insights:
                log.warning("Monitoring: #%s metrics nem elérhető — kihagyva", cid)
                continue
            fetched += 1
            log.info(
                "Monitoring: #%s metrics OK (impr=%s, spend=%s, conv=%s)",
                cid, insights["impressions"], insights["spend"], insights["conversions"],
            )

            # 2) Insight perzisztálás (óránként egy sor kampányonként)
            await asyncio.to_thread(insert_campaign_insight, cid, insights)

            # 3) Anomália-detektálás + alertek + routing
            anomalies = await detect_anomalies_for_campaign(cid, insights)
            for a in anomalies:
                alert_row = await asyncio.to_thread(
                    insert_alert,
                    a["campaign_id"],
                    a["severity"],
                    a["metric"],
                    a.get("observed_value"),
                    a.get("threshold_value"),
                    a["message"],
                )
                if alert_row:
                    new_alerts += 1
                    # ROUTING — kiküldés a megfelelő csatornákra (Discord/ClickUp)
                    try:
                        await route_alert(alert_row)
                    except Exception as exc:  # noqa: BLE001
                        log.error(
                            "Monitoring: routing hiba (alert #%s): %s",
                            alert_row.get("id"), exc,
                        )
        except Exception as exc:  # noqa: BLE001 — egy kampány hibája ne állítsa le a ciklust
            log.error("Monitoring: kampány #%s hiba: %s", cid, exc)

    # History-karbantartás: 1 hétnél régebbi insightok törlése
    try:
        await asyncio.to_thread(prune_old_insights, 7)
    except Exception:  # noqa: BLE001
        log.exception("Monitoring: insight prune hiba")

    log.info(
        "Monitoring ciklus kész: %d kampány, %d metrics OK, %d új alert",
        len(campaigns), fetched, new_alerts,
    )


# ---------------------------------------------------------------------------
# Scheduler életciklus
# ---------------------------------------------------------------------------

def start_scheduler() -> AsyncIOScheduler:
    """Létrehozza és elindítja az óránkénti monitoring schedulert.

    Idempotens: ha már fut, a meglévő példányt adja vissza (az on_ready
    többször is lefuthat reconnectkor, ezért védjük a dupla indítás ellen).
    A scheduler a hívó (bot) futó event loopjához kötődik.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    timezone = get_config().timezone or "UTC"
    _scheduler = AsyncIOScheduler(timezone=timezone)
    # 'cron' minden óra :00 perckor (hour='*', minute=0)
    _scheduler.add_job(
        hourly_monitoring,
        trigger="cron",
        minute=0,
        id="hourly_monitoring",
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )
    _scheduler.start()
    log.info("Monitoring scheduler indítva (óránkénti ciklus, tz=%s)", timezone)
    return _scheduler


def shutdown_scheduler() -> None:
    """Scheduler leállítása (teszteléshez / graceful shutdownhoz)."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
