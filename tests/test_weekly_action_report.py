"""
Heti riport-összefoglaló & akciójavaslat — aggregáció, AI, ClickUp, izoláció.

Négy elvárás áll a tesztek mögött:

1. AZ AGGREGÁCIÓ NE ADJA ÖSSZE UGYANAZT A NAPOT 24-SZER. A `campaign_insights`
   sorai óránkéntiek, de a napra HALMOZOTTAK — a naiv `sum()` a heti költés
   többszörösét adná, és ez a hiba a riportban „reális" számként nézne ki.

2. A parancs UGYANAZT a generátort hívja, mint a hétfői cron job — nem egy
   párhuzamos másolatot. Ez a projekt legdrágább hibaosztálya (az insight
   scan duplikált verziója miatt hetekig senki nem vette észre, hogy az
   ütemezett futás sosem ért végig).

3. EGY ÜGYFÉL HIBÁJA NEM VISZI EL A TÖBBIT — és a hiba nem tűnik el némán,
   hanem névvel és okkal jelenik meg az eredményben.

4. HIÁNYZÓ CLICKUP/ANTHROPIC BEÁLLÍTÁS = graceful skip, nem kivétel. A heti
   aggregátum ilyenkor is elmentődik, különben a jövő heti összehasonlítás
   véglegesen elveszne.
"""
from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from src.bot.commands import reports as reports_cmd
from src.integrations import clickup
from src.monitoring import scheduler as sched
from src.monitoring import weekly_action_report as war

_TZ = ZoneInfo("Europe/Budapest")


# ---------------------------------------------------------------------------
# Segédek
# ---------------------------------------------------------------------------

def _row(cid, ts, *, spend=None, impressions=None, clicks=None,
         conversions=None, conversion_value=None):
    """Egy óránkénti, NAPRA HALMOZOTT campaign_insights sor."""
    return {
        "campaign_id": cid,
        "fetched_at": ts,
        "spend": spend,
        "impressions": impressions,
        "clicks": clicks,
        "conversions": conversions,
        "conversion_value": conversion_value,
    }


def _client(cid, name):
    return {"id": cid, "name": name, "is_active": True}


def _campaign(cid, *, lifecycle="mature"):
    return {"id": cid, "name": f"K{cid}", "lifecycle_state": lifecycle}


def _analysis(summary="Összefoglaló.", items=("egy", "kettő", "három")):
    return {"vezetoi_osszefoglalo": summary, "akcioterv": list(items)}


def _patch_generator(
    stack,
    *,
    clients,
    campaigns=None,
    rows=None,
    cached=None,
    analysis=_analysis(),
    doc=None,
    config_problem=None,
    analysis_boom_on=(),
    doc_none_on=(),
):
    """A generátor összes külső függését kigúnyolja; csak a saját logikája marad.

    Visszatérés: a megfigyelésre érdemes mockok (cache-írás, Claude, ClickUp).
    """
    stack.enter_context(mock.patch.object(war, "_tz", return_value=_TZ))
    stack.enter_context(mock.patch.object(
        war, "_config_problem", return_value=config_problem,
    ))
    stack.enter_context(mock.patch.object(
        war.clients_storage, "list_clients", return_value=list(clients),
    ))
    stack.enter_context(mock.patch.object(
        war.campaigns_storage, "list_campaigns",
        side_effect=lambda cid, **_k: list(
            (campaigns(cid) if callable(campaigns) else campaigns)
            if campaigns is not None else [_campaign(cid * 10)]
        ),
    ))
    stack.enter_context(mock.patch.object(
        war.weekly_metrics_storage, "get_insight_rows_for_campaigns",
        side_effect=lambda ids, *a, **k: list(rows or []),
    ))
    upsert = mock.Mock(return_value=True)
    stack.enter_context(mock.patch.object(
        war.weekly_metrics_storage, "upsert_cached_week", new=upsert,
    ))
    stack.enter_context(mock.patch.object(
        war.weekly_metrics_storage, "get_cached_week", return_value=cached,
    ))
    stack.enter_context(mock.patch.object(
        war.alerts_storage, "get_alert_counts_for_campaigns", return_value=[],
    ))

    async def _analyse(client_name, *_a, **_k):
        if client_name in analysis_boom_on:
            raise RuntimeError("szándékos Claude hiba")
        return analysis

    analyse = mock.AsyncMock(side_effect=_analyse)
    stack.enter_context(mock.patch.object(war, "generate_weekly_analysis", new=analyse))

    async def _create_doc(title, _markdown):
        if any(name in title for name in doc_none_on):
            return None
        return doc or {"doc_id": "abc", "url": "https://app.clickup.com/1/docs/abc"}

    create_doc = mock.AsyncMock(side_effect=_create_doc)
    stack.enter_context(mock.patch.object(
        war.clickup, "create_weekly_report_doc", new=create_doc,
    ))
    return {"upsert": upsert, "analyse": analyse, "create_doc": create_doc}


# ---------------------------------------------------------------------------
# 1. Időablak
# ---------------------------------------------------------------------------

def test_window_is_the_previous_full_monday_to_sunday_week():
    with mock.patch.object(war, "_tz", return_value=_TZ):
        # 2026-08-17 hétfő 08:00 — a cron futásának pillanata
        from_dt, to_dt = war.previous_week_range(datetime(2026, 8, 17, 8, 0, tzinfo=_TZ))

    assert from_dt == datetime(2026, 8, 10, 0, 0, tzinfo=_TZ), "hétfő 00:00"
    assert to_dt == datetime(2026, 8, 17, 0, 0, tzinfo=_TZ), "a felső határ EXKLUZÍV hétfő"
    assert war.week_label(from_dt, to_dt) == "2026-08-10 – 2026-08-16", (
        "a címke a vasárnapig tart, nem a kizárt hétfőig"
    )


def test_manual_run_midweek_gets_the_same_window_as_the_monday_cron():
    """A kézi teszt PONTOSAN azt mutassa, amit a hétfői futás produkálna.

    Ha az ablak a futás napjától függne, a szerdai `/report weekly-now` egy
    másik hétre számolna, és a teszt eredménye semmit nem mondana az élesről.
    """
    with mock.patch.object(war, "_tz", return_value=_TZ):
        hetfo = war.previous_week_range(datetime(2026, 8, 17, 8, 0, tzinfo=_TZ))
        szerda = war.previous_week_range(datetime(2026, 8, 19, 15, 30, tzinfo=_TZ))
        vasarnap = war.previous_week_range(datetime(2026, 8, 23, 23, 59, tzinfo=_TZ))

    assert hetfo == szerda == vasarnap


# ---------------------------------------------------------------------------
# 2. Aggregáció — a kumulált sorok csapdája
# ---------------------------------------------------------------------------

def test_hourly_rows_are_reduced_to_one_row_per_campaign_per_day():
    """A napon belüli sorok NEM adódnak össze — naponta az utolsó (legteljesebb).

    Ez a modul legfontosabb tesztje: a `campaign_insights` óránkénti, de napra
    HALMOZOTT. A naiv összeadás itt 1100-at adna 1000 helyett — és egy ilyen
    hiba a riportban nem tűnik fel, csak az ügyfélnek a számlán.
    """
    rows = [
        # 1-es kampány, hétfő: a nap folyamán 100 → 500 (kumulált)
        _row(1, "2026-08-10T10:00:00+00:00", spend=100),
        _row(1, "2026-08-10T23:00:00+00:00", spend=500),
        # 1-es kampány, kedd: 300
        _row(1, "2026-08-11T23:00:00+00:00", spend=300),
        # 2-es kampány, hétfő: 200
        _row(2, "2026-08-10T23:00:00+00:00", spend=200),
    ]
    totals = war.aggregate_weekly_totals(rows)

    assert totals["spend"] == 1000, "500 (hétfő) + 300 (kedd) + 200 (2-es kampány)"
    assert totals["campaign_days"] == 3


def test_derived_metrics_come_from_the_raw_sums_not_from_row_averages():
    """A CTR impresszió-súlyozott: összes klikk / összes impresszió.

    A soronkénti CTR-ek számtani átlaga mást adna — egy 10 impressziós kampány
    ugyanannyit nyomna a latban, mint egy 100 000 impressziós.
    """
    rows = [
        _row(1, "2026-08-10T23:00:00+00:00",
             spend=1000, impressions=10_000, clicks=500,
             conversions=10, conversion_value=4000),
        _row(2, "2026-08-10T23:00:00+00:00",
             spend=1000, impressions=10, clicks=5,
             conversions=0, conversion_value=0),
    ]
    totals = war.aggregate_weekly_totals(rows)

    assert totals["clicks"] == 505
    assert totals["impressions"] == 10_010
    assert totals["ctr"] == pytest.approx(505 / 10_010)
    assert totals["cpa"] == pytest.approx(2000 / 10)
    assert totals["roas"] == pytest.approx(4000 / 2000)


def test_a_metric_with_no_measurement_is_none_not_zero():
    """A „nem tudjuk" nem ugyanaz, mint a „0" — a változás % nem hazudhat."""
    totals = war.aggregate_weekly_totals(
        [_row(1, "2026-08-10T23:00:00+00:00", spend=500)]
    )
    assert totals["spend"] == 500
    assert totals["conversion_value"] is None
    assert totals["roas"] is None, "bevétel-adat nélkül nincs ROAS"
    assert totals["cpa"] is None, "konverzió nélkül nincs CPA"


def test_empty_input_still_returns_the_full_key_set():
    totals = war.aggregate_weekly_totals([])
    for key in ("spend", "impressions", "clicks", "conversions",
                "conversion_value", "ctr", "cpa", "roas"):
        assert key in totals, key
        assert totals[key] is None


# ---------------------------------------------------------------------------
# 3. Változás %
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("current, previous, expected", [
    (120, 100, 20.0),
    (80, 100, -20.0),
    (100, 100, 0.0),
])
def test_pct_change_math(current, previous, expected):
    assert war.pct_change(current, previous) == pytest.approx(expected)


@pytest.mark.parametrize("current, previous", [
    (100, 0),      # nullához képest nincs értelmes százalék
    (100, None),   # nincs előző heti adat (első futás)
    (None, 100),
    (None, None),
])
def test_pct_change_is_none_when_it_would_be_meaningless(current, previous):
    assert war.pct_change(current, previous) is None


# ---------------------------------------------------------------------------
# 4. Formázás és a metrika-táblázat
# ---------------------------------------------------------------------------

def test_hungarian_number_formatting():
    assert war._fmt_money(1_234_567) == "1 234 567 Ft"
    assert war._fmt_pct_from_ratio(0.0512) == "5,12%"
    assert war._fmt_ratio(3.456) == "3,46"
    assert war._fmt_change(12.34) == "+12,3%"
    assert war._fmt_change(-8.44) == "-8,4%"
    assert war._fmt_money(None) == "nincs adat"
    assert war._fmt_change(None) == "nincs adat"


def test_roas_row_is_dropped_when_there_is_no_revenue_data():
    """Lead-generációs ügyfélnél a „ROAS: nincs adat" sor csak zaj lenne."""
    rows = war.build_metric_rows(
        {"spend": 100, "ctr": 0.05, "cpa": 10, "roas": None},
        {"spend": 80, "ctr": 0.04, "cpa": 8, "roas": None},
    )
    assert [r["label"] for r in rows] == ["Költés", "CTR", "CPA"]


def test_roas_row_is_kept_when_either_week_has_revenue():
    rows = war.build_metric_rows(
        {"spend": 100, "ctr": 0.05, "cpa": 10, "roas": 2.5},
        {"spend": 80, "ctr": 0.04, "cpa": 8, "roas": None},
    )
    roas = [r for r in rows if r["label"] == "ROAS"]
    assert roas and roas[0]["current"] == "2,50"
    assert roas[0]["previous"] == "nincs adat"


def test_metric_rows_carry_the_change_column():
    rows = war.build_metric_rows({"spend": 120}, {"spend": 100})
    spend = next(r for r in rows if r["label"] == "Költés")
    assert spend["current"] == "120 Ft"
    assert spend["previous"] == "100 Ft"
    assert spend["change"] == "+20,0%"


def test_missing_previous_week_shows_no_data_instead_of_a_fake_zero():
    """Az első futáskor nincs előző hét — ez nem „0%-os változás"."""
    rows = war.build_metric_rows({"spend": 120}, None)
    spend = next(r for r in rows if r["label"] == "Költés")
    assert spend["previous"] == "nincs adat"
    assert spend["change"] == "nincs adat"


# ---------------------------------------------------------------------------
# 5. Claude prompt + válasz-parszolás
# ---------------------------------------------------------------------------

def test_prompt_contains_the_client_the_week_and_the_ready_made_numbers():
    metric_rows = war.build_metric_rows({"spend": 120, "ctr": 0.05}, {"spend": 100})
    prompt = war.build_analysis_prompt(
        "Stopvill", "2026-08-10 – 2026-08-16", metric_rows,
        [{"severity": "critical", "metric": "cpa_spike", "count": 3}],
    )

    assert "Stopvill" in prompt
    assert "2026-08-10 – 2026-08-16" in prompt
    assert "120 Ft" in prompt and "100 Ft" in prompt and "+20,0%" in prompt
    assert "CRITICAL / cpa_spike: 3 db" in prompt
    assert "ne számolj újra" in prompt, "a promptnak tiltania kell az újraszámolást"


def test_prompt_says_so_when_there_were_no_alerts():
    prompt = war.build_analysis_prompt("X", "hét", [], [])
    assert "Nem keletkezett CRITICAL vagy WARNING riasztás" in prompt


def test_parses_a_clean_json_response():
    out = war.parse_analysis_response(
        '{"vezetoi_osszefoglalo": "Jó hét volt.", '
        '"akcioterv": ["A", "B", "C"]}'
    )
    assert out == {"vezetoi_osszefoglalo": "Jó hét volt.", "akcioterv": ["A", "B", "C"]}


def test_parses_json_wrapped_in_a_code_fence_or_prose():
    """A modell hajlamos kódblokkba tenni vagy bevezetőt írni — ez nem hiba."""
    fenced = war.parse_analysis_response(
        'Íme az elemzés:\n```json\n'
        '{"vezetoi_osszefoglalo": "Szöveg.", "akcioterv": ["A"]}\n'
        '```\nRemélem segít!'
    )
    assert fenced is not None
    assert fenced["vezetoi_osszefoglalo"] == "Szöveg."
    assert fenced["akcioterv"] == ["A"]


def test_action_plan_is_capped():
    out = war.parse_analysis_response(
        '{"vezetoi_osszefoglalo": "x", "akcioterv": ["1","2","3","4","5","6","7"]}'
    )
    assert len(out["akcioterv"]) == war._MAX_ACTION_ITEMS


@pytest.mark.parametrize("text", [
    None,
    "",
    "Sajnálom, nem tudok segíteni.",                       # nincs JSON
    '{"vezetoi_osszefoglalo": "x"}',                       # nincs akcióterv
    '{"akcioterv": ["A"]}',                                # nincs összefoglaló
    '{"vezetoi_osszefoglalo": "  ", "akcioterv": ["A"]}',  # üres összefoglaló
    '{"vezetoi_osszefoglalo": "x", "akcioterv": []}',      # üres akcióterv
    '{"vezetoi_osszefoglalo": "x", "akcioterv": "A"}',     # rossz típus
    '{"vezetoi_osszefoglalo": "x", "akcioterv": ["A"',     # csonka JSON
])
def test_unusable_responses_are_rejected_instead_of_producing_an_empty_doc(text):
    """Egy fél-válaszból NE szülessen félkész Doc — inkább legyen látható hiba."""
    assert war.parse_analysis_response(text) is None


# ---------------------------------------------------------------------------
# 6. A Doc tartalma
# ---------------------------------------------------------------------------

def test_doc_markdown_follows_the_agreed_structure():
    metric_rows = war.build_metric_rows(
        {"spend": 1_234_567, "ctr": 0.0512, "cpa": 4500, "roas": 3.2},
        {"spend": 1_000_000, "ctr": 0.048, "cpa": 5000, "roas": 2.8},
    )
    md = war.build_report_markdown(
        "Stopvill", "2026-08-10 – 2026-08-16",
        _analysis("Erős hét volt.", ("Emeld a büdzsét", "Vágd a rosszul futókat", "Teszt")),
        metric_rows,
    )

    assert md.startswith("# Heti Riport — Stopvill — 2026-08-10 – 2026-08-16")
    assert "## Vezetői összefoglaló" in md
    assert "Erős hét volt." in md
    assert "## Kulcsmetrikák" in md
    assert "| Metrika | Ez a hét | Előző hét | Változás |" in md
    assert "| Költés | 1 234 567 Ft | 1 000 000 Ft | +23,5% |" in md
    assert "| ROAS | 3,20 | 2,80 | +14,3% |" in md
    assert "## Akcióterv a következő hétre" in md
    assert "1. Emeld a büdzsét" in md
    assert "3. Teszt" in md


def test_doc_title_matches_the_markdown_heading():
    """A Doc neve és a lap H1-e ne tudjon szétcsúszni."""
    title = war.build_doc_title("Stopvill", "2026-08-10 – 2026-08-16")
    md = war.build_report_markdown("Stopvill", "2026-08-10 – 2026-08-16", _analysis(), [])
    assert md.splitlines()[0] == f"# {title}"


# ---------------------------------------------------------------------------
# 7. ClickUp — graceful skip hiányzó konfignál
# ---------------------------------------------------------------------------

class _Cfg:
    def __init__(self, token="pk_1", team="team1", folder="", space=""):
        self.clickup_api_token = token
        self.clickup_team_id = team
        self.clickup_weekly_report_folder_id = folder
        self.clickup_weekly_report_space_id = space


@pytest.mark.parametrize("cfg, expected_fragment", [
    (_Cfg(token=""), "CLICKUP_API_TOKEN"),
    (_Cfg(team=""), "CLICKUP_TEAM_ID"),
    (_Cfg(), "CLICKUP_WEEKLY_REPORT_FOLDER_ID"),   # sem folder, sem space
])
def test_config_error_names_exactly_what_is_missing(cfg, expected_fragment):
    """A néma „0 Doc" helyett mondja meg, MELYIK változó hiányzik."""
    with mock.patch.object(clickup, "get_config", return_value=cfg):
        problem = clickup.weekly_report_config_error()
    assert problem and expected_fragment in problem


@pytest.mark.parametrize("cfg, expected", [
    (_Cfg(space="SP1"), ("SP1", 4)),                 # Space → parent.type 4
    (_Cfg(folder="FD1"), ("FD1", 5)),                # Folder → parent.type 5
    (_Cfg(folder="FD1", space="SP1"), ("FD1", 5)),   # mindkettő → a Folder nyer
])
def test_parent_resolution_picks_the_right_clickup_container(cfg, expected):
    assert clickup._parent_from_config(cfg) == expected


@pytest.mark.asyncio
async def test_missing_token_skips_the_doc_without_touching_the_network():
    """Hiányzó token → warning + None, NEM kivétel és NEM HTTP hívás."""
    with mock.patch.object(clickup, "get_config", return_value=_Cfg(token="")), \
         mock.patch.object(clickup.requests, "post") as post:
        out = await clickup.create_weekly_report_doc("cím", "# tartalom")

    assert out is None
    post.assert_not_called()


@pytest.mark.asyncio
async def test_doc_creation_posts_the_markdown_as_a_page():
    """A tartalom a MÁSODIK hívásban megy fel, text/md formátumban."""
    responses = [
        mock.Mock(status_code=201, json=mock.Mock(return_value={"id": "doc9"})),
        mock.Mock(status_code=201, json=mock.Mock(return_value={"id": "page1"})),
    ]
    with mock.patch.object(clickup, "get_config", return_value=_Cfg(folder="FD1")), \
         mock.patch.object(clickup.requests, "post", side_effect=responses) as post:
        out = await clickup.create_weekly_report_doc("cím", "# tartalom")

    assert out == {"doc_id": "doc9", "url": "https://app.clickup.com/team1/docs/doc9"}

    doc_call, page_call = post.call_args_list
    assert doc_call.args[0].endswith("/workspaces/team1/docs")
    assert doc_call.kwargs["json"]["parent"] == {"id": "FD1", "type": 5}
    assert doc_call.kwargs["json"]["create_page"] is False, (
        "különben a Doc egy üres lappal nyílna meg"
    )
    assert page_call.args[0].endswith("/workspaces/team1/docs/doc9/pages")
    assert page_call.kwargs["json"]["content"] == "# tartalom"
    assert page_call.kwargs["json"]["content_format"] == "text/md"


@pytest.mark.asyncio
async def test_api_errors_return_none_instead_of_raising():
    with mock.patch.object(clickup, "get_config", return_value=_Cfg(folder="FD1")), \
         mock.patch.object(clickup.requests, "post",
                           side_effect=RuntimeError("hálózat")):
        assert await clickup.create_weekly_report_doc("cím", "tartalom") is None

    with mock.patch.object(clickup, "get_config", return_value=_Cfg(folder="FD1")), \
         mock.patch.object(clickup.requests, "post",
                           return_value=mock.Mock(status_code=401, text="nope")):
        assert await clickup.create_weekly_report_doc("cím", "tartalom") is None


# ---------------------------------------------------------------------------
# 8. A generátor — izoláció, kapuk, számlálók
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path_counts_every_client_and_returns_the_doc_links():
    with contextlib.ExitStack() as stack:
        _patch_generator(
            stack,
            clients=[_client(1, "A"), _client(2, "B")],
            rows=[_row(1, "2026-08-10T23:00:00+00:00", spend=500)],
        )
        stats = await war.generate_weekly_action_reports(
            now=datetime(2026, 8, 17, 8, 0, tzinfo=_TZ)
        )

    assert stats["total"] == 2
    assert stats["success"] == 2
    assert stats["failed"] == 0
    assert stats["week_label"] == "2026-08-10 – 2026-08-16"
    assert [d["client"] for d in stats["docs"]] == ["A", "B"]


@pytest.mark.asyncio
async def test_one_clients_failure_does_not_stop_the_others():
    """Fault isolation — ÉS a hiba névvel, okkal jelenik meg, nem némán."""
    with contextlib.ExitStack() as stack:
        _patch_generator(
            stack,
            clients=[_client(1, "A"), _client(2, "B"), _client(3, "C")],
            rows=[_row(1, "2026-08-10T23:00:00+00:00", spend=500)],
            analysis_boom_on={"B"},
        )
        stats = await war.generate_weekly_action_reports(
            now=datetime(2026, 8, 17, 8, 0, tzinfo=_TZ)
        )

    assert stats["total"] == 3
    assert stats["success"] == 2, "az A és a C a B hibája UTÁN is lefut"
    assert stats["failed"] == 1
    assert [e["client"] for e in stats["errors"]] == ["B"]
    assert "Claude" in stats["errors"][0]["error"]


@pytest.mark.asyncio
async def test_a_failed_clickup_doc_is_reported_as_a_failure_with_a_reason():
    with contextlib.ExitStack() as stack:
        _patch_generator(
            stack,
            clients=[_client(1, "A")],
            rows=[_row(1, "2026-08-10T23:00:00+00:00", spend=500)],
            doc_none_on={"A"},
        )
        stats = await war.generate_weekly_action_reports(
            now=datetime(2026, 8, 17, 8, 0, tzinfo=_TZ)
        )

    assert stats["success"] == 0
    assert stats["failed"] == 1
    assert "ClickUp" in stats["errors"][0]["error"]


@pytest.mark.asyncio
async def test_clients_without_a_mature_campaign_are_skipped():
    with contextlib.ExitStack() as stack:
        mocks = _patch_generator(
            stack,
            clients=[_client(1, "A")],
            campaigns=[_campaign(11, lifecycle="learning")],
            rows=[_row(11, "2026-08-10T23:00:00+00:00", spend=500)],
        )
        stats = await war.generate_weekly_action_reports(
            now=datetime(2026, 8, 17, 8, 0, tzinfo=_TZ)
        )

    assert stats["skipped_no_mature"] == 1
    assert stats["total"] == 0
    mocks["analyse"].assert_not_awaited()


@pytest.mark.asyncio
async def test_clients_with_no_spend_in_either_week_are_skipped():
    """Üres riportot nem generálunk — de a (nulla) aggregátum akkor is mentődik."""
    with contextlib.ExitStack() as stack:
        mocks = _patch_generator(stack, clients=[_client(1, "A")], rows=[])
        stats = await war.generate_weekly_action_reports(
            now=datetime(2026, 8, 17, 8, 0, tzinfo=_TZ)
        )

    assert stats["skipped_no_data"] == 1
    assert stats["total"] == 0
    mocks["analyse"].assert_not_awaited()
    mocks["upsert"].assert_called_once()


@pytest.mark.asyncio
async def test_a_client_with_spend_only_last_week_still_gets_a_report():
    """Nullára esett költés ROSSZ hír — pont ilyenkor kell a riport."""
    with contextlib.ExitStack() as stack:
        _patch_generator(
            stack,
            clients=[_client(1, "A")],
            rows=[],
            cached={"spend": 900_000, "ctr": 0.04, "cpa": 5000, "roas": None},
        )
        stats = await war.generate_weekly_action_reports(
            now=datetime(2026, 8, 17, 8, 0, tzinfo=_TZ)
        )

    assert stats["skipped_no_data"] == 0
    assert stats["success"] == 1


@pytest.mark.asyncio
async def test_client_filter_narrows_the_run_to_one_client():
    with contextlib.ExitStack() as stack:
        _patch_generator(
            stack,
            clients=[_client(1, "A"), _client(2, "B")],
            rows=[_row(1, "2026-08-10T23:00:00+00:00", spend=500)],
        )
        stats = await war.generate_weekly_action_reports(
            client_id=2, now=datetime(2026, 8, 17, 8, 0, tzinfo=_TZ)
        )

    assert stats["total"] == 1
    assert [d["client"] for d in stats["docs"]] == ["B"]


# ---------------------------------------------------------------------------
# 9. Hiányzó beállítás → graceful skip (a cache akkor is épül)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_config_skips_delivery_without_burning_claude_calls():
    """Kézbesíthetetlen riporthoz ne hívjunk (fizetős) Claude-ot."""
    with contextlib.ExitStack() as stack:
        mocks = _patch_generator(
            stack,
            clients=[_client(1, "A"), _client(2, "B")],
            rows=[_row(1, "2026-08-10T23:00:00+00:00", spend=500)],
            config_problem="hiányzik a `CLICKUP_API_TOKEN`",
        )
        stats = await war.generate_weekly_action_reports(
            now=datetime(2026, 8, 17, 8, 0, tzinfo=_TZ)
        )

    assert stats["skipped_config"] == 2
    assert stats["success"] == 0
    assert stats["failed"] == 0, "a hiányzó beállítás nem HIBA, hanem kihagyás"
    assert "CLICKUP_API_TOKEN" in stats["config_error"]
    mocks["analyse"].assert_not_awaited()
    mocks["create_doc"].assert_not_awaited()


@pytest.mark.asyncio
async def test_the_weekly_aggregate_is_cached_even_when_the_report_fails():
    """A cache-írás a Claude/ClickUp lépés ELŐTT történik.

    Enélkül egy elhasalt hét után a KÖVETKEZŐ heti riportból is hiányozna az
    összehasonlítás — a nyers sorokat addigra törli az óránkénti prune.
    """
    with contextlib.ExitStack() as stack:
        mocks = _patch_generator(
            stack,
            clients=[_client(1, "A")],
            rows=[_row(1, "2026-08-10T23:00:00+00:00", spend=500)],
            analysis_boom_on={"A"},
        )
        stats = await war.generate_weekly_action_reports(
            now=datetime(2026, 8, 17, 8, 0, tzinfo=_TZ)
        )

    assert stats["failed"] == 1
    mocks["upsert"].assert_called_once()
    args = mocks["upsert"].call_args.args
    assert args[0] == 1, "client_id"
    assert args[1].isoformat() == "2026-08-10", "a hét hétfője"
    assert args[2]["spend"] == 500


@pytest.mark.asyncio
async def test_closing_log_line_carries_the_full_breakdown(caplog):
    """A záró sort olvassa a Railway log — minden szám legyen benne."""
    with contextlib.ExitStack() as stack:
        _patch_generator(
            stack,
            clients=[_client(1, "A"), _client(2, "B")],
            rows=[_row(1, "2026-08-10T23:00:00+00:00", spend=500)],
            analysis_boom_on={"B"},
        )
        # A modul-loggerek `propagate = False`-szal jönnek létre
        # (src/utils/logging.py), ezért a caplog gyökér-handlere nem látná őket.
        logger = logging.getLogger("src.monitoring.weekly_action_report")
        logger.addHandler(caplog.handler)
        try:
            await war.generate_weekly_action_reports(
                now=datetime(2026, 8, 17, 8, 0, tzinfo=_TZ)
            )
        finally:
            logger.removeHandler(caplog.handler)

    zaro = [r.getMessage() for r in caplog.records if "Heti riport kész:" in r.getMessage()]
    assert zaro, "nincs záró összesítő log sor"
    sor = zaro[-1]
    assert "2 ügyfél feldolgozva" in sor
    assert "1 sikeres" in sor
    assert "1 hibázott" in sor
    assert "2026-08-10 – 2026-08-16" in sor


# ---------------------------------------------------------------------------
# 10. STRUKTURÁLIS: a parancs és a cron ugyanazt a generátort hívja
# ---------------------------------------------------------------------------

def test_scheduler_exposes_the_real_generator_object():
    """Nem másolat, nem wrapper-újraimplementáció: UGYANAZ az objektum."""
    assert sched.generate_weekly_action_reports is war.generate_weekly_action_reports
    assert reports_cmd.scheduler_mod is sched


@pytest.mark.asyncio
async def test_cron_job_and_command_route_through_the_same_symbol():
    """Egyetlen patch mindkét utat elkapja — tehát egyetlen implementáció van.

    Ha a parancs egy párhuzamos másolatot hívna, ez a patch csak az egyik utat
    fogná el, és az `hivasok` lista nem tartalmazna két bejegyzést. Pontosan ez
    a hiba maradt rejtve hetekig az insight scan-nél.
    """
    hivasok: list[dict] = []

    async def _fake(*, client_id=None):
        hivasok.append({"client_id": client_id})
        return war._empty_stats()

    cog = reports_cmd.ReportsCog(mock.Mock())
    interaction = _Interaction()

    with mock.patch.object(sched, "generate_weekly_action_reports", new=_fake), \
         mock.patch.object(reports_cmd, "_is_admin_channel", return_value=True), \
         mock.patch.object(reports_cmd.audit, "log_action"):
        await sched.weekly_action_report_job()                       # a cron útja
        await reports_cmd.ReportsCog.weekly_now.callback(cog, interaction)  # a parancsé

    assert hivasok == [{"client_id": None}, {"client_id": None}], (
        "mindkét út ugyanazon a szimbólumon megy keresztül"
    )


@pytest.mark.asyncio
async def test_the_cron_job_is_registered_for_monday_08_00():
    """A regisztráció maga is teszthiány volt korábban — a cron létezzen tényleg."""
    with mock.patch.object(sched, "get_config",
                           return_value=mock.Mock(timezone="Europe/Budapest")):
        scheduler = sched.start_scheduler()
        try:
            job = scheduler.get_job("weekly_action_report")
            assert job is not None, "nincs regisztrálva a heti riport job"
            fields = {f.name: str(f) for f in job.trigger.fields}
            assert fields["day_of_week"] == "mon"
            assert fields["hour"] == "8"
            assert fields["minute"] == "0"
            assert job.func is sched.weekly_action_report_job
        finally:
            sched.shutdown_scheduler()


# ---------------------------------------------------------------------------
# 11. A parancs válasza
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
async def test_command_is_rejected_outside_the_admin_channel():
    cog = reports_cmd.ReportsCog(mock.Mock())
    interaction = _Interaction()
    futott = False

    async def _fake(**_k):
        nonlocal futott
        futott = True
        return war._empty_stats()

    with mock.patch.object(reports_cmd, "_is_admin_channel", return_value=False), \
         mock.patch.object(reports_cmd.scheduler_mod,
                           "generate_weekly_action_reports", new=_fake):
        await reports_cmd.ReportsCog.weekly_now.callback(cog, interaction)

    assert futott is False
    assert any("admin csatorná" in m for m in interaction.sent())


@pytest.mark.asyncio
async def test_command_with_client_scopes_the_run():
    cog = reports_cmd.ReportsCog(mock.Mock())
    interaction = _Interaction()
    hivas = {}

    async def _fake(*, client_id=None):
        hivas["client_id"] = client_id
        stats = war._empty_stats()
        stats.update({"total": 1, "success": 1, "week_label": "2026-08-10 – 2026-08-16"})
        return stats

    with mock.patch.object(reports_cmd, "_is_admin_channel", return_value=True), \
         mock.patch.object(reports_cmd, "_resolve_client",
                           return_value={"id": 7, "name": "Stopvill"}), \
         mock.patch.object(reports_cmd.scheduler_mod,
                           "generate_weekly_action_reports", new=_fake), \
         mock.patch.object(reports_cmd.audit, "log_action"):
        await reports_cmd.ReportsCog.weekly_now.callback(cog, interaction, client="Stopvill")

    assert hivas["client_id"] == 7
    assert any("Stopvill" in m for m in interaction.sent())


@pytest.mark.asyncio
async def test_command_rejects_unknown_client_without_running_anything():
    cog = reports_cmd.ReportsCog(mock.Mock())
    interaction = _Interaction()
    futott = False

    async def _fake(**_k):
        nonlocal futott
        futott = True
        return war._empty_stats()

    with mock.patch.object(reports_cmd, "_is_admin_channel", return_value=True), \
         mock.patch.object(reports_cmd, "_resolve_client", return_value=None), \
         mock.patch.object(reports_cmd.scheduler_mod,
                           "generate_weekly_action_reports", new=_fake):
        await reports_cmd.ReportsCog.weekly_now.callback(cog, interaction, client="Nincsilyen")

    assert futott is False
    assert any("Nem található ügyfél" in m for m in interaction.sent())


def test_report_names_every_client_that_failed_and_why():
    stats = war._empty_stats()
    stats.update({
        "total": 5, "success": 3, "failed": 2,
        "skipped_no_mature": 1, "skipped_no_data": 2,
        "week_label": "2026-08-10 – 2026-08-16",
        "errors": [
            {"client": "Stopvill", "error": "a ClickUp Doc létrehozása sikertelen"},
            {"client": "Béta Kft", "error": "rate limit"},
        ],
        "docs": [{"client": "Alfa", "url": "https://app.clickup.com/1/docs/x"}],
    })
    out = reports_cmd._format_report(stats, scope="minden ügyfél", elapsed_s=12.3)

    assert "5" in out and "ügyfél feldolgozva" in out
    assert "3" in out and "ClickUp Doc elkészült" in out
    assert "Stopvill" in out and "Béta Kft" in out
    assert "rate limit" in out
    assert "nincs `mature` kampánya" in out
    assert "2026-08-10 – 2026-08-16" in out
    assert "https://app.clickup.com/1/docs/x" in out


def test_report_explains_a_zero_run_instead_of_just_showing_zero():
    stats = war._empty_stats()
    stats.update({"skipped_no_mature": 4, "week_label": "2026-08-10 – 2026-08-16"})
    out = reports_cmd._format_report(stats, scope="X", elapsed_s=0.4)
    assert "0" in out and "mature" in out


def test_report_surfaces_a_missing_env_var_instead_of_a_silent_zero():
    """A hiányzó ClickUp beállítás a válaszban is látszódjon, ne csak a logban."""
    stats = war._empty_stats()
    stats.update({
        "total": 3, "skipped_config": 3,
        "config_error": "hiányzik a `CLICKUP_WEEKLY_REPORT_FOLDER_ID`",
        "week_label": "2026-08-10 – 2026-08-16",
    })
    out = reports_cmd._format_report(stats, scope="minden ügyfél", elapsed_s=1.0)
    assert "CLICKUP_WEEKLY_REPORT_FOLDER_ID" in out
    assert "jövő heti" in out, "mondja meg, hogy az adat azért nem veszett el"
