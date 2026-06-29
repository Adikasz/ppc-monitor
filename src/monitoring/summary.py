"""
Napi és heti összefoglaló-generátor (13. lépés).

A scheduler (reggel 9-kor) és a manuális `/summary` parancs is ezt hívja.
Felhasználónként összesíti a hozzá rendelt kampányokra érkezett riasztásokat
egy időablakban, és visszaad egy struktúrát, amit a discord_router formáz meg.

Időablakok (Europe/Budapest, config.timezone):
    - daily : tegnap 00:00 → ma 00:00
    - weekly: a legutóbbi hétvége — péntek 22:00 → hétfő 08:00

A riasztás időbélyege `detected_at` (lásd alerts séma — NEM `created_at`).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.config import get_config
from src.storage import alerts as alerts_storage
from src.storage import assignments as assignments_storage
from src.utils.logging import get_logger

log = get_logger(__name__)

# Severity prioritás a "top problémák" rendezéséhez (kisebb = előrébb).
_SEVERITY_RANK = {"critical": 0, "warning": 1, "insight": 2}
_TOP_ISSUES_LIMIT = 5


def _tz() -> ZoneInfo:
    return ZoneInfo(get_config().timezone or "UTC")


def daily_range(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Tegnap 00:00 → ma 00:00 a konfigurált időzónában."""
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


def _build_summary_sync(user_id: int, from_dt: datetime, to_dt: datetime) -> dict[str, Any]:
    """Szinkron összesítés (to_thread-ből hívva)."""
    campaign_ids = assignments_storage.get_campaign_ids_for_user(user_id)
    total_campaigns = len(campaign_ids)

    alerts = alerts_storage.get_alerts_for_user_in_range(user_id, from_dt, to_dt)

    critical_count = sum(1 for a in alerts if (a.get("severity") or "").lower() == "critical")
    warning_count = sum(1 for a in alerts if (a.get("severity") or "").lower() == "warning")

    # Top problémák: severity-prioritás szerint. A lista a DB-ből már
    # detected_at DESC-ben jön, és a sorted() stabil → azonos severity-n belül
    # megmarad a "legfrissebb előre" sorrend.
    ordered = sorted(
        alerts,
        key=lambda a: _SEVERITY_RANK.get((a.get("severity") or "").lower(), 99),
    )
    top_issues = [
        {
            "campaign": (a.get("campaigns") or {}).get("name") or f"#{a.get('campaign_id')}",
            "platform": ((a.get("campaigns") or {}).get("ad_accounts") or {}).get("platform"),
            "severity": (a.get("severity") or "").lower(),
            "message": a.get("message") or a.get("metric") or "",
        }
        for a in ordered[:_TOP_ISSUES_LIMIT]
    ]

    campaigns_with_alerts = {a.get("campaign_id") for a in alerts if a.get("campaign_id") is not None}
    healthy_campaigns = max(0, total_campaigns - len(campaigns_with_alerts & set(campaign_ids)))

    return {
        "total_campaigns": total_campaigns,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "alert_count": len(alerts),
        "top_issues": top_issues,
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
