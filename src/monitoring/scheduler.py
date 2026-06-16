"""
Monitoring scheduler — óránkénti anomália-ciklus (APScheduler).

A bot event loopjában futó AsyncIOScheduler óránként (minden óra :00-kor)
végigfut az aktívan monitorozott kampányokon, ad_account-onként BATCH pull-lal
(16. lépés — 1 API hívás / fiók, nem 1 / kampány):

    1. Aktív kampányok lekérése (ad_account adatokkal), csoportosítás fiókonként.
    2. Fiókonként EGY batch metrika-hívás (Meta/Google), majd a kampányok
       összepárosítása external_campaign_id alapján.
    3. Insight perzisztálás + lifecycle auto-promóció + anomália-detektálás
       + alertek beszúrása (dedup) és routing.

Indítás:
    A botban (src.bot.main on_ready) a `start_scheduler()` hívja meg, egyszer.

Hibatűrés:
    Egy kampány hibája nem állítja le a ciklust — logoljuk és megyünk tovább.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.config import get_config
from src.integrations.discord_router import send_summary_to_user
from src.integrations.google_ads import GoogleAdsClient
from src.integrations.meta_ads import MetaAdsClient
from src.monitoring.detector import detect_anomalies_for_campaign
from src.monitoring.router import route_alert
from src.monitoring.summary import generate_daily_summary, generate_weekly_summary
from src.storage import campaigns as campaigns_storage
from src.storage import users as users_storage
from src.storage.alerts import insert_alert
from src.storage.insights import insert_campaign_insight, prune_old_insights
from src.utils.logging import get_logger

log = get_logger(__name__)

# Kis szünet ad_account-ok között (gyengéd az API-kra; Google ajánlás ~0.1s).
_INTER_ACCOUNT_DELAY_S = 0.1
# Lifecycle auto-promóció: ennyi nap után 'new'/'learning' → 'mature'.
_AUTO_PROMOTE_AFTER_DAYS = 14

_scheduler: AsyncIOScheduler | None = None


# ---------------------------------------------------------------------------
# Óránkénti ciklus — batch pull ad_account-onként (16. lépés)
# ---------------------------------------------------------------------------

async def _batch_pull(platform: str, ext_account_id: str, day: str) -> list[dict[str, Any]]:
    """Egy ad_account összes kampányának metrikái EGY API hívással.

    A platform-kliens get_instance() hiányzó token/SDK esetén RuntimeError-t
    dob — ezt a hívó (hourly_monitoring per-account try) kezeli.
    """
    if platform == "meta":
        client = MetaAdsClient.get_instance()
        return await asyncio.to_thread(
            client.get_all_campaigns_insights, ext_account_id, day, day
        )
    if platform == "google":
        client = GoogleAdsClient.get_instance()
        return await asyncio.to_thread(
            client.get_all_campaigns_metrics, ext_account_id, day, day
        )
    log.warning("Monitoring: ismeretlen platform %r — fiók kihagyva", platform)
    return []


def _older_than_days(iso_value: Any, days: int) -> bool:
    """True, ha az ISO időbélyeg régebbi mint `days` nap (tz-naiv → UTC)."""
    if not iso_value:
        return False
    try:
        dt = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt) > timedelta(days=days)


async def auto_promote_lifecycle(
    campaign: dict[str, Any],
    insight: dict[str, Any],
) -> bool:
    """'new'/'learning' kampány automatikus 'mature'-ré léptetése.

    Feltétel: a kampány `_AUTO_PROMOTE_AFTER_DAYS`+ napos (discovered_at vagy
    created_at alapján) ÉS volt már költése (insight['spend'] > 0). Ilyenkor a
    tanulási fázis lezárul, és a teljes (WARNING-ot is tartalmazó) monitoring
    bekapcsol. A scheduler a detektálás ELŐTT hívja.

    Visszatérés: True, ha most promótált; egyébként False.
    """
    state = (campaign.get("lifecycle_state") or "").lower()
    if state not in ("new", "learning"):
        return False

    try:
        has_spend = float(insight.get("spend") or 0) > 0
    except (TypeError, ValueError):
        has_spend = False
    if not has_spend:
        return False

    age_ref = campaign.get("discovered_at") or campaign.get("created_at")
    if not _older_than_days(age_ref, _AUTO_PROMOTE_AFTER_DAYS):
        return False

    updated = await asyncio.to_thread(
        campaigns_storage.set_lifecycle_state, campaign["id"], "mature"
    )
    if updated:
        log.info(
            "Auto-promóció → mature: #%s %s (%d+ nap, spend>0)",
            campaign["id"], campaign.get("name"), _AUTO_PROMOTE_AFTER_DAYS,
        )
        return True
    return False


async def hourly_monitoring() -> None:
    """Egy monitoring ciklus BATCH pull-lal: 1 API hívás / ad_account."""
    log.info("Monitoring ciklus indítva (batch)…")

    try:
        campaigns = await asyncio.to_thread(campaigns_storage.get_active_campaigns)
    except Exception:
        log.exception("Monitoring: a kampánylista lekérése sikertelen — ciklus kihagyva")
        return

    if not campaigns:
        log.info("Monitoring: nincs aktívan monitorozott kampány — ciklus kihagyva")
        return

    accounts = campaigns_storage.group_campaigns_by_account(campaigns)
    today = date.today().isoformat()

    fetched = 0
    promoted = 0
    new_alerts = 0

    for account_db_id, account_campaigns in accounts.items():
        account = account_campaigns[0].get("ad_accounts") or {}
        platform = account.get("platform")
        ext_account_id = account.get("external_account_id")

        # 1 hívás / fiók — bármilyen hiba esetén ezt a fiókot kihagyjuk, a
        # ciklus folytatódik a többivel (fault isolation).
        try:
            all_insights = await _batch_pull(platform, ext_account_id, today)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Monitoring: account #%s (%s/%s) batch pull hiba: %s — kihagyva",
                account_db_id, platform, ext_account_id, exc,
            )
            await asyncio.sleep(_INTER_ACCOUNT_DELAY_S)
            continue

        # Match: external_campaign_id → insight
        insights_map = {str(i["external_campaign_id"]): i for i in all_insights}

        for campaign in account_campaigns:
            cid = campaign.get("id")
            insight = insights_map.get(str(campaign.get("external_campaign_id")))
            if not insight:
                continue
            fetched += 1
            try:
                # 1) Insight perzisztálás (óránként egy sor kampányonként)
                await asyncio.to_thread(insert_campaign_insight, cid, insight)

                # 2) Lifecycle auto-promóció — a detektálás ELŐTT
                if await auto_promote_lifecycle(campaign, insight):
                    promoted += 1

                # 3) Anomália-detektálás + alertek + routing
                anomalies = await detect_anomalies_for_campaign(cid, insight)
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
                        try:
                            await route_alert(alert_row)
                        except Exception as exc:  # noqa: BLE001
                            log.error(
                                "Monitoring: routing hiba (alert #%s): %s",
                                alert_row.get("id"), exc,
                            )
            except Exception as exc:  # noqa: BLE001 — egy kampány hibája ne állítsa le a ciklust
                log.error("Monitoring: kampány #%s hiba: %s", cid, exc)

        await asyncio.sleep(_INTER_ACCOUNT_DELAY_S)

    # History-karbantartás: 1 hétnél régebbi insightok törlése
    try:
        await asyncio.to_thread(prune_old_insights, 7)
    except Exception:  # noqa: BLE001
        log.exception("Monitoring: insight prune hiba")

    log.info(
        "Monitoring ciklus kész: %d account, %d kampány, %d metrics, "
        "%d auto-promóció, %d új alert",
        len(accounts), len(campaigns), fetched, promoted, new_alerts,
    )


# ---------------------------------------------------------------------------
# Napi / heti összefoglaló jobok (13. lépés)
# ---------------------------------------------------------------------------

async def _send_summaries(*, is_weekly: bool) -> None:
    """Minden aktív usernek összefoglaló kiküldése (daily vagy weekly)."""
    label = "heti" if is_weekly else "napi"
    try:
        users = await asyncio.to_thread(users_storage.list_users, active_only=True)
    except Exception:
        log.exception("Összefoglaló (%s): a userlista lekérése sikertelen — kihagyva", label)
        return

    sent = 0
    for user in users:
        uid = user.get("id")
        try:
            if is_weekly:
                summary = await generate_weekly_summary(uid)
            else:
                summary = await generate_daily_summary(uid)
            res = await send_summary_to_user(user, summary, is_weekly=is_weekly)
            if res:
                sent += 1
        except Exception as exc:  # noqa: BLE001 — egy user hibája ne állítsa le a kört
            log.error("Összefoglaló (%s) hiba user #%s: %s", label, uid, exc)

    log.info("Összefoglaló kész (%s): %d/%d usernek kiküldve", label, sent, len(users))


async def daily_summary_job() -> None:
    """Napi összefoglaló (kedd–péntek reggel; hétfőn a heti összefoglaló váltja)."""
    log.info("Napi összefoglaló job indítva…")
    await _send_summaries(is_weekly=False)


async def weekly_summary_job() -> None:
    """Hétvégi összefoglaló (hétfő reggel) — a hétfői napi összefoglalót VÁLTJA."""
    log.info("Hétvégi összefoglaló job indítva…")
    await _send_summaries(is_weekly=True)


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

    # Napi összefoglaló: KEDD–PÉNTEK 09:00. Hétfőn SZÁNDÉKOSAN nem fut, mert a
    # heti (hétvégi) összefoglaló váltja — így senki nem kap két üzenetet hétfőn.
    _scheduler.add_job(
        daily_summary_job,
        trigger="cron",
        hour=9,
        minute=0,
        day_of_week="tue-fri",
        id="daily_summary",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )

    # Hétvégi összefoglaló: HÉTFŐ 09:00 (péntek 22:00 → hétfő 08:00 ablak).
    _scheduler.add_job(
        weekly_summary_job,
        trigger="cron",
        hour=9,
        minute=0,
        day_of_week="mon",
        id="weekly_summary",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )

    _scheduler.start()
    log.info(
        "Monitoring scheduler indítva (óránkénti ciklus + napi/heti összefoglaló, tz=%s)",
        timezone,
    )
    return _scheduler


def shutdown_scheduler() -> None:
    """Scheduler leállítása (teszteléshez / graceful shutdownhoz)."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
