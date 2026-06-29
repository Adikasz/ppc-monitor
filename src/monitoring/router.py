"""
Alert-router — ki kap értesítést és hol.

A detektor által észlelt és a DB-be írt riasztásokat ez a modul juttatja el a
megfelelő csatornákra, a severity és a hozzárendelések (assignments) alapján.

route_alert(alert) lépései:
    a) Kampány + ügyfél + címzettek (assignments: primary + supporter) lekérése
    a2) Lifecycle-ellenőrzés (paused/ended kampány → skip, a detektorral egyezően;
        csak az /alert test force-override lépi át)
    b) Némítás-ellenőrzés (muted kampány → skip)
    c) Dedup (a már elküldött — status='sent' — alertet nem küldjük újra)
    d) Per-OM kiküldés (CRITICAL + WARNING + INSIGHT egyaránt):
        - Minden assignee a SAJÁT alert csatornájába kapja a riasztást
          (users.alerts_channel_id), kiegészítve a többi értesített kolléga
          megjelölésével ("Értesítve még: …").
        - Ha egy assignee-nek NINCS beállítva csatornája → admin fallback
          (DISCORD_ADMIN_CHANNEL_ID) + figyelmeztetés.
        - Ha a kampánynak NINCS assignee-je → admin fallback + figyelmeztetés.
        - CRITICAL esetén emellett ClickUp task is készül.
       (Email = 10b. lépés, most kimarad.)
    e) Az alert megjelölése elküldöttként (status='sent', sent_at, msg/task id)

Hibatűrés: a csatorna-hibák már a router_integrációkban elnyelődnek (None-t
adnak), így a routing sosem állítja le a schedulert.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from src.config import get_config
from src.integrations import clickup_router, discord_router, email_router
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import alerts as alerts_storage
from src.storage import assignments as assignments_storage
from src.storage import campaigns as campaigns_storage
from src.storage import clients as clients_storage
from src.storage import mutes as mutes_storage
from src.utils import quiet_hours
from src.utils.logging import get_logger

log = get_logger(__name__)

# A leállított kampányokra nem küldünk riasztást (a detektorral egyezően —
# detector._SKIP_LIFECYCLE). Védelem mélységben: a normál folyamatban a detektor
# már kiszűri ezeket, de a routert közvetlenül hívó utak (pl. /alert test) így is
# védve vannak.
_SKIP_LIFECYCLE = {"paused", "ended"}


async def route_alert(
    alert: dict[str, Any],
    *,
    bypass_quiet_hours: bool = False,
    bypass_lifecycle: bool = False,
) -> dict[str, Any]:
    """Egy riasztás kiküldése a megfelelő csatornákra (lásd modul-docstring).

    `bypass_quiet_hours=True` esetén a csendes idő nem nyomja el a nem-kritikus
    riasztást — a manuális `/alert test` parancs használja, hogy a routing
    bármikor tesztelhető legyen.

    `bypass_lifecycle=True` esetén a paused/ended kampány sem nyomja el a
    riasztást — kizárólag az `/alert test … force:true` admin-override használja.
    """
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

    # a2) Lifecycle — a leállított kampányokra nem küldünk (a detektorral egyezően).
    lifecycle = (campaign.get("lifecycle_state") or "new").lower()
    if lifecycle in _SKIP_LIFECYCLE and not bypass_lifecycle:
        log.info(
            "Routing skip — kampány %s állapotban (#%s, alert #%s)",
            lifecycle, campaign_id, alert_id,
        )
        result["reason"] = f"lifecycle_{lifecycle}"
        return result

    # b) Némítás-ellenőrzés
    if await asyncio.to_thread(mutes_storage.is_muted, campaign_id):
        log.info("Routing skip — kampány némítva (#%s, alert #%s)", campaign_id, alert_id)
        result["reason"] = "muted"
        return result

    # b2) Csendes idő — a nem-kritikus riasztást elnyomjuk (CRITICAL átmegy).
    if severity != "critical" and not bypass_quiet_hours and quiet_hours.is_quiet_now():
        log.info(
            "Routing skip — csendes idő, nem-kritikus alert elnyomva (#%s, severity=%s)",
            alert_id, severity,
        )
        if alert_id is not None:
            await asyncio.to_thread(alerts_storage.mark_alert_suppressed, alert_id)
        result["reason"] = "quiet_hours"
        return result

    # Fiók (platform-jelöléshez + ügyfélhez). Egyszer kérjük le, és a CRITICAL
    # ClickUp-ág is ezt használja újra.
    account = await asyncio.to_thread(
        ad_accounts_storage.get_ad_account, campaign.get("ad_account_id")
    )
    platform = account.get("platform") if account else None
    client = await asyncio.to_thread(
        clients_storage.get_client, account.get("client_id")
    ) if account else None
    client_name = client.get("name") if client else "?"
    client_id = client.get("id") if client else None

    # Platform-jelölés a fejlécben: "Ügyfél [META] / Kampány".
    platform_tag = f" [{platform.upper()}]" if platform else ""
    campaign_name = campaign.get("campaign_type") or campaign.get("name") or "?"
    campaign_label = f"{client_name}{platform_tag} / {campaign_name}"

    recipients = await asyncio.to_thread(_resolve_recipients, campaign_id, client_id)
    result["recipients"] = [r["discord_user_id"] for r in recipients]

    admin_channel_id = get_config().discord_admin_channel_id

    # d) Per-OM kiküldés (lásd modul-docstring)
    channels: list[str] = []
    first_message_id: str | None = None

    if recipients:
        for recipient in recipients:
            others = _other_recipient_labels(recipients, recipient)
            personal_channel = recipient.get("alerts_channel_id")

            if personal_channel:
                log.info(
                    "Routing: alert #%s → @%s személyes csatornája (%s)",
                    alert_id, recipient["discord_user_id"], personal_channel,
                )
                res = await discord_router.send_discord_alert(
                    personal_channel, alert,
                    campaign_label=campaign_label,
                    other_recipients=others or None,
                )
            else:
                log.info(
                    "Routing: alert #%s → admin fallback (@%s csatornája nincs beállítva)",
                    alert_id, recipient["discord_user_id"],
                )
                res = await discord_router.send_discord_alert(
                    admin_channel_id, alert,
                    campaign_label=campaign_label,
                    missing_channel_user=recipient["discord_user_id"],
                )

            if res:
                channels.append("discord")
                first_message_id = first_message_id or str(res["message_id"])
    else:
        log.info("Routing: alert #%s → admin fallback (nincs assignee)", alert_id)
        res = await discord_router.send_discord_alert(
            admin_channel_id, alert,
            campaign_label=campaign_label,
            no_assignee=True,
        )
        if res:
            channels.append("discord")
            first_message_id = first_message_id or str(res["message_id"])

    clickup_res = None
    if severity == "critical":
        # A platformot fent már feloldottuk a fiókból (a campaigns táblában nincs
        # platform) — itt újrahasználjuk a ClickUp leíráshoz.
        clickup_res = await clickup_router.create_clickup_task(
            alert, campaign, client, platform=platform,
        )
        if clickup_res:
            channels.append("clickup")

        # Ügyfél-email (CRITICAL) — csak ha van contact_email és még nem ment email
        # erről az alertről (dedup: alerts.email_sent_at, 0007 migration).
        if client and client.get("contact_email") and not alert.get("email_sent_at"):
            email_ok = await email_router.send_client_email(alert, client, campaign)
            if email_ok:
                channels.append("email")
                if alert_id is not None:
                    await asyncio.to_thread(alerts_storage.mark_alert_emailed, alert_id)

    # e) Alert megjelölése elküldöttként (csak ha legalább egy csatorna sikerült).
    # A manuális /alert test fake anomáliának nincs DB id-ja → nem jelölünk.
    dispatched_at = datetime.now(timezone.utc).isoformat()
    if channels:
        if alert_id is not None:
            await asyncio.to_thread(
                alerts_storage.mark_alert_routed,
                alert_id,
                discord_message_id=first_message_id,
                clickup_task_id=str(clickup_res["task_id"]) if (clickup_res and clickup_res.get("task_id")) else None,
                routed_to_discord_user_id=(result["recipients"][0] if result["recipients"] else None),
            )
        result["routed"] = True

    if not channels:
        # Egyetlen csatornára sem ment ki — tipikusan a cél csatorna (személyes
        # vagy admin fallback) nem feloldható: nincs konfigurálva, rossz ID/URL,
        # vagy a bot nem éri el. Ezt jelezzük (a /alert test ezt mutatja).
        result["reason"] = "no_channel"
        log.warning(
            "Routing: alert #%s egyetlen csatornára sem ment ki "
            "(csatorna nem feloldható — config/jogosultság?)",
            alert_id,
        )

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

def _other_recipient_labels(
    recipients: list[dict[str, Any]],
    current: dict[str, Any],
) -> list[str]:
    """A `current`-en kívüli címzettek megjelölései: ["<@id> (role)", …].

    Ez a "Értesítve még: …" sorhoz kell — csak vizuális, nem pingel.
    """
    return [
        f"<@{r['discord_user_id']}> ({r.get('role') or 'primary'})"
        for r in recipients
        if r["discord_user_id"] != current["discord_user_id"]
    ]


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
                "role": row.get("role") or "primary",
                "alerts_channel_id": user.get("alerts_channel_id"),
            })
    return out
