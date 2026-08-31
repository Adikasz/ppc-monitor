"""
Reggeli értesítések — userenként/naponta PONTOSAN EGY Discord üzenet.

Az ügyfél jelezte, hogy reggelente KÉTSZER kapnak összefoglalót/riasztást. Két
job fut egymás után, átfedő tartalommal:

    08:00  daily_insight_scan   — insightokat generál
    09:00  daily_summary_job    — napi összefoglaló, aminek a problémalistája
                                  (`summary._build_summary_sync`) az `insight`
                                  severity-t IS tartalmazza

Amíg a scan önállóan is `route_alert`-elt, ugyanaz az insight kétszer landolt az
OM csatornáján: egyszer külön riasztásként 08:00-kor, egyszer az összefoglalóban
09:00-kor. A javítás: a scan csak generál és ELMENT, a kiküldés az összefoglalón
keresztül történik.

A tesztek a LEGKÜLSŐ határon számolnak — a `channel.send` hívásokon —, nem egy
belső mockon, így a "hány üzenetet kap valójában az OM" kérdésre válaszolnak.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from src.integrations import discord_router
from src.monitoring import router as alert_router
from src.monitoring import scheduler as sched
from src.monitoring import summary as summary_mod

pytestmark = pytest.mark.asyncio

TZ = ZoneInfo("Europe/Budapest")

# Egy OM, egy saját #alerts csatorna.
_USER = {"id": 1, "discord_user_id": "om1", "display_name": "OM1",
         "alerts_channel_id": "chan-om1", "is_active": True}
_ADMIN_CHANNEL = "chan-admin"

_CAMPAIGN = {
    "id": 100,
    "name": "Teszt kampány",
    "lifecycle_state": "mature",
    "ad_account_id": 5,
    "ad_accounts": {"id": 5, "platform": "meta"},
}


class _FakeChannel:
    """Minimális Discord csatorna, ami feljegyzi a kiküldött üzeneteket."""

    _counter = 0

    def __init__(self, channel_id: str, naplo: list[tuple[str, str]]) -> None:
        self.id = channel_id
        self._naplo = naplo

    async def send(self, content, allowed_mentions=None):  # noqa: ANN001
        self._naplo.append((self.id, content))
        _FakeChannel._counter += 1
        return SimpleNamespace(id=_FakeChannel._counter)


def _insight_row(cid: int) -> list[dict]:
    return [{
        "campaign_id": cid,
        "severity": "insight",
        "metric": "scaling_opportunity",
        "observed_value": None,
        "threshold_value": None,
        "message": "💡 Skálázási lehetőség: emeld a büdzsét",
    }]


def _stored_alert(detected_at: datetime) -> dict:
    """Az insight úgy, ahogy az összefoglaló a DB-ből visszakapja."""
    return {
        "id": 900,
        "campaign_id": _CAMPAIGN["id"],
        "severity": "insight",
        "metric": "scaling_opportunity",
        "message": "💡 Skálázási lehetőség: emeld a büdzsét",
        "detected_at": detected_at.isoformat(),
        "campaigns": {
            "name": _CAMPAIGN["name"],
            "ad_accounts": {
                "id": 5, "platform": "meta", "client_id": 10,
                "account_name": "Fiók", "clients": {"name": "TesztÜgyfél"},
            },
        },
    }


def _patch_morning(stack: contextlib.ExitStack, *, tarolt_alertek: list[dict]) -> list[tuple[str, str]]:
    """A teljes reggeli menet külső függéseit mockolja; visszaadja az üzenet-naplót.

    A `route_alert` SZÁNDÉKOSAN nincs kimockolva: ha a scan mégis hívná, a
    routing végigfutna és VALÓDI `channel.send` hívást produkálna — így a napló
    darabszáma őszintén mutatja meg a regressziót.
    """
    naplo: list[tuple[str, str]] = []

    # --- Discord réteg: minden csatorna feloldható, a küldés naplózódik -----
    async def _resolve(channel_id_raw):  # noqa: ANN001
        return _FakeChannel(str(channel_id_raw), naplo)

    stack.enter_context(mock.patch.object(discord_router, "_resolve_channel", new=_resolve))
    stack.enter_context(mock.patch.object(
        discord_router, "get_config",
        return_value=SimpleNamespace(
            discord_admin_channel_id=_ADMIN_CHANNEL, timezone="Europe/Budapest",
        ),
    ))

    # --- 08:00 insight scan -------------------------------------------------
    stack.enter_context(mock.patch.object(
        sched.campaigns_storage, "get_active_campaigns", return_value=[_CAMPAIGN],
    ))
    stack.enter_context(mock.patch.object(
        sched.insights_history_storage, "get_insights_history",
        side_effect=lambda cid, days: [{"x": 1}] * 5,
    ))
    stack.enter_context(mock.patch.object(
        sched.insights_history_storage, "get_merged_kpis", return_value={},
    ))
    stack.enter_context(mock.patch.object(
        sched.insights_history_storage, "get_latest_roas_map", return_value={},
    ))
    stack.enter_context(mock.patch.object(
        sched, "_resolve_client_for_account", return_value=None,
    ))

    async def _detect(campaign, *_a, **_k):  # noqa: ANN001
        return _insight_row(campaign["id"])

    stack.enter_context(mock.patch.object(sched, "detect_insights_for_campaign", new=_detect))
    stack.enter_context(mock.patch.object(
        sched, "insert_alert",
        side_effect=lambda *a, **k: {"id": 900, "campaign_id": a[0], "severity": a[1]},
    ))
    stack.enter_context(mock.patch.object(sched.alerts_storage, "mark_alert_summarized"))

    # --- routing (csak akkor számít, ha valami mégis hívja) -----------------
    stack.enter_context(mock.patch.object(
        alert_router.campaigns_storage, "get_campaign",
        return_value={"id": 100, "name": "Teszt kampány", "ad_account_id": 5,
                      "lifecycle_state": "mature"},
    ))
    stack.enter_context(mock.patch.object(
        alert_router.mutes_storage, "is_muted", return_value=False,
    ))
    stack.enter_context(mock.patch.object(
        alert_router.ad_accounts_storage, "get_ad_account",
        return_value={"id": 5, "platform": "meta", "client_id": 10},
    ))
    stack.enter_context(mock.patch.object(
        alert_router.clients_storage, "get_client",
        return_value={"id": 10, "name": "TesztÜgyfél"},
    ))
    stack.enter_context(mock.patch.object(
        alert_router, "_resolve_recipients",
        return_value=[{"discord_user_id": "om1", "display_name": "OM1",
                       "role": "primary", "alerts_channel_id": "chan-om1"}],
    ))
    stack.enter_context(mock.patch.object(
        alert_router, "get_config",
        return_value=SimpleNamespace(discord_admin_channel_id=_ADMIN_CHANNEL),
    ))
    stack.enter_context(mock.patch.object(
        alert_router.quiet_hours, "is_quiet_now", return_value=False,
    ))
    stack.enter_context(mock.patch.object(alert_router.alerts_storage, "mark_alert_routed"))
    stack.enter_context(mock.patch.object(alert_router.alerts_storage, "mark_alert_suppressed"))

    # --- 09:00 napi összefoglaló -------------------------------------------
    stack.enter_context(mock.patch.object(
        sched.users_storage, "list_users", return_value=[_USER],
    ))
    stack.enter_context(mock.patch.object(
        summary_mod.assignments_storage, "get_campaign_ids_for_user",
        return_value=[_CAMPAIGN["id"]],
    ))
    stack.enter_context(mock.patch.object(
        summary_mod.alerts_storage, "get_alerts_for_user_in_range",
        return_value=tarolt_alertek,
    ))
    stack.enter_context(mock.patch.object(
        summary_mod, "get_config",
        return_value=SimpleNamespace(timezone="Europe/Budapest"),
    ))
    stack.enter_context(mock.patch.object(
        summary_mod.ad_accounts_storage, "get_ad_accounts_for_client", return_value=[],
    ))
    return naplo


# ---------------------------------------------------------------------------
# A reggeli menet
# ---------------------------------------------------------------------------

async def test_one_morning_produces_exactly_one_message_per_user():
    """08:00 insight scan + 09:00 napi összefoglaló → PONTOSAN 1 üzenet.

    Ez a dupla értesítés regressziós tesztje. Ha a scan újra elkezdene önállóan
    küldeni, itt 2 üzenet lenne — pontosan az, amit az ügyfél panaszolt.
    """
    tegnap = datetime.now(TZ).replace(hour=10, minute=0) - timedelta(days=1)
    with contextlib.ExitStack() as stack:
        naplo = _patch_morning(stack, tarolt_alertek=[_stored_alert(tegnap)])

        await sched.daily_insight_scan()      # 08:00
        await sched.daily_summary_job()       # 09:00

    cimzettek = [csatorna for csatorna, _ in naplo]
    assert cimzettek == ["chan-om1"], f"userenként EGY üzenet, kapott: {naplo}"


async def test_the_insight_scan_alone_sends_nothing():
    """A 08:00-s scan önmagában NULLA Discord üzenetet küld."""
    with contextlib.ExitStack() as stack:
        naplo = _patch_morning(stack, tarolt_alertek=[])
        stats = await sched.daily_insight_scan()

    assert naplo == [], "a scan nem küld önálló üzenetet"
    assert stats["insights"] == 1, "az insight ettől még elkészült és elmentődött"


async def test_the_single_message_still_contains_the_insight():
    """Az egy darab üzenet TARTALMAZZA az insightot — nem néma elhallgatás.

    A duplikáció megszüntetése nem jelenthet adatvesztést: az insight továbbra
    is eljut az OM-hez, csak egyszer, a napi összefoglaló problémalistájában.
    """
    tegnap = datetime.now(TZ).replace(hour=10, minute=0) - timedelta(days=1)
    with contextlib.ExitStack() as stack:
        naplo = _patch_morning(stack, tarolt_alertek=[_stored_alert(tegnap)])

        await sched.daily_insight_scan()
        await sched.daily_summary_job()

    assert len(naplo) == 1
    _, szoveg = naplo[0]
    assert "Napi összefoglaló" in szoveg
    assert "Skálázási lehetőség" in szoveg
    assert "TesztÜgyfél" in szoveg


async def test_the_admin_channel_gets_nothing_in_the_morning():
    """Az admin csatorna NEM kap reggeli összefoglalót/insightot.

    Az insight kizárólag a hozzárendelt OM saját csatornájára mehet — az
    összefoglaló-küldő pedig eleve kihagyja az admin csatornát.
    """
    tegnap = datetime.now(TZ).replace(hour=10, minute=0) - timedelta(days=1)
    with contextlib.ExitStack() as stack:
        naplo = _patch_morning(stack, tarolt_alertek=[_stored_alert(tegnap)])

        await sched.daily_insight_scan()
        await sched.daily_summary_job()

    assert all(csatorna != _ADMIN_CHANNEL for csatorna, _ in naplo)


async def test_monday_morning_produces_exactly_two_messages_and_they_differ():
    """Hétfő reggel: napi (péntek) + hétvégi — 2 KÜLÖN üzenet, eltérő tartalommal.

    Ez az egyetlen nap, amikor egynél több üzenet a HELYES viselkedés, és a
    kettő két külön időszakról szól (nem ugyanaz a tartalom kétszer).
    """
    ablakok: list[tuple[str, str]] = []
    valos_build = summary_mod._build_summary_sync

    def _build(user_id, from_dt, to_dt):
        ablakok.append((from_dt.isoformat(), to_dt.isoformat()))
        return valos_build(user_id, from_dt, to_dt)

    hetfo = datetime(2026, 8, 24, 9, 0, tzinfo=TZ)
    with contextlib.ExitStack() as stack:
        naplo = _patch_morning(stack, tarolt_alertek=[])
        stack.enter_context(mock.patch.object(
            summary_mod, "_build_summary_sync", side_effect=_build,
        ))
        stack.enter_context(mock.patch.object(
            summary_mod, "daily_range", return_value=summary_mod.daily_range(hetfo),
        ))
        stack.enter_context(mock.patch.object(
            summary_mod, "weekend_range", return_value=summary_mod.weekend_range(hetfo),
        ))

        await sched.daily_summary_job()
        await sched.weekly_summary_job()

    assert len(naplo) == 2, "hétfőn két külön üzenet megy ki"
    assert [cs for cs, _ in naplo] == ["chan-om1", "chan-om1"]

    # Két külön időablak — a napi a pénteket, a hétvégi a hétvégét fedi.
    assert len(ablakok) == 2 and ablakok[0] != ablakok[1]
    assert ablakok[0][0].startswith("2026-08-21")   # péntek 00:00
    assert ablakok[0][1].startswith("2026-08-22")   # szombat 00:00

    # A két üzenet szövege is különbözik (nem összevont "előző hét" riport).
    assert "Napi összefoglaló" in naplo[0][1]
    assert "Hétvégi összefoglaló" in naplo[1][1]


# ---------------------------------------------------------------------------
# Második duplikáció-forrás: közös alert-csatorna
#
# Az összefoglaló USERENKÉNT megy ki. Ha két aktív usernek UGYANAZ az
# `alerts_channel_id`-ja, abba az egy csatornába két összefoglaló érkezik —
# az olvasónak ez "dupla értesítés", pedig userenként pontosan egy megy ki.
# A tartalmuk nem azonos (más kampánykészlet), ezért a kód nem dobja el az
# egyiket, csak LÁTHATÓVÁ teszi a helyzetet.
# ---------------------------------------------------------------------------

def test_shared_alert_channel_is_logged_as_a_warning(caplog):
    """Közös csatorna → figyelmeztetés, ami mindkét usert megnevezi."""
    import logging

    logger = logging.getLogger("src.monitoring.scheduler")
    logger.addHandler(caplog.handler)
    try:
        sched._warn_on_shared_channels(
            [
                {"id": 1, "display_name": "Adam", "alerts_channel_id": "közös"},
                {"id": 5, "display_name": "Máté", "alerts_channel_id": "közös"},
                {"id": 6, "display_name": "Nándi", "alerts_channel_id": "saját"},
                {"id": 4, "display_name": "bot", "alerts_channel_id": None},
            ],
            "napi",
        )
    finally:
        logger.removeHandler(caplog.handler)

    sorok = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(sorok) == 1, "csak a TÉNYLEG ütköző csatornára szól figyelmeztetés"
    assert "Adam" in sorok[0] and "Máté" in sorok[0]
    assert "Nándi" not in sorok[0]


def test_distinct_channels_produce_no_warning(caplog):
    """Külön csatornák → nincs zaj a logban."""
    import logging

    logger = logging.getLogger("src.monitoring.scheduler")
    logger.addHandler(caplog.handler)
    try:
        sched._warn_on_shared_channels(
            [
                {"id": 1, "display_name": "Adam", "alerts_channel_id": "a"},
                {"id": 5, "display_name": "Máté", "alerts_channel_id": "b"},
            ],
            "napi",
        )
    finally:
        logger.removeHandler(caplog.handler)

    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
