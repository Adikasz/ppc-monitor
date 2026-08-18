"""
`/insight scan-now` — a napi insight scan manuális futtatása + megfigyelhetőség.

Két elvárás áll a tesztek mögött:

1. A parancs UGYANAZT a függvényt hívja, mint a 08:00-s cron job — nem egy
   párhuzamos másolatot. Különben a teszt sikere semmit nem mondana az éles
   futásról (pont ez a hiba szült egy hónapnyi néma insight-hiányt).

2. A scan MEGSZÁMOLJA, mi történt. Korábban a "< 3 history sor" skip némán
   ugrott és a hibaág traceback nélkül logolt, így egy elhasalt scan
   megkülönböztethetetlen volt egy jogosan üres scantől ("0 insight").
"""
from __future__ import annotations

import logging
from unittest import mock

import pytest

from src.bot.commands import insights as insight_cmd
from src.monitoring import scheduler as sched


# ---------------------------------------------------------------------------
# Segédek
# ---------------------------------------------------------------------------

def _campaign(cid: int, *, lifecycle="mature", account=1):
    return {
        "id": cid,
        "name": f"K{cid}",
        "lifecycle_state": lifecycle,
        "ad_account_id": account,
        "ad_accounts": {"id": account, "platform": "meta"},
    }


def _patch_scan(stack, *, campaigns, history_rows=5, insights=None, boom_on=()):
    """A scan összes külső függését kigúnyolja; csak a saját logikája marad."""
    stack.enter_context(mock.patch.object(
        sched.campaigns_storage, "get_active_campaigns", return_value=campaigns,
    ))
    stack.enter_context(mock.patch.object(
        sched.insights_history_storage, "get_insights_history",
        side_effect=lambda cid, days: [{"x": 1}] * (
            history_rows(cid) if callable(history_rows) else history_rows
        ),
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

    async def _detect(campaign, *_a, **_k):
        cid = campaign.get("id")
        if cid in boom_on:
            raise RuntimeError(f"szándékos hiba #{cid}")
        return list(insights(cid) if callable(insights) else (insights or []))

    stack.enter_context(mock.patch.object(sched, "detect_insights_for_campaign", new=_detect))
    stack.enter_context(mock.patch.object(
        sched, "insert_alert", side_effect=lambda *a, **k: {"id": 1, "campaign_id": a[0]},
    ))
    routed = mock.AsyncMock(return_value={"routed": True})
    stack.enter_context(mock.patch.object(sched, "route_alert", new=routed))
    return routed


def _insight(cid):
    return [{
        "campaign_id": cid, "severity": "insight", "metric": "scaling_opportunity",
        "observed_value": None, "threshold_value": None, "message": "üzenet",
    }]


# ---------------------------------------------------------------------------
# A scan számlálói
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_returns_counters_instead_of_none():
    """A scan mérhető eredményt ad — enélkül a parancs nem tudna mit jelenteni."""
    import contextlib
    with contextlib.ExitStack() as stack:
        _patch_scan(stack, campaigns=[_campaign(1), _campaign(2)], insights=_insight)
        stats = await sched.daily_insight_scan()

    assert stats["total"] == 2
    assert stats["insights"] == 2
    assert stats["skipped_no_history"] == 0
    assert stats["failed"] == 0


@pytest.mark.asyncio
async def test_scan_counts_campaigns_skipped_for_missing_history():
    """A "kevés adat" skip KORÁBBAN NÉMA volt — most számláló van rá.

    Ez a kulcs-megkülönböztetés: a "0 insight, mert nincs adat" és a
    "0 insight, mert elhasalt" két nagyon különböző hiba.
    """
    import contextlib
    with contextlib.ExitStack() as stack:
        _patch_scan(
            stack,
            campaigns=[_campaign(1), _campaign(2), _campaign(3)],
            # #2-nek csak 1 mérése van → a 3-as minimum alatt
            history_rows=lambda cid: 1 if cid == 2 else 5,
            insights=_insight,
        )
        stats = await sched.daily_insight_scan()

    assert stats["total"] == 3
    assert stats["skipped_no_history"] == 1
    assert stats["insights"] == 2


@pytest.mark.asyncio
async def test_scan_counts_failed_campaigns_and_keeps_going():
    """Egy kampány hibája nem állítja meg a scant, de MEGSZÁMOLÓDIK."""
    import contextlib
    with contextlib.ExitStack() as stack:
        _patch_scan(
            stack,
            campaigns=[_campaign(1), _campaign(2), _campaign(3)],
            insights=_insight,
            boom_on={2},
        )
        stats = await sched.daily_insight_scan()

    assert stats["failed"] == 1
    assert stats["insights"] == 2, "a hiba után is folytatódnia kell"


@pytest.mark.asyncio
async def test_scan_counts_quiet_hours_suppression_separately():
    """A csendes idő miatt elnyomott insight NEM "sikeres kiküldés".

    Ez teszi láthatóvá a most felfedezett latens csapdát: rossz
    QUIET_HOURS_END mellett a 08:00-s scan minden insightja elnyomódik.
    """
    import contextlib
    with contextlib.ExitStack() as stack:
        routed = _patch_scan(stack, campaigns=[_campaign(1)], insights=_insight)
        routed.return_value = {"routed": False, "reason": "quiet_hours"}
        stats = await sched.daily_insight_scan()

    assert stats["insights"] == 1
    assert stats["routed"] == 0
    assert stats["quiet_hours"] == 1


@pytest.mark.asyncio
async def test_scan_logs_the_full_breakdown_at_the_end(caplog):
    """A záró log sor mind a négy számot tartalmazza — ezt olvassa a Railway log."""
    import contextlib
    with contextlib.ExitStack() as stack:
        _patch_scan(
            stack,
            campaigns=[_campaign(1), _campaign(2)],
            history_rows=lambda cid: 1 if cid == 2 else 5,
            insights=_insight,
        )
        # A modul-loggerek `propagate = False`-szal jönnek létre
        # (src/utils/logging.py), ezért a caplog gyökér-handlere NEM látná őket.
        # Ugyanez az oka annak, hogy a Railway logban is csak az alkalmazás saját
        # sorai látszanak — ezért kell ez az összesítő sor egyáltalán.
        logger = logging.getLogger("src.monitoring.scheduler")
        logger.addHandler(caplog.handler)
        try:
            await sched.daily_insight_scan()
        finally:
            logger.removeHandler(caplog.handler)

    zaro = [r.getMessage() for r in caplog.records if "Insight scan kész" in r.getMessage()]
    assert zaro, "nincs záró összesítő log sor"
    sor = zaro[-1]
    assert "1 insight generálva" in sor
    assert "1 kampány kihagyva (kevés adat)" in sor
    assert "0 kampány hibázott" in sor
    assert "2 kampány vizsgálva összesen" in sor


@pytest.mark.asyncio
async def test_scan_returns_full_stats_even_when_there_is_nothing_to_do():
    """A korai kilépési ág is teljes kulcskészletet ad — a hívó ne .get()-eljen."""
    import contextlib
    with contextlib.ExitStack() as stack:
        _patch_scan(stack, campaigns=[_campaign(1, lifecycle="new")])
        stats = await sched.daily_insight_scan()

    assert stats["total"] == 0
    assert set(stats) == {
        "total", "insights", "skipped_no_history", "failed", "routed", "quiet_hours",
    }


# ---------------------------------------------------------------------------
# Ügyfél-szűkítés
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_filter_narrows_to_that_clients_accounts():
    """A kampánysorban nincs client_id — a szűrés a kliens FIÓKJAIN át megy."""
    import contextlib
    vizsgalt: list[int] = []

    async def _detect(campaign, *_a, **_k):
        vizsgalt.append(campaign["id"])
        return []

    with contextlib.ExitStack() as stack:
        _patch_scan(
            stack,
            campaigns=[_campaign(1, account=10), _campaign(2, account=20)],
        )
        stack.enter_context(mock.patch.object(sched, "detect_insights_for_campaign", new=_detect))
        stack.enter_context(mock.patch.object(
            sched.ad_accounts_storage, "get_ad_accounts_for_client",
            return_value=[{"id": 10}],
        ))
        stats = await sched.daily_insight_scan(client_id=99)

    assert vizsgalt == [1], "csak a kliens saját fiókjának kampánya futhat"
    assert stats["total"] == 1


@pytest.mark.asyncio
async def test_limit_caps_the_number_of_campaigns():
    import contextlib
    with contextlib.ExitStack() as stack:
        _patch_scan(stack, campaigns=[_campaign(i) for i in range(1, 6)], insights=_insight)
        stats = await sched.daily_insight_scan(limit=2)

    assert stats["total"] == 2


# ---------------------------------------------------------------------------
# A parancs
# ---------------------------------------------------------------------------

class _Interaction:
    def __init__(self, channel_id=1, user_id=42):
        self.channel_id = channel_id
        self.user = mock.Mock(id=user_id)
        self.response = mock.AsyncMock()
        self.followup = mock.AsyncMock()
        self.channel = mock.AsyncMock()

    def sent(self) -> list[str]:
        return [c.args[0] for c in self.followup.send.call_args_list if c.args]


@pytest.mark.asyncio
async def test_command_calls_the_same_function_as_the_cron_job():
    """A parancs a scheduler saját `daily_insight_scan`-jét hívja.

    Ha itt egy másolat futna, a "működik a teszt" semmit nem mondana arról,
    hogy reggel 08:00-kor is működik-e.
    """
    cog = insight_cmd.InsightsCog(mock.Mock())
    interaction = _Interaction()
    hivas = {}

    async def _fake_scan(*, client_id=None, limit=None):
        hivas["args"] = (client_id, limit)
        return {"total": 3, "insights": 2, "skipped_no_history": 1,
                "failed": 0, "routed": 2, "quiet_hours": 0}

    with mock.patch.object(insight_cmd, "_is_admin_channel", return_value=True), \
         mock.patch.object(insight_cmd.scheduler_mod, "daily_insight_scan", new=_fake_scan), \
         mock.patch.object(insight_cmd.audit, "log_action"):
        await insight_cmd.InsightsCog.scan_now.callback(cog, interaction)

    assert hivas["args"] == (None, None)
    # A parancs ugyanarra az objektumra hivatkozik, amit a scheduler regisztrál.
    assert insight_cmd.scheduler_mod is sched


@pytest.mark.asyncio
async def test_command_with_client_scopes_the_scan():
    cog = insight_cmd.InsightsCog(mock.Mock())
    interaction = _Interaction()
    hivas = {}

    async def _fake_scan(*, client_id=None, limit=None):
        hivas["client_id"] = client_id
        return {"total": 1, "insights": 1, "skipped_no_history": 0,
                "failed": 0, "routed": 1, "quiet_hours": 0}

    with mock.patch.object(insight_cmd, "_is_admin_channel", return_value=True), \
         mock.patch.object(insight_cmd, "_resolve_client",
                           return_value={"id": 7, "name": "Stopvill"}), \
         mock.patch.object(insight_cmd.scheduler_mod, "daily_insight_scan", new=_fake_scan), \
         mock.patch.object(insight_cmd.audit, "log_action"):
        await insight_cmd.InsightsCog.scan_now.callback(cog, interaction, client="Stopvill")

    assert hivas["client_id"] == 7
    assert any("Stopvill" in m for m in interaction.sent())


@pytest.mark.asyncio
async def test_command_is_rejected_outside_the_admin_channel():
    cog = insight_cmd.InsightsCog(mock.Mock())
    interaction = _Interaction()
    futott = False

    async def _fake_scan(**_k):
        nonlocal futott
        futott = True
        return {}

    with mock.patch.object(insight_cmd, "_is_admin_channel", return_value=False), \
         mock.patch.object(insight_cmd.scheduler_mod, "daily_insight_scan", new=_fake_scan):
        await insight_cmd.InsightsCog.scan_now.callback(cog, interaction)

    assert futott is False, "admin csatornán kívül nem indulhat scan"
    assert any("admin csatorná" in m for m in interaction.sent())


@pytest.mark.asyncio
async def test_command_rejects_unknown_client_without_running_a_scan():
    cog = insight_cmd.InsightsCog(mock.Mock())
    interaction = _Interaction()
    futott = False

    async def _fake_scan(**_k):
        nonlocal futott
        futott = True
        return {}

    with mock.patch.object(insight_cmd, "_is_admin_channel", return_value=True), \
         mock.patch.object(insight_cmd, "_resolve_client", return_value=None), \
         mock.patch.object(insight_cmd.scheduler_mod, "daily_insight_scan", new=_fake_scan):
        await insight_cmd.InsightsCog.scan_now.callback(cog, interaction, client="Nincsilyen")

    assert futott is False
    assert any("Nem található ügyfél" in m for m in interaction.sent())


# ---------------------------------------------------------------------------
# A jelentés szövege
# ---------------------------------------------------------------------------

def test_report_names_every_reason_a_campaign_was_left_out():
    """A válasz megmondja, MIÉRT nem lett insight — ez a parancs fő haszna."""
    out = insight_cmd._format_report(
        {"total": 10, "insights": 2, "skipped_no_history": 5,
         "failed": 3, "routed": 1, "quiet_hours": 1},
        scope="Stopvill", elapsed_s=1.2,
    )
    assert "10" in out and "mature kampány vizsgálva" in out
    assert "2" in out and "insight generálva" in out
    assert "kevés historikus adat" in out
    assert "hibázott" in out
    assert "csendes idő" in out


def test_report_explains_a_zero_result_instead_of_just_showing_zero():
    """Nulla insightnál nem elég a 0 — meg kell mondani, mit jelent."""
    csak_adathiany = insight_cmd._format_report(
        {"total": 4, "insights": 0, "skipped_no_history": 4,
         "failed": 0, "routed": 0, "quiet_hours": 0},
        scope="X", elapsed_s=0.5,
    )
    assert "campaign_insights" in csak_adathiany

    motor_futott = insight_cmd._format_report(
        {"total": 4, "insights": 0, "skipped_no_history": 0,
         "failed": 0, "routed": 0, "quiet_hours": 0},
        scope="X", elapsed_s=0.5,
    )
    assert "egyetlen szabály sem tüzelt" in motor_futott


def test_report_handles_no_mature_campaigns():
    out = insight_cmd._format_report(
        {"total": 0, "insights": 0, "skipped_no_history": 0,
         "failed": 0, "routed": 0, "quiet_hours": 0},
        scope="X", elapsed_s=0.1,
    )
    assert "mature" in out
