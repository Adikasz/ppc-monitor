"""
Napi és heti összefoglaló-generátor (13. lépés).

A scheduler (reggel 9-kor) és a manuális `/summary` parancs is ezt hívja.
Felhasználónként összesíti a hozzá rendelt kampányokra érkezett riasztásokat
egy időablakban, és visszaad egy struktúrát, amit a discord_router formáz meg.

Időablakok (Europe/Budapest, config.timezone):
    - daily : tegnap 00:00 → ma 00:00
    - weekly: a legutóbbi hétvége — péntek 22:00 → hétfő 08:00

A riasztás időbélyege `detected_at` (lásd alerts séma — NEM `created_at`).

A problémalista (`top_issues`) NINCS top-N-re vágva: az időablak MINDEN
riasztása szerepel benne (csak egy magas biztonsági plafon védi, lásd
`_MAX_ISSUE_LINES`). A megjelenítést — rövid listánál lapos felsorolás,
hosszúnál súlyosság szerinti csoportosítás — a `discord_router._format_summary`
végzi, a 2000 karakteres Discord-limitet pedig a küldő bontja több üzenetre.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.config import get_config
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import alerts as alerts_storage
from src.storage import assignments as assignments_storage
from src.utils.logging import get_logger

log = get_logger(__name__)

# Severity prioritás a problémalista rendezéséhez (kisebb = előrébb).
_SEVERITY_RANK = {"critical": 0, "warning": 1, "insight": 2}

# NINCS "top N" vágás: az ügyfél elvárása, hogy MINDEN aznapi CRITICAL és
# WARNING szerepeljen az összefoglalóban (korábban fix top-5 volt, így egy
# 20-alertes napból 15 riasztás némán kimaradt).
#
# Ez itt csak egy végső biztonsági plafon egy elszabadult nap ellen (pl. API-
# hiba miatt minden kampányra alert keletkezik) — a Discord-üzenetek számát
# tartja kordában. Ha életbe lép, NEM néma: a `issues_truncated` mezőt a
# formázó kiírja ("… és még N további"), és WARNING logot is kap.
# A severity-sorrend miatt előbb az insightok, majd a warningok esnek ki —
# critical csak akkor, ha egyetlen napon 200+ kritikus riasztás van.
_MAX_ISSUE_LINES = 200


def _tz() -> ZoneInfo:
    return ZoneInfo(get_config().timezone or "UTC")


def daily_range(now: datetime | None = None) -> tuple[datetime, datetime]:
    """A TELJES előző naptári nap a konfigurált időzónában: tegnap 00:00 → ma 00:00.

    A visszaadott pár közvetlenül a lekérdezés két határa lesz:
        detected_at >= from_dt  ÉS  detected_at < to_dt
    (lásd `storage.alerts.get_alerts_for_user_in_range` — `.gte()` / `.lt()`).

    Fél-nyitott intervallum, szándékosan:
      - az alsó határ INKLUZÍV → a tegnap 00:00:00-kor keletkezett alert benne van
      - a felső határ EXKLUZÍV → a ma 00:00:00-kor keletkezett alert már a
        következő napé, viszont a tegnap 23:59:59.999999 még benne van
    Ez pontosabb, mint egy `<= 23:59:59` felső határ, ami a másodperc törtrészében
    keletkezett riasztásokat némán elhagyná.

    NEM gördülő 24 óra: a `now`-ból CSAK a naptári napot vesszük, az időt
    nullázzuk — így teljesen mindegy, hogy a scheduler 09:00-kor futtatja
    (scheduler.py: cron hour=9, day_of_week="tue-fri"), vagy valaki kézzel
    kéri le a `/summary`-val délután; az ablak ugyanaz.

    Óraátállításkor is a teljes napot fedi: a `today0 - timedelta(days=1)`
    fali-óra aritmetika (a tegnapi 00:00-t adja, nem "24 órával korábbat"),
    így az őszi nap 25, a tavaszi 23 órányi valós időt jelent.
    """
    tz = _tz()
    now = now.astimezone(tz) if now else datetime.now(tz)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return today0 - timedelta(days=1), today0


def weekend_range(now: datetime | None = None) -> tuple[datetime, datetime]:
    """A legutóbbi hétvége: péntek 22:00 → hétfő 08:00 a konfigurált időzónában.

    A hét hétfőjének 08:00-ját vesszük felső határnak (weekday(): hétfő=0),
    az alsó határ az azt megelőző péntek 22:00.
    """
    tz = _tz()
    now = now.astimezone(tz) if now else datetime.now(tz)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=8, minute=0, second=0, microsecond=0
    )
    friday = (monday - timedelta(days=3)).replace(
        hour=22, minute=0, second=0, microsecond=0
    )
    return friday, monday


def workweek_range(now: datetime | None = None) -> tuple[datetime, datetime]:
    """A HÉT MUNKANAPJAI: hétfő 00:00 → szombat 00:00 a konfigurált időzónában.

    Vagyis a hétfő–péntek öt teljes naptári nap. A `daily_range` mintáját
    követi, a határok ugyanúgy fél-nyitottak:
        detected_at >= hétfő 00:00  ÉS  detected_at < szombat 00:00
    Így a péntek 23:59:59.999999-kor keletkezett riasztás még benne van, a
    szombat 00:00:00-kor keletkezett viszont már nem.

    NEM gördülő "utolsó 5×24 óra": a `now`-ból CSAK a naptári napot vesszük
    (a `weekday()` adja, hányadik napon állunk), az időt nullázzuk. A péntek
    17:05-ös ütemezett futás és egy kézi, délelőtti lekérés tehát PONTOSAN
    ugyanazt az ablakot adja.

    Óraátállításkor is a teljes naptári napokat fedi: a `timedelta` itt
    fali-óra aritmetika (hétfő 00:00 + 5 nap = szombat 00:00), nem "120 óra".

    FIGYELEM: az ütemezett job péntek 17:05-kor fut (a munkanap végén), amikor
    a péntek még nem telt el teljesen. Az ablak felső határa szándékosan mégis
    szombat 00:00 — így a szombat 00:00-ig keletkező riasztások mind beleesnek,
    és az ablak nem függ a futás órájától. A 17:05 után keletkező pénteki
    riasztások értelemszerűen már nem szerepelhetnek a 17:05-kor kiküldött
    üzenetben (lásd a scheduler kommentjét a vállalt résről).
    """
    tz = _tz()
    now = now.astimezone(tz) if now else datetime.now(tz)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monday0 = today0 - timedelta(days=now.weekday())   # weekday(): hétfő=0
    return monday0, monday0 + timedelta(days=5)        # szombat 00:00


def _multi_account_label(
    ad_account: dict[str, Any],
    cache: dict[tuple[int, str], int],
) -> str | None:
    """Fiók-megkülönböztető címke, ha az ügyfélnek 2+ fiókja van ugyanazon a platformon.

    None-t ad vissza, ha a kliensnek csak 1 fiókja van az adott platformon
    (redundáns lenne kiírni). A `cache` elkerüli az ismételt lekérdezést
    ugyanarra a (client_id, platform) párra egy összefoglaló-generáláson belül.
    """
    client_id = ad_account.get("client_id")
    platform = ad_account.get("platform")
    if client_id is None or not platform:
        return None

    key = (client_id, platform)
    if key not in cache:
        siblings = ad_accounts_storage.get_ad_accounts_for_client(
            client_id, platform=platform, active_only=False
        )
        cache[key] = len(siblings)

    if cache[key] < 2:
        return None

    return ad_account.get("account_name") or ad_account.get("external_account_id") or "?"


def _build_summary_sync(user_id: int, from_dt: datetime, to_dt: datetime) -> dict[str, Any]:
    """Szinkron összesítés (to_thread-ből hívva)."""
    campaign_ids = assignments_storage.get_campaign_ids_for_user(user_id)
    total_campaigns = len(campaign_ids)

    alerts = alerts_storage.get_alerts_for_user_in_range(user_id, from_dt, to_dt)

    critical_count = sum(1 for a in alerts if (a.get("severity") or "").lower() == "critical")
    warning_count = sum(1 for a in alerts if (a.get("severity") or "").lower() == "warning")

    # Problémalista: severity-prioritás szerint. A lista a DB-ből már
    # detected_at DESC-ben jön, és a sorted() stabil → azonos severity-n belül
    # megmarad a "legfrissebb előre" sorrend.
    ordered = sorted(
        alerts,
        key=lambda a: _SEVERITY_RANK.get((a.get("severity") or "").lower(), 99),
    )
    account_cache: dict[tuple[int, str], int] = {}
    top_issues = []
    for a in ordered[:_MAX_ISSUE_LINES]:
        campaign = a.get("campaigns") or {}
        ad_account = campaign.get("ad_accounts") or {}
        client = ad_account.get("clients") or {}
        top_issues.append({
            "client": client.get("name"),
            "campaign": campaign.get("name") or f"#{a.get('campaign_id')}",
            "platform": ad_account.get("platform"),
            "account_label": _multi_account_label(ad_account, account_cache),
            "severity": (a.get("severity") or "").lower(),
            "message": a.get("message") or a.get("metric") or "",
            # Az észlelés időpontja a sor végére (a formázó teszi ki, helyi
            # időzónára váltva) — az OM így látja, mikor keletkezett a gond.
            "detected_at": a.get("detected_at"),
        })

    issues_truncated = max(0, len(ordered) - len(top_issues))
    if issues_truncated:
        log.warning(
            "Összefoglaló user #%s: %d riasztásból csak %d fér a listába "
            "(biztonsági plafon: %d) — %d sor összevontan jelenik meg",
            user_id, len(ordered), len(top_issues), _MAX_ISSUE_LINES, issues_truncated,
        )

    campaigns_with_alerts = {a.get("campaign_id") for a in alerts if a.get("campaign_id") is not None}
    healthy_campaigns = max(0, total_campaigns - len(campaigns_with_alerts & set(campaign_ids)))

    return {
        "total_campaigns": total_campaigns,
        # FONTOS: a critical_count / warning_count MINDIG a teljes időablak
        # összesítése — nem a `top_issues` megjelenített sorainak száma.
        # A formázó ezt írja ki "Kritikus: X | Figyelmeztetés: Y" néven.
        "critical_count": critical_count,
        "warning_count": warning_count,
        "alert_count": len(alerts),
        "top_issues": top_issues,
        "issues_truncated": issues_truncated,
        "healthy_campaigns": healthy_campaigns,
        "from": from_dt.isoformat(),
        "to": to_dt.isoformat(),
    }


async def generate_daily_summary(user_id: int) -> dict[str, Any]:
    """Napi összefoglaló a userhez rendelt kampányok tegnapi alertjeiből."""
    from_dt, to_dt = daily_range()
    summary = await asyncio.to_thread(_build_summary_sync, user_id, from_dt, to_dt)
    log.info(
        "Napi összefoglaló user #%s: %d kampány, %d alert (crit=%d, warn=%d)",
        user_id, summary["total_campaigns"], summary["alert_count"],
        summary["critical_count"], summary["warning_count"],
    )
    return summary


async def generate_weekly_summary(user_id: int) -> dict[str, Any]:
    """Hétvégi összefoglaló (péntek 22:00 → hétfő 08:00)."""
    from_dt, to_dt = weekend_range()
    summary = await asyncio.to_thread(_build_summary_sync, user_id, from_dt, to_dt)
    log.info(
        "Hétvégi összefoglaló user #%s: %d kampány, %d alert (crit=%d, warn=%d)",
        user_id, summary["total_campaigns"], summary["alert_count"],
        summary["critical_count"], summary["warning_count"],
    )
    return summary


async def generate_workweek_summary(user_id: int) -> dict[str, Any]:
    """Heti MUNKANAPI összefoglaló (hétfő 00:00 → szombat 00:00).

    Ugyanazt az aggregáló logikát használja, mint a napi és a hétvégi
    összefoglaló — csak az időablak más (lásd `workweek_range`).
    """
    from_dt, to_dt = workweek_range()
    summary = await asyncio.to_thread(_build_summary_sync, user_id, from_dt, to_dt)
    log.info(
        "Heti munkanapi összefoglaló user #%s: %d kampány, %d alert (crit=%d, warn=%d)",
        user_id, summary["total_campaigns"], summary["alert_count"],
        summary["critical_count"], summary["warning_count"],
    )
    return summary
