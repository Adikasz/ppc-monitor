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
    c2) CRITICAL esetén ClickUp task — MÉG A DISCORD ÜZENET ELŐTT, hogy a task
        linkje beleférjen az üzenetbe (lásd KÉTIRÁNYÚ LINKELÉS lentebb)
    d) Per-OM kiküldés (CRITICAL + WARNING + INSIGHT egyaránt):
        - Minden assignee a SAJÁT alert csatornájába kapja a riasztást
          (users.alerts_channel_id), kiegészítve a többi értesített kolléga
          megjelölésével ("Értesítve még: …").
        - Ha egy assignee-nek NINCS beállítva csatornája → admin fallback
          (DISCORD_ADMIN_CHANNEL_ID) + figyelmeztetés.
        - Ha a kampánynak NINCS assignee-je → admin fallback + figyelmeztetés.
        - KIVÉTEL: INSIGHT severity-re NINCS admin fallback (lásd
          `_NO_ADMIN_FALLBACK_SEVERITY`) — az insight kizárólag a hozzárendelt
          OM saját csatornájára mehet, sehova máshova.
       (Email = 10b. lépés, most kimarad.)
    d2) A kiküldött üzenet ugrólinkjének visszaírása a ClickUp taskra
    e) Az alert megjelölése elküldöttként (status='sent', sent_at, msg/task id)

KÉTIRÁNYÚ LINKELÉS (ClickUp ↔ Discord) — a sorrend nem cserélhető fel:
    1. task létrejön     → megvan a task URL
    2. Discord üzenet    → benne "📋 ClickUp: {task_url}"
    3. task frissítése    → benne "🔗 Discord: {üzenet ugrólinkje}"
    A 2. lépéshez kell az 1. eredménye, a 3.-hoz a 2.-é — ezért készül a task
    a küldés ELŐTT, és ezért csak utólag kerül rá a Discord link.

    HIBA-IZOLÁCIÓ mindkét irányban:
      - Ha az 1. lépés bármiért elhasal (nincs mapping, nincs token, API hiba),
        a Discord üzenet AKKOR IS KIMEGY, csak task-link nélkül.
      - Ha a 2. lépés hasal el, a task megmarad Discord-link nélkül (warning).
      - Egyik hiba sem állítja meg a riasztási folyamatot.

    Kinek a listájába kerül a task: a MÁR FELOLDOTT címzettek közül az első
    `primary` szerepűébe (`_primary_recipient`) — a routing itt nem old fel
    semmit újra, ugyanazt a hozzárendelést használja, mint a Discord-ág.

NINCS "megoldódott" / feloldó értesítés — SZÁNDÉKOSAN:
    A rendszer CSAK akkor küld üzenetet, ha VAN probléma. Ha egy korábban
    riasztott anomália elmúlik (a detektor már nem adja vissza), semmilyen
    follow-up nem megy ki, és a korábbi riasztást sem "vonjuk vissza". Ez
    ügyfél-döntés: a feloldó üzenetek megdupláznák a csatorna-forgalmat, és a
    valódi problémák beleolvadnának a zajba. Ha ilyen igény felmerül, az
    ÚJ funkció — ne "javításként" kerüljön be.
    (Kapcsolódó korlát: `storage.alerts.insert_alert` docstringje — a napi
    dedup-sor a nap végéig a legutolsó rossz értéken marad.)

Hibatűrés: a csatorna-hibák már a router_integrációkban elnyelődnek (None-t
adnak), így a routing sosem állítja le a schedulert.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from src.config import get_config
from src.integrations import alert_content, clickup_router, discord_router, email_router
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import alerts as alerts_storage
from src.storage import assignments as assignments_storage
from src.storage import campaigns as campaigns_storage
from src.storage import clickup_mapping as clickup_storage
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

# Ezekre a severity-kre NINCS admin fallback: ha nincs assignee, vagy az
# assignee-nek nincs beállítva személyes csatornája, a riasztás egyszerűen
# kimarad — NEM landol az admin csatornán.
#
# Miért csak az insightra: az insight JAVASLAT az adott kampányt kezelő OM-nek,
# nem üzemzavar-jelzés. Az admin csatorna (és bárki más csatornája) fogalmilag
# rossz cím neki — az ügyfél explicit elvárása, hogy insight KIZÁRÓLAG a
# hozzárendelt OM saját #alerts csatornájára mehessen. A CRITICAL/WARNING
# ezzel szemben tovább használja a fallbacket: egy hozzárendelés nélküli
# kampány kritikus hibája nem veszhet el némán.
#
# (A napi menetben ez ma védelem mélységben van: a napi insight scan nem is
# hívja a routert — az insightok az összefoglalóban mennek ki, ami eleve csak
# a user saját csatornájára megy. Ez a kapu a közvetlen router-hívásokat —
# pl. /alert test — és a jövőbeli útvonalakat védi.)
_NO_ADMIN_FALLBACK_SEVERITY = {"insight"}


async def route_alert(
    alert: dict[str, Any],
    *,
    bypass_quiet_hours: bool = False,
    bypass_lifecycle: bool = False,
) -> dict[str, Any]:
    """Egy riasztás kiküldése a megfelelő csatornákra (lásd modul-docstring).

    `bypass_quiet_hours=True` esetén a csendes idő egyáltalán nem nyomja el a
    riasztást — a manuális `/alert test` parancs használja, hogy a routing
    bármikor tesztelhető legyen. Enélkül csendes időben SEMMI nem megy ki (a
    CRITICAL sem) — az OM-ek reggel 08:00-kor a napi összefoglalóból értesülnek.

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

    # b2) Csendes idő — semmi nem megy ki (17:00–08:00, hétvége). Nincs kivétel:
    # a CRITICAL (akár budget_depleted) is reggel 08:00-ig vár, az OM-ek akkor a
    # napi összefoglalóból látják, mi történt éjjel. A `bypass_quiet_hours=True`
    # (pl. /alert test) továbbra is teljesen felülírja a csendes időt.
    if not bypass_quiet_hours and quiet_hours.is_quiet_now():
        log.info(
            "Routing skip — csendes idő, alert elnyomva (#%s, severity=%s)",
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

    # Platform-jelölés a fejlécben: "Ügyfél [META] / Kampány". A címkét a közös
    # formázó adja, hogy a ClickUp task ugyanezt az ügyfél/platform megnevezést
    # lássa (lásd integrations/alert_content.py).
    campaign_name = campaign.get("campaign_type") or campaign.get("name") or "?"
    campaign_label = alert_content.campaign_label(client_name, platform, campaign_name)

    recipients = await asyncio.to_thread(_resolve_recipients, campaign_id, client_id)
    result["recipients"] = [r["discord_user_id"] for r in recipients]

    admin_channel_id = get_config().discord_admin_channel_id

    # c2) ClickUp task — a Discord üzenet ELŐTT, hogy a linkje beleférjen.
    # Bármilyen hiba esetén None: a riasztás task nélkül is kimegy.
    clickup_res = await _create_clickup_task(
        alert, campaign, client, platform=platform, recipients=recipients,
    )
    clickup_task_url = clickup_res.get("url") if clickup_res else None

    # d) Per-OM kiküldés (lásd modul-docstring)
    channels: list[str] = []
    first_send: dict[str, Any] | None = None

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
                    clickup_task_url=clickup_task_url,
                )
            elif severity in _NO_ADMIN_FALLBACK_SEVERITY:
                log.info(
                    "Routing: insight #%s kihagyva — @%s csatornája nincs "
                    "beállítva, insight pedig nem megy admin csatornára",
                    alert_id, recipient["discord_user_id"],
                )
                continue
            else:
                log.info(
                    "Routing: alert #%s → admin fallback (@%s csatornája nincs beállítva)",
                    alert_id, recipient["discord_user_id"],
                )
                res = await discord_router.send_discord_alert(
                    admin_channel_id, alert,
                    campaign_label=campaign_label,
                    missing_channel_user=recipient["discord_user_id"],
                    clickup_task_url=clickup_task_url,
                )

            if res:
                channels.append("discord")
                first_send = first_send or res
    elif severity in _NO_ADMIN_FALLBACK_SEVERITY:
        log.info(
            "Routing: insight #%s kihagyva — nincs assignee, insight pedig "
            "nem megy admin csatornára",
            alert_id,
        )
        result["reason"] = "insight_no_assignee"
    else:
        log.info("Routing: alert #%s → admin fallback (nincs assignee)", alert_id)
        res = await discord_router.send_discord_alert(
            admin_channel_id, alert,
            campaign_label=campaign_label,
            no_assignee=True,
            clickup_task_url=clickup_task_url,
        )
        if res:
            channels.append("discord")
            first_send = first_send or res

    if clickup_res:
        channels.append("clickup")
        # d2) A kiküldött üzenet ugrólinkje vissza a taskra. Ha nem ment ki
        # üzenet (vagy hiányzik egy azonosító), a task link nélkül marad —
        # warning, de nem hiba.
        await _append_discord_link(clickup_res, first_send, alert_id=alert_id)

    first_message_id = str(first_send["message_id"]) if first_send else None

    if severity == "critical":
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

    if not channels and not result.get("reason"):
        # Egyetlen csatornára sem ment ki — tipikusan a cél csatorna (személyes
        # vagy admin fallback) nem feloldható: nincs konfigurálva, rossz ID/URL,
        # vagy a bot nem éri el. Ezt jelezzük (a /alert test ezt mutatja).
        #
        # A `reason` ellenőrzése azért kell, mert az insight szándékos
        # kihagyása (nincs assignee / nincs személyes csatorna) már beállított
        # egy pontosabb okot — azt nem szabad "no_channel"-lel felülírni,
        # különben egy TERVEZETT viselkedés config-hibának látszana.
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
# ClickUp — task létrehozás és visszalinkelés (lásd modul-docstring)
# ---------------------------------------------------------------------------

def _primary_recipient(recipients: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A ClickUp task gazdája a MÁR FELOLDOTT címzettek közül. None ha nincs.

    Az első `primary` szerepű címzett; ha csak `supporter` van, az első bármelyik
    — egy helyettesre kiosztott task is jobb, mint egy sem.

    Itt SEMMILYEN új feloldás nem történik: a `route_alert` már lefuttatta a
    `_resolve_recipients`-et (assignments: kampány- + ügyfél-szint), ez a
    függvény csak választ a kész listából. Így a ClickUp task és a Discord
    üzenet nem tudhat két különböző felelőst gondolni ugyanarról a kampányról.
    """
    for recipient in recipients:
        if (recipient.get("role") or "primary") == "primary":
            return recipient
    return recipients[0] if recipients else None


async def _create_clickup_task(
    alert: dict[str, Any],
    campaign: dict[str, Any],
    client: dict[str, Any] | None,
    *,
    platform: str | None,
    recipients: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """ClickUp task a riasztáshoz — vagy None, ha bármiért kimarad.

    Graceful skip (warning + None, a Discord riasztás megy tovább):
      - a severity nem szerepel a `clickup_router.TASK_SEVERITIES`-ben (alap: csak CRITICAL)
      - nincs feloldott címzett (a taskot nem tudnánk kire szignálni / hova tenni)
      - az OM-nek még nincs `clickup_manager_mapping` sora (`/clickup setup-manager`)
      - hiányzó CLICKUP_API_TOKEN vagy ClickUp API hiba (a task-modul nyeli el)

    A `try/except` a legkülső védőháló: a riasztás akkor sem eshet ki, ha ezen
    az ágon valami VÁRATLAN történik (pl. a DB-réteg mégis dob).
    """
    severity = (alert.get("severity") or "").lower()
    if not clickup_router.is_task_severity(severity):
        return None

    alert_id = alert.get("id")
    try:
        primary = _primary_recipient(recipients)
        if primary is None:
            log.warning(
                "ClickUp task kihagyva (alert #%s) — a kampánynak nincs "
                "hozzárendelt OM-je, így nincs cél-lista. A riasztás Discordon "
                "kimegy (admin fallback).",
                alert_id,
            )
            return None

        user_id = primary.get("user_id")
        if user_id is None:
            log.warning(
                "ClickUp task kihagyva (alert #%s) — @%s címzetthez nem tartozik "
                "users sor id. A riasztás Discordon kimegy.",
                alert_id, primary.get("discord_user_id"),
            )
            return None

        mapping = await asyncio.to_thread(clickup_storage.get_mapping_for_user, user_id)
        if not mapping:
            log.warning(
                "ClickUp task kihagyva (alert #%s) — @%s (user #%s) OM-nek még "
                "nincs ClickUp mappingja. Létrehozás: `/clickup setup-manager "
                "user:@%s clickup_user_id:… folder_id:… list_id:…`. "
                "A riasztás Discord-only routinggal megy.",
                alert_id, primary.get("display_name") or primary.get("discord_user_id"),
                user_id, primary.get("discord_user_id"),
            )
            return None

        return await clickup_router.create_clickup_task(
            alert, campaign, client, platform=platform, mapping=mapping,
        )
    except Exception as exc:  # noqa: BLE001 — a ClickUp-ág SOHA nem blokkolhat
        log.error(
            "ClickUp task létrehozása váratlan hibával elszállt (alert #%s): %s "
            "— a riasztás Discordon task nélkül megy ki.",
            alert_id, exc,
        )
        return None


async def _append_discord_link(
    clickup_res: dict[str, Any],
    send_res: dict[str, Any] | None,
    *,
    alert_id: Any,
) -> None:
    """A kiküldött Discord üzenet ugrólinkjének visszaírása a ClickUp taskra.

    Az ELSŐ SIKERES küldés üzenetére linkelünk. Több címzettnél ez a címzett-
    lista első olyan tagjának üzenete, akinek a küldés ténylegesen sikerült —
    nem feltétlenül a `primary` OM-é, akinek a listájába a task került. Ez
    szándékos egyszerűsítés: MINDEN címzett UGYANAZT a riasztás-szöveget kapja,
    így bármelyik példány jó horgony a taskról visszafelé.

    Sosem dob és sosem blokkol: ha nem ment ki üzenet, vagy hiányzik valamelyik
    azonosító, a task egyszerűen Discord-link nélkül marad (warning). Ez a
    kevésbé súlyos hiba-irány: a riasztás ilyenkor MÁR kiment, a task pedig
    létezik — csak a kereszthivatkozás hiányzik.
    """
    task_id = clickup_res.get("task_id")
    if not task_id:
        return

    try:
        if not send_res:
            log.warning(
                "ClickUp task #%s Discord-link nélkül marad (alert #%s) — "
                "egyetlen Discord üzenet sem ment ki sikeresen.",
                task_id, alert_id,
            )
            return

        # A guild ID-t elsődlegesen a küldés válaszából vesszük (az a TÉNYLEGES
        # szerver); ha hiányzik (pl. DM), a konfigurált guild a tartalék.
        guild_id = send_res.get("guild_id") or get_config().discord_guild_id
        url = alert_content.discord_jump_url(
            guild_id, send_res.get("channel_id"), send_res.get("message_id"),
        )
        if url is None:
            log.warning(
                "ClickUp task #%s Discord-link nélkül marad (alert #%s) — "
                "hiányzó guild/csatorna/üzenet azonosító (guild=%r).",
                task_id, alert_id, guild_id,
            )
            return

        await clickup_router.append_discord_link(
            str(task_id), clickup_res.get("description") or "", url,
        )
    except Exception as exc:  # noqa: BLE001 — a visszalinkelés sosem blokkolhat
        log.warning(
            "ClickUp task #%s Discord-linkjének visszaírása elszállt (alert #%s): %s",
            task_id, alert_id, exc,
        )


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

    A `user_id` (a MI users táblánk id-ja) is benne van a sorokban: a ClickUp-ág
    ezzel keresi ki az OM `clickup_manager_mapping` sorát. Ez szándékosan itt,
    a MEGLÉVŐ feloldásban keletkezik — nem külön lekérdezésből —, hogy a task és
    a Discord üzenet garantáltan ugyanarra a felelősre hivatkozzon.
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
                "user_id": user.get("id"),
                "discord_user_id": discord_user_id,
                "display_name": user.get("display_name"),
                "role": row.get("role") or "primary",
                "alerts_channel_id": user.get("alerts_channel_id"),
            })
    return out
