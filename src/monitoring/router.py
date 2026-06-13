"""
Alert-router — ki kap értesítést és hol.

A detektor által észlelt és a DB-be írt riasztásokat ez a modul juttatja el a
megfelelő csatornákra, a severity és a hozzárendelések (assignments) alapján.

route_alert(alert) lépései:
    a) Kampány + ügyfél + címzettek (assignments: primary + supporter) lekérése
    b) Némítás-ellenőrzés (muted kampány → skip)
    c) Dedup (a már elküldött — status='sent' — alertet nem küldjük újra)
    d) Severity szerinti kiküldés:
        - CRITICAL → Discord (kritikus csatorna, mention) + ClickUp task
        - WARNING  → Discord (összefoglaló csatorna, mention nélkül)
        - egyéb    → Discord összefoglaló csatorna (mention nélkül)
       (Email = 10b. lépés, most kimarad.)
    e) Az alert megjelölése elküldöttként (status='sent', sent_at, msg/task id)

Hibatűrés: a csatorna-hibák már a router_integrációkban elnyelődnek (None-t
adnak), így a routing sosem állítja le a schedulert.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from src.integrations import clickup_router, discord_router
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import alerts as alerts_storage
from src.storage import assignments as assignments_storage
from src.storage import campaigns as campaigns_storage
from src.storage import clients as clients_storage
from src.storage import mutes as mutes_storage
from src.utils.logging import get_logger

log = get_logger(__name__)


async def route_alert(alert: dict[str, Any]) -> dict[str, Any]:
    """Egy riasztás kiküldése a megfelelő csatornákra (lásd modul-docstring)."""
    alert_id = alert.get("id")
    campaign_id = alert.get("campaign_id")
    severity = (alert.get("severity") or "warning").lower()

    result: dict[str, Any] = {
        "alert_id": alert_id,
        "routed": False,
        "channels": [],
        "recipients": [],
        "dispatched_at": None,
    }

    # c) Dedup — már elküldött alertet nem küldünk újra
    if alert.get("status") == "sent":
        log.debug("Routing skip — már elküldve (alert #%s)", alert_id)
        result["reason"] = "already_sent"
        return result

    # a) Kampány
    campaign = await asyncio.to_thread(campaigns_storage.get_campaign, campaign_id)
    if campaign is None:
        log.warning("Routing: nincs ilyen kampány #%s (alert #%s)", campaign_id, alert_id)
        result["reason"] = "no_campaign"
        return result

    # b) Némítás-ellenőrzés
    if await asyncio.to_thread(mutes_storage.is_muted, campaign_id):
        log.info("Routing skip — kampány némítva (#%s, alert #%s)", campaign_id, alert_id)
        result["reason"] = "muted"
        return result

    # Ügyfél (címkéhez) + címzettek
    client = await asyncio.to_thread(_resolve_client, campaign)
    client_name = client.get("name") if client else "?"
    client_id = client.get("id") if client else None
    campaign_label = f"{client_name} / {campaign.get('campaign_type') or campaign.get('name') or '?'}"

    recipients = await asyncio.to_thread(_resolve_recipients, campaign_id, client_id)
    result["recipients"] = [r["discord_user_id"] for r in recipients]

    # d) Severity szerinti kiküldés
    channels: list[str] = []

    discord_res = await discord_router.send_discord_alert(
        alert, recipients, campaign_label=campaign_label,
    )
    if discord_res:
        channels.append("discord")

    clickup_res = None
    if severity == "critical":
        clickup_res = await clickup_router.create_clickup_task(alert, campaign)
        if clickup_res:
            channels.append("clickup")
        # Email az ügyfélnek → 10b. lépés (clients.contact_email = %r)
        # itt majd: if client and client.get("contact_email"): ...

    # e) Alert megjelölése elküldöttként (csak ha legalább egy csatorna sikerült)
    dispatched_at = datetime.now(timezone.utc).isoformat()
    if channels:
        await asyncio.to_thread(
            alerts_storage.mark_alert_routed,
            alert_id,
            discord_message_id=str(discord_res["message_id"]) if discord_res else None,
            clickup_task_id=str(clickup_res["task_id"]) if (clickup_res and clickup_res.get("task_id")) else None,
            routed_to_discord_user_id=(result["recipients"][0] if result["recipients"] else None),
        )
        result["routed"] = True

    result["channels"] = channels
    result["dispatched_at"] = dispatched_at

    log.info(
        "Routing kész: alert #%s (severity=%s) → channels=%s, recipients=%d",
        alert_id, severity, channels, len(recipients),
    )
    return result


# ---------------------------------------------------------------------------
# Belső segédfüggvények (szinkron — to_thread-ből hívva)
# ---------------------------------------------------------------------------

def _resolve_client(campaign: dict[str, Any]) -> dict[str, Any] | None:
    """Kampány → ad_account → ügyfél."""
    account = ad_accounts_storage.get_ad_account(campaign.get("ad_account_id"))
    if not account:
        return None
    return clients_storage.get_client(account.get("client_id"))


def _resolve_recipients(campaign_id: int, client_id: int | None) -> list[dict[str, Any]]:
    """Címzettek: kampány-szintű + ügyfél-szintű hozzárendelések (duplikátum-mentes).

    A riasztás CSAK a hozzárendelt személy(ek)hez megy (követelmény).
    """
    rows = assignments_storage.get_assignments_for_campaign(campaign_id)
    if client_id is not None:
        rows = rows + assignments_storage.get_assignments_for_client(client_id)

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        user = row.get("users")
        if not user:
            continue
        discord_user_id = user.get("discord_user_id")
        if discord_user_id and discord_user_id not in seen:
            seen.add(discord_user_id)
            out.append({
                "discord_user_id": discord_user_id,
                "display_name": user.get("display_name"),
                "role": row.get("role"),
            })
    return out
