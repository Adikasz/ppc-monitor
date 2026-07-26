"""Azonnali (real-time) fiók-összefoglaló — `/account summary-now`.

Különbség a `/summary type:daily`-hoz képest:

    /summary daily      — NEM indít adatlekérést; a MÁR TÁROLT alertekből
                          épít összefoglalót az utolsó lezárt napra (tegnap).
    /account summary-now — ÉLŐBEN lehúzza a MAI adatokat a platform API-ból
                          (1 batch hívás / fiók), lefuttatja rajtuk ugyanazt
                          az anomália-detektort, mint az óránkénti ciklus, és
                          azonnal visszaadja az eredményt.

Hatókör — SZÁNDÉKOSAN egy fiók:
    A teljes flotta ~100 ad_account, fiókonként egy batch API hívással ez
    3-5 percet és ~100 hívást jelentene egyetlen parancsra. Egy fiókra
    viszont 1 hívás és néhány másodperc. Ezért a parancs fiók-szintű.

MELLÉKHATÁS-MENTES (fontos):
    Ez egy read-only health check. NEM ír a campaign_insights táblába, NEM
    szúr be alerteket és NEM küld riasztást senkinek — különben minden
    lefuttatás megduplázná az órás ciklus alertjeit és spamelné az OM-eket.
    A `detect_anomalies_for_campaign` maga is tiszta függvény: kiszámolja az
    anomáliákat, de nem perzisztál (az insert/route a scheduler dolga).

    Egy kivétel a lifecycle auto-promóció: azt NEM futtatjuk itt, mert az
    állapotot írna.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.config import get_config
from src.integrations.google_ads import GoogleAdsClient
from src.integrations.meta_ads import MetaAdsClient
from src.monitoring.detector import detect_anomalies_for_campaign
from src.storage import campaigns as campaigns_storage
from src.utils.logging import get_logger

log = get_logger(__name__)

# Ugyanaz a rangsor, mint a napi összefoglalóban.
_SEVERITY_RANK = {"critical": 0, "warning": 1, "insight": 2}
_TOP_ISSUES_LIMIT = 5

# Ezekben az állapotokban lévő kampányokat a detektor amúgy is kihagyná —
# az élő lekérés előtt szűrjük ki őket, hogy a "figyelt kampány" szám valós legyen.
_SKIP_LIFECYCLE = {"paused", "ended"}


async def _batch_pull(platform: str, ext_account_id: str, day: str) -> list[dict[str, Any]]:
    """Egy fiók összes kampányának MAI metrikái EGY API hívással."""
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
    raise ValueError(f"Ismeretlen platform: {platform!r}")


async def generate_instant_account_summary(
    ad_account: dict[str, Any],
    *,
    client_name: str | None = None,
) -> dict[str, Any]:
    """Élő health check egy hirdetési fiókra.

    Paraméterek:
        ad_account  — ad_accounts sor (id, external_account_id, platform, ...)
        client_name — az ügyfél neve a top-problémák címkézéséhez

    Visszatérés (a napi összefoglalóval egyező kulcsok + real-time extrák):
        {
            "total_campaigns":   int,  — figyelt (nem paused/ended) kampányok
            "campaigns_with_data": int,— ezekre volt MA adat az API-ban
            "critical_count":    int,
            "warning_count":     int,
            "alert_count":       int,
            "healthy_campaigns": int,
            "top_issues":        [...],
            "spend_today":       float,— a fiók mai összköltése
            "fetched_at":        str,  — a lekérés időpontja (ISO)
            "day":               str,  — melyik napra kértük (ma)
            "account_label":     str,
            "platform":          str,
            "is_live":           True, — élő adat (nem tárolt)
        }

    Raises:
        Exception — az API hívás hibáját TOVÁBBDOBJA, hogy a parancs
                    egyértelmű hibaüzenetet adhasson (szemben az órás
                    ciklussal, ami csendben átlép a következő fiókra).
    """
    platform = ad_account["platform"]
    ext_account_id = ad_account["external_account_id"]
    db_account_id = ad_account["id"]

    tz = ZoneInfo(get_config().timezone or "UTC")
    now = datetime.now(tz)
    day = date.today().isoformat()

    # 1) A fiók figyelt kampányai (a paused/ended nem számít bele)
    all_campaigns = await asyncio.to_thread(
        campaigns_storage.get_campaigns_by_ad_account, db_account_id
    )
    campaigns = [
        c for c in all_campaigns
        if c.get("is_monitored")
        and (c.get("lifecycle_state") or "new").lower() not in _SKIP_LIFECYCLE
    ]

    # 2) ÉLŐ adatlekérés — 1 batch hívás a fiókra
    insights = await _batch_pull(platform, ext_account_id, day)
    insights_map = {str(i["external_campaign_id"]): i for i in insights}

    log.info(
        "Azonnali összefoglaló: fiók=%s (%s) — %d figyelt kampány, "
        "%d kampányra jött MAI adat",
        ext_account_id, platform, len(campaigns), len(insights_map),
    )

    # 3) Detektor futtatása — ugyanaz, mint az órás ciklusban, de MENTÉS NÉLKÜL
    critical_count = 0
    warning_count = 0
    all_anomalies: list[dict[str, Any]] = []
    campaigns_with_data = 0
    campaigns_with_alerts: set[int] = set()
    spend_today = 0.0

    for campaign in campaigns:
        insight = insights_map.get(str(campaign.get("external_campaign_id")))
        if not insight:
            continue
        campaigns_with_data += 1
        try:
            spend_today += float(insight.get("spend") or 0.0)
        except (TypeError, ValueError):
            pass

        try:
            anomalies = await detect_anomalies_for_campaign(campaign["id"], insight)
        except Exception as exc:  # noqa: BLE001 — egy kampány hibája ne bukjon meg mindent
            log.error(
                "Azonnali összefoglaló: detektor hiba (kampány #%s): %s",
                campaign.get("id"), exc,
            )
            continue

        for a in anomalies:
            severity = (a.get("severity") or "").lower()
            if severity == "critical":
                critical_count += 1
            elif severity == "warning":
                warning_count += 1
            campaigns_with_alerts.add(campaign["id"])
            all_anomalies.append({**a, "_campaign_name": campaign.get("name")})

    # 4) Top problémák — a napi összefoglalóval azonos szerkezetben
    ordered = sorted(
        all_anomalies,
        key=lambda a: _SEVERITY_RANK.get((a.get("severity") or "").lower(), 99),
    )
    account_label = (
        ad_account.get("account_name") or ext_account_id
    )
    top_issues = [
        {
            "client": client_name,
            "campaign": a.get("_campaign_name") or f"#{a.get('campaign_id')}",
            "platform": platform,
            "account_label": account_label,
            "severity": (a.get("severity") or "").lower(),
            "message": a.get("message") or a.get("metric") or "",
        }
        for a in ordered[:_TOP_ISSUES_LIMIT]
    ]

    return {
        "total_campaigns": len(campaigns),
        "campaigns_with_data": campaigns_with_data,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "alert_count": len(all_anomalies),
        "healthy_campaigns": max(0, len(campaigns) - len(campaigns_with_alerts)),
        "top_issues": top_issues,
        "spend_today": round(spend_today, 2),
        "fetched_at": now.isoformat(),
        "day": day,
        "account_label": account_label,
        "platform": platform,
        "is_live": True,
    }


def format_instant_summary(summary: dict[str, Any]) -> str:
    """Az azonnali összefoglaló Discord-szövege.

    A napi összefoglaló formátumát követi (kritikus/warning szám, top
    problémák, figyelt kampányok), de egyértelműen jelzi, hogy ÉLŐ, mai
    adatról van szó, és hogy a nap még nem zárult le.
    """
    total = summary.get("total_campaigns", 0)
    with_data = summary.get("campaigns_with_data", 0)
    crit = summary.get("critical_count", 0)
    warn = summary.get("warning_count", 0)
    healthy = summary.get("healthy_campaigns", 0)
    alert_count = summary.get("alert_count", 0)
    top_issues = summary.get("top_issues") or []
    label = summary.get("account_label", "?")
    platform = (summary.get("platform") or "").upper()
    spend = summary.get("spend_today", 0.0)

    fetched = str(summary.get("fetched_at") or "")[:16].replace("T", " ")
    # Ezres tagolás szóközzel — CSAK a számon (a sor többi vesszőjét nem bántjuk).
    spend_str = f"{spend:,.0f}".replace(",", " ")

    header = f"⚡ **Azonnali összefoglaló** — {label} [{platform}]"
    meta_line = f"🔄 *Élő adat, lekérve: {fetched}* · mai költés: **{spend_str}**"

    if with_data == 0:
        return (
            f"{header}\n{meta_line}\n\n"
            f"ℹ️ Ma még egyik figyelt kampányra sincs adat a platform API-ban "
            f"(**{total}** figyelt kampány).\n"
            f"Ez a nap elején normális — a Meta/Google insights késve frissül."
        )

    if alert_count == 0:
        return (
            f"✅ {header}\n{meta_line}\n\n"
            f"Jelenleg nincs anomália. Mind a **{with_data}** ma adatot adó "
            f"kampány rendben. 🎉\n"
            f"**Kampányok figyelve:** {total}"
        )

    lines = [
        header,
        meta_line,
        "",
        f"🔴 Kritikus: {crit}  |  🟡 Figyelmeztetés: {warn}  |  ✅ Egészséges: {healthy}",
    ]

    if top_issues:
        lines.append("")
        lines.append("**Top problémák MOST:**")
        for issue in top_issues:
            client = issue.get("client") or "?"
            lines.append(
                f"• {client} — {issue.get('campaign', '?')} — {issue.get('message', '')}"
            )

    lines.append("")
    lines.append(
        f"**Kampányok figyelve:** {total} "
        f"(ma adattal: {with_data})  |  **Anomáliák most:** {alert_count}"
    )
    lines.append(
        "─────────────\n"
        "*A nap még nem zárult le — ezek élő, részleges napi adatok. "
        "Riasztás nem került rögzítésre, ez csak pillanatkép.*"
    )
    return "\n".join(lines)
