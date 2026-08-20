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
from src.monitoring.ai_insights import generate_ai_insight
from src.monitoring.detector import detect_anomalies_for_campaign
from src.monitoring.insight_engine import detect_insights_for_campaign
from src.monitoring.router import route_alert
from src.monitoring.summary import SUMMARY_KINDS
from src.monitoring.token_monitor import token_health_check
from src.monitoring.weekly_action_report import generate_weekly_action_reports
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import campaigns as campaigns_storage
from src.storage import clients as clients_storage
from src.storage import insights_history as insights_history_storage
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

async def _send_summaries(*, is_weekly: bool = False, kind: str | None = None) -> None:
    """Minden aktív usernek összefoglaló kiküldése (daily / weekend / workweek).

    A generátor a `summary.SUMMARY_KINDS` EGYETLEN forrásból jön — ugyanaz,
    amit a `/summary` és a `/my summary` is hív.
    """
    kind = kind or ("weekend" if is_weekly else "daily")
    generate, label = SUMMARY_KINDS[kind]
    label = label.lower()

    try:
        users = await asyncio.to_thread(users_storage.list_users, active_only=True)
    except Exception:
        log.exception("Összefoglaló (%s): a userlista lekérése sikertelen — kihagyva", label)
        return

    sent = 0
    for user in users:
        uid = user.get("id")
        try:
            summary = await generate(uid)
            res = await send_summary_to_user(user, summary, kind=kind)
            if res:
                sent += 1
        except Exception as exc:  # noqa: BLE001 — egy user hibája ne állítsa le a kört
            log.error("Összefoglaló (%s) hiba user #%s: %s", label, uid, exc)

    log.info("Összefoglaló kész (%s): %d/%d usernek kiküldve", label, sent, len(users))


async def daily_summary_job() -> None:
    """Napi összefoglaló (kedd–péntek reggel; hétfőn a heti összefoglaló váltja)."""
    log.info("Napi összefoglaló job indítva…")
    await _send_summaries(kind="daily")


async def weekly_summary_job() -> None:
    """Hétvégi összefoglaló (hétfő reggel) — a hétfői napi összefoglalót VÁLTJA."""
    log.info("Hétvégi összefoglaló job indítva…")
    await _send_summaries(kind="weekend")


async def workweek_summary_job() -> None:
    """Heti MUNKANAPI összefoglaló (péntek délután) — hétfő 00:00 → szombat 00:00.

    A hét UTOLSÓ összefoglalója: a pénteki napi összefoglaló (reggel 09:00, a
    csütörtöki napról) mellé délután megy ki a teljes hétfő–pénteki kép.
    Külön üzenet, nem váltja ki a napit.
    """
    log.info("Heti munkanapi összefoglaló job indítva…")
    await _send_summaries(kind="workweek")


# ---------------------------------------------------------------------------
# Meta token egészség (17. lépés) — heti ellenőrzés
# ---------------------------------------------------------------------------

async def weekly_token_check() -> None:
    """Heti Meta token-ellenőrzés (lejárat/érvénytelenség → admin értesítés)."""
    log.info("Heti Meta token-ellenőrzés indítva…")
    try:
        await token_health_check()
    except Exception:  # noqa: BLE001 — a token-check sosem buktathatja meg a schedulert
        log.exception("Heti token-ellenőrzés hiba")


# ---------------------------------------------------------------------------
# Napi INSIGHT scan (18. lépés) — szabály-alapú + AI optimalizálási javaslatok
# ---------------------------------------------------------------------------

# Insight csak 'mature' kampányra (a 'new'/'learning' tanulási fázis kihagyva).
_INSIGHT_LIFECYCLE = "mature"
# Ennyi history sor kell legalább, hogy értelmes insightot adjunk.
_INSIGHT_MIN_HISTORY_ROWS = 3


def _resolve_client_for_account(ad_account_id: int | None) -> dict[str, Any] | None:
    """Fiók → ügyfél (insights_enabled + név) az AI insighthoz. None ha nem feloldható."""
    if not ad_account_id:
        return None
    account = ad_accounts_storage.get_ad_account(ad_account_id)
    if not account:
        return None
    return clients_storage.get_client(account.get("client_id"))


async def daily_insight_scan(
    *,
    limit: int | None = None,
    client_id: int | None = None,
) -> dict[str, int]:
    """Napi INSIGHT scan (08:00, hétköznap) — szabály-alapú + AI javaslatok.

    Csak 'mature' lifecycle-ú kampányokat néz (a tanulási fázis nem kap insightot).
    Kampányonként:
      1. utolsó 7 napi history (>= 3 sor kell, különben skip),
      2. hatékony KPI (campaign→account→client→default),
      3. szabály-alapú insightok (peer = ugyanazon fiók kampányai, ROAS-szal),
      4. AI javaslat, ha a kliensnél insights_enabled=True,
      5. alert beszúrás (dedup) + routing.

    A routing tiszteli a csendes időt (nincs bypass): a scan 08:00-kor (a quiet
    hours VÉGÉN, hétköznap) fut, így az insight munkaidőben megy ki, nem éjjel.
    A manuális futtatás (`/insight scan-now`) SEM bypassolja — így a teszt
    hűen azt mutatja, amit az ütemezett futás produkálna. A `quiet_hours`
    számláló teszi láthatóvá, ha emiatt nem ment ki semmi.

    Paraméterek:
        limit     — legfeljebb ennyi mature kampányt dolgoz fel (teszt/manuális)
        client_id — csak ennek az ügyfélnek a kampányai (gyorsabb teszteléshez)

    Visszatérés — a `/insight scan-now` ebből építi a válaszát, és ebből készül
    a záró log sor is. MINDIG teljes kulcskészlettel tér vissza, a korai
    kilépési ágakon is, hogy a hívónak ne kelljen `.get()`-elnie:

        {"total": int,             # a scan által vizsgált mature kampányok
         "insights": int,          # beszúrt (nem deduplikált) insight
         "skipped_no_history": int,
         "failed": int,
         "routed": int,            # ténylegesen kiment Discordra
         "quiet_hours": int}       # csendes idő miatt elnyomva
    """
    log.info("Napi insight scan indítva…")
    stats = {
        "total": 0, "insights": 0, "skipped_no_history": 0,
        "failed": 0, "routed": 0, "quiet_hours": 0,
    }

    try:
        campaigns = await asyncio.to_thread(campaigns_storage.get_active_campaigns)
    except Exception:
        log.exception("Insight scan: a kampánylista lekérése sikertelen — kihagyva")
        return stats

    by_account = campaigns_storage.group_campaigns_by_account(campaigns)
    mature = [
        c for c in campaigns
        if (c.get("lifecycle_state") or "").lower() == _INSIGHT_LIFECYCLE
    ]

    # Ügyfél-szűkítés: a kampány sorban NINCS client_id (a get_active_campaigns
    # csak az ad_accounts alap-mezőit ágyazza be), ezért a kliens FIÓKJAIN
    # keresztül szűrünk.
    if client_id is not None:
        account_ids = {
            a["id"] for a in await asyncio.to_thread(
                ad_accounts_storage.get_ad_accounts_for_client,
                client_id, active_only=False,
            )
            if a.get("id") is not None
        }
        mature = [c for c in mature if c.get("ad_account_id") in account_ids]

    if limit is not None:
        mature = mature[:limit]

    stats["total"] = len(mature)

    if not mature:
        log.info(
            "Insight scan: nincs 'mature' kampány%s — kihagyva",
            f" (ügyfél #{client_id})" if client_id is not None else "",
        )
        return stats

    client_cache: dict[int, dict[str, Any] | None] = {}
    roas_cache: dict[int, dict[int, float | None]] = {}

    for campaign in mature:
        cid = campaign.get("id")
        acct_id = campaign.get("ad_account_id")
        try:
            history = await asyncio.to_thread(
                insights_history_storage.get_insights_history, cid, 7
            )
            if len(history) < _INSIGHT_MIN_HISTORY_ROWS:
                # Korábban ez NÉMÁN ugrott — így egy üres scan
                # megkülönböztethetetlen volt egy elhasalt scantől.
                stats["skipped_no_history"] += 1
                log.debug(
                    "Insight scan: kampány #%s kihagyva — %d history sor a %d-es "
                    "minimum alatt",
                    cid, len(history), _INSIGHT_MIN_HISTORY_ROWS,
                )
                continue

            kpi = await asyncio.to_thread(insights_history_storage.get_merged_kpis, cid)

            # Peer ROAS map fiókonként cache-elve (büdzsé-átcsoportosítás insighthoz).
            peers_raw = by_account.get(acct_id, [])
            if acct_id not in roas_cache:
                peer_ids = [p["id"] for p in peers_raw if p.get("id") is not None]
                roas_cache[acct_id] = await asyncio.to_thread(
                    insights_history_storage.get_latest_roas_map, peer_ids
                )
            rmap = roas_cache[acct_id]
            peers = [{**p, "roas": rmap.get(p.get("id"))} for p in peers_raw]

            # 1) szabály-alapú insightok (7 napos deduppal)
            insights = await detect_insights_for_campaign(campaign, history, kpi, peers)

            # 2) AI javaslat — csak ha a kliensnél engedélyezett
            if acct_id not in client_cache:
                client_cache[acct_id] = await asyncio.to_thread(
                    _resolve_client_for_account, acct_id
                )
            client = client_cache[acct_id]
            if client and client.get("insights_enabled"):
                enriched = {**campaign, "client_name": client.get("name")}
                ai = await generate_ai_insight(enriched, history, kpi)
                if ai:
                    insights.append({
                        "campaign_id": cid,
                        "severity": "insight",
                        "metric": "ai_recommendation",
                        "observed_value": None,
                        "threshold_value": None,
                        "message": f"🤖 AI javaslat: {ai}",
                    })

            # 3) beszúrás + routing (ugyanaz a csővezeték, mint az anomáliáknál)
            for ins in insights:
                alert_row = await asyncio.to_thread(
                    insert_alert,
                    ins["campaign_id"], ins["severity"], ins["metric"],
                    ins.get("observed_value"), ins.get("threshold_value"), ins["message"],
                )
                if alert_row:
                    stats["insights"] += 1
                    try:
                        # NEM bypassoljuk a csendes időt: az insight is csak
                        # munkaidőben (08:00–17:00, hétköznap) menjen ki. A scan
                        # 08:00-kor fut, így nem nyomódik el.
                        routing = await route_alert(alert_row)
                        if routing.get("routed"):
                            stats["routed"] += 1
                        elif routing.get("reason") == "quiet_hours":
                            stats["quiet_hours"] += 1
                    except Exception:
                        # `exception` (nem `error`): traceback nélkül nem
                        # derül ki, MI hasalt el — a hiányzó stack trace miatt
                        # maradt sokáig rejtve a `timedelta` NameError is.
                        log.exception(
                            "Insight routing hiba (alert #%s)", alert_row.get("id"),
                        )
        except Exception:  # noqa: BLE001 — egy kampány hibája ne állítsa le a scant
            stats["failed"] += 1
            log.exception("Insight scan: kampány #%s hiba", cid)

    log.info(
        "Insight scan kész: %d insight generálva, %d kampány kihagyva (kevés adat), "
        "%d kampány hibázott, %d kampány vizsgálva összesen "
        "(kiküldve: %d, csendes idő miatt elnyomva: %d)",
        stats["insights"], stats["skipped_no_history"], stats["failed"],
        stats["total"], stats["routed"], stats["quiet_hours"],
    )
    return stats


# ---------------------------------------------------------------------------
# Heti riport-összefoglaló & akciójavaslat — hétfő 08:00
# ---------------------------------------------------------------------------

async def weekly_action_report_job() -> dict[str, Any]:
    """Heti riport minden jogosult ügyfélre (ClickUp Doc + Claude elemzés).

    SZÁNDÉKOSAN egysoros delegálás: a tényleges logika a
    `weekly_action_report.generate_weekly_action_reports`-ban él, és a
    `/report weekly-now` parancs UGYANEZT hívja. Ha itt bármi „csak a cronhoz"
    tartozó extra logika lenne, a kézi teszt megint mást futtatna, mint az éles
    ütemezés — pontosan az a hiba, ami az insight scan-nél hetekig rejtve maradt.
    """
    return await generate_weekly_action_reports()


# ---------------------------------------------------------------------------
# Auto-resume (25. lépés) — lejárt szüneteltetésű kampányok visszaállítása
# ---------------------------------------------------------------------------

async def auto_resume_job() -> None:
    """A `/client pause`-zal szüneteltetett, határidőt elért kampányok visszaállítása.

    A `paused` + lejárt `lifecycle_until` kampányokat `mature`-re állítja. Naponta
    fut; a határidő nélkül (kézzel) szüneteltetett kampányokat nem érinti.
    """
    log.info("Auto-resume job indítva…")
    try:
        n = await asyncio.to_thread(campaigns_storage.resume_due_paused_campaigns)
    except Exception:  # noqa: BLE001 — sosem buktathatja meg a schedulert
        log.exception("Auto-resume job hiba")
        return
    log.info("Auto-resume job kész: %d kampány visszaállítva", n)


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

    # Heti MUNKANAPI összefoglaló: PÉNTEK 17:05 — a hét utolsó összefoglalója,
    # a teljes hétfő 00:00 → szombat 00:00 ablakról. A pénteki napi
    # összefoglalót (09:00, a csütörtöki napról) NEM váltja ki: külön üzenet.
    #
    # Miért 17:05? A munkanap vége — üzleti döntés, hogy a jelentés a nap
    # lezárásakor érkezzen. Reggel azért nem jó, mert akkor a péntek még alig
    # kezdődött el, így a "hétfő–péntek" kép csonka lenne.
    #
    # FIGYELEM — ez NEM esik egybe a csendes idő kezdetével: a .env.example
    # szerint QUIET_HOURS_START=18, tehát a 17:05–18:00 sáv MÉG AKTÍV. Az ott
    # keletkező riasztásokról valós időben megy értesítés (a router még nem
    # némít), de ebbe a heti összefoglalóba már nem kerülnek bele, és a hétfői
    # hétvégi összefoglaló is csak péntek 22:00-tól számol. Tudatosan vállalt
    # rés; ha meg kell szüntetni, a job 18:05-re állítása fedi le.
    #
    # (Az ablak felső határa ettől függetlenül fix szombat 00:00 — lásd
    # summary.workweek_range: a futás órája nem befolyásolja az ablakot, csak
    # azt, hogy annak meddig tartó részéről LÉTEZIK már adat.)
    _scheduler.add_job(
        workweek_summary_job,
        trigger="cron",
        hour=17,
        minute=5,
        day_of_week="fri",
        id="workweek_summary",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )

    # Heti Meta token-ellenőrzés: HÉTFŐ 08:00 (a napi munka előtt, hogy időben
    # kiderüljön, ha a token lejárt vagy hamarosan lejár).
    _scheduler.add_job(
        weekly_token_check,
        trigger="cron",
        hour=8,
        minute=0,
        day_of_week="mon",
        id="weekly_token_check",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )

    # Napi INSIGHT scan: HÉTKÖZNAP 08:00 (18. lépés). Reggel, a csendes idő VÉGÉN
    # fut, hogy a friss javaslatok ott legyenek az OM csatornáiban, de NE éjjel
    # (02:00) pingeljenek — az insight is tiszteli a quiet hours-t (a scan nem
    # bypassolja). Hétvégén nem fut (akkor nincs aktív kampánykezelés).
    _scheduler.add_job(
        daily_insight_scan,
        trigger="cron",
        hour=8,
        minute=0,
        day_of_week="mon-fri",
        id="daily_insight_scan",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )

    # Heti riport: HÉTFŐ 08:00 — az ELŐZŐ teljes hét (hétfő–vasárnap) ügyfél-
    # szintű összesítése, Claude elemzéssel, ClickUp Docba.
    #
    # Miért 08:00 hétfőn: a hét első munkaórája, így a riport ott van, mielőtt
    # az OM-ek nekiállnának a hétnek. Ugyanebben a percben fut a heti
    # token-ellenőrzés és a napi insight scan is — mindhárom az event loopban,
    # egymástól függetlenül; egyik sem blokkolja a másikat, mert mindegyik
    # `asyncio.to_thread`-be teszi a szinkron DB/HTTP hívásait.
    #
    # Az ablakot NEM a futás órája határozza meg (lásd
    # `weekly_action_report.previous_week_range`): egy késleltetett vagy kézzel
    # indított futás is pontosan ugyanarra a lezárult hétre számol.
    _scheduler.add_job(
        weekly_action_report_job,
        trigger="cron",
        hour=8,
        minute=0,
        day_of_week="mon",
        id="weekly_action_report",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )

    # Auto-resume: MINDEN NAP 06:00 — a lejárt szüneteltetésű kampányok (25. lépés
    # /client pause) visszaállítása mature-re.
    _scheduler.add_job(
        auto_resume_job,
        trigger="cron",
        hour=6,
        minute=0,
        id="auto_resume_paused",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )

    _scheduler.start()
    log.info(
        "Monitoring scheduler indítva (óránkénti ciklus + napi összefoglaló 09:00 "
        "+ hétvégi összefoglaló hétfő 09:00 + heti munkanapi összefoglaló péntek "
        "17:05 + napi insight scan 08:00 + heti riport hétfő 08:00, tz=%s)",
        timezone,
    )
    return _scheduler


def shutdown_scheduler() -> None:
    """Scheduler leállítása (teszteléshez / graceful shutdownhoz)."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
