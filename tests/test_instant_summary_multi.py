"""
`/my summary-now` — az OM ÖSSZES fiókjának azonnali, összesített pillanatképe.

Kulcs-elvárások:
  - fiókonkénti hiba-izoláció (egy fiók API hibája nem buktatja meg a többit)
  - korlátozott párhuzamosság (a `max_concurrency` sosem lépődik túl)
  - helyes összesítés (kampányszám, kritikus/warning/healthy, költés)
  - a top_issues az összes SIKERES fiókból összefésülve, súlyosság szerint
  - a formázás jelzi az élő/részleges jelleget, és a hibás fiókokat is
"""
from __future__ import annotations

import asyncio

import pytest

from src.monitoring import instant_summary as isum


def _account(aid, ext, platform="meta", client_id=1, name=None):
    return {
        "id": aid, "external_account_id": ext, "platform": platform,
        "client_id": client_id, "account_name": name,
    }


@pytest.mark.asyncio
async def test_ures_fioklista_ures_osszesitest_ad():
    s = await isum.generate_instant_summary_for_accounts([])
    assert s["accounts_total"] == 0
    assert s["accounts_ok"] == 0
    assert s["top_issues"] == []


@pytest.mark.asyncio
async def test_osszesites_tobb_sikeres_fiokbol(monkeypatch):
    accounts = [_account(1, "act_1"), _account(2, "act_2"), _account(3, "act_3")]

    fake_results = {
        1: {"total_campaigns": 10, "campaigns_with_data": 5, "critical_count": 1,
            "warning_count": 0, "healthy_campaigns": 4, "alert_count": 1,
            "spend_today": 100.0, "top_issues": [{"severity": "critical", "campaign": "A"}],
            "account_label": "Fiok1", "platform": "meta"},
        2: {"total_campaigns": 20, "campaigns_with_data": 20, "critical_count": 0,
            "warning_count": 2, "healthy_campaigns": 18, "alert_count": 2,
            "spend_today": 200.0, "top_issues": [{"severity": "warning", "campaign": "B"}],
            "account_label": "Fiok2", "platform": "google"},
        3: {"total_campaigns": 5, "campaigns_with_data": 5, "critical_count": 0,
            "warning_count": 0, "healthy_campaigns": 5, "alert_count": 0,
            "spend_today": 50.0, "top_issues": [], "account_label": "Fiok3", "platform": "meta"},
    }

    async def _fake_single(acct, *, client_name=None):
        return fake_results[acct["id"]]

    monkeypatch.setattr(isum, "generate_instant_account_summary", _fake_single)

    s = await isum.generate_instant_summary_for_accounts(accounts)

    assert s["accounts_total"] == 3
    assert s["accounts_ok"] == 3
    assert s["accounts_failed"] == 0
    assert s["total_campaigns"] == 35
    assert s["campaigns_with_data"] == 30
    assert s["critical_count"] == 1
    assert s["warning_count"] == 2
    assert s["healthy_campaigns"] == 27
    assert s["alert_count"] == 3
    assert s["spend_today"] == 350.0
    assert len(s["per_account"]) == 3
    # top_issues súlyosság szerint: critical előbb, mint warning
    assert [i["severity"] for i in s["top_issues"]] == ["critical", "warning"]


@pytest.mark.asyncio
async def test_egy_fiok_hibaja_nem_buktatja_meg_a_tobbit(monkeypatch):
    accounts = [_account(1, "act_1"), _account(2, "act_2")]

    async def _fake_single(acct, *, client_name=None):
        if acct["id"] == 1:
            raise RuntimeError("Meta API 401")
        return {
            "total_campaigns": 5, "campaigns_with_data": 5, "critical_count": 0,
            "warning_count": 0, "healthy_campaigns": 5, "alert_count": 0,
            "spend_today": 10.0, "top_issues": [], "account_label": "Fiok2", "platform": "meta",
        }

    monkeypatch.setattr(isum, "generate_instant_account_summary", _fake_single)

    s = await isum.generate_instant_summary_for_accounts(accounts)

    assert s["accounts_ok"] == 1
    assert s["accounts_failed"] == 1
    assert len(s["errors"]) == 1
    assert "Meta API 401" in s["errors"][0]["error"]
    # a sikeres fiók adata ÉRVÉNYES marad
    assert s["total_campaigns"] == 5


@pytest.mark.asyncio
async def test_konkurencia_korlatozott(monkeypatch):
    """A max_concurrency sosem lépődik túl, még sok fióknál sem."""
    accounts = [_account(i, f"act_{i}") for i in range(12)]
    in_flight = {"current": 0, "peak": 0}

    async def _fake_single(acct, *, client_name=None):
        in_flight["current"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["current"])
        await asyncio.sleep(0.01)
        in_flight["current"] -= 1
        return {
            "total_campaigns": 1, "campaigns_with_data": 1, "critical_count": 0,
            "warning_count": 0, "healthy_campaigns": 1, "alert_count": 0,
            "spend_today": 0.0, "top_issues": [], "account_label": str(acct["id"]),
            "platform": "meta",
        }

    monkeypatch.setattr(isum, "generate_instant_account_summary", _fake_single)

    s = await isum.generate_instant_summary_for_accounts(accounts, max_concurrency=3)

    assert s["accounts_ok"] == 12
    assert in_flight["peak"] <= 3


# ---------------------------------------------------------------------------
# Formázás
# ---------------------------------------------------------------------------

def _base_multi(**over):
    base = {
        "accounts_total": 10, "accounts_ok": 10, "accounts_failed": 0, "errors": [],
        "total_campaigns": 250, "campaigns_with_data": 100,
        "critical_count": 0, "warning_count": 0, "healthy_campaigns": 100,
        "alert_count": 0, "spend_today": 5000.0, "per_account": [],
        "top_issues": [], "fetched_at": "2026-07-27T09:15:00+02:00",
        "is_live": True,
    }
    base.update(over)
    return base


def test_format_multi_minden_rendben():
    text = isum.format_instant_summary_multi(_base_multi())
    assert "10/10 fiók" in text
    assert "nincs anomália egyik fiókban sem" in text


def test_format_multi_hibas_fiokot_jelzi():
    text = isum.format_instant_summary_multi(_base_multi(
        accounts_ok=8, accounts_failed=2,
        errors=[
            {"account_label": "Fiok-A", "error": "401"},
            {"account_label": "Fiok-B", "error": "timeout"},
        ],
    ))
    assert "8/10 fiók" in text
    assert "2 fióknál nem sikerült" in text
    assert "Fiok-A" in text and "Fiok-B" in text


def test_format_multi_problemas_fiokok_bontasa():
    text = isum.format_instant_summary_multi(_base_multi(
        accounts_total=3, accounts_ok=3,
        critical_count=2, warning_count=1, alert_count=3, healthy_campaigns=97,
        per_account=[
            {"account_label": "GTX", "platform": "meta", "total_campaigns": 50,
             "critical_count": 2, "warning_count": 0, "alert_count": 2},
            {"account_label": "JOY", "platform": "meta", "total_campaigns": 30,
             "critical_count": 0, "warning_count": 1, "alert_count": 1},
            {"account_label": "Clean", "platform": "google", "total_campaigns": 20,
             "critical_count": 0, "warning_count": 0, "alert_count": 0},
        ],
        top_issues=[{
            "client": "GTX Kft", "campaign": "Prospecting", "severity": "critical",
            "message": "ROAS 0.5", "account_label": "GTX",
        }],
    ))
    assert "Fiókonkénti bontás" in text
    assert "GTX" in text and "JOY" in text
    # a tiszta fiók NEM jelenik meg névvel a bontásban, csak összesítve
    assert "+ 1 fiók rendben" in text
    assert "ROAS 0.5" in text


def test_format_multi_meg_nincs_mai_adat():
    text = isum.format_instant_summary_multi(_base_multi(campaigns_with_data=0))
    assert "Ma még egyik figyelt kampányra sincs adat" in text


def test_format_multi_minden_fiok_hibas():
    text = isum.format_instant_summary_multi(_base_multi(
        accounts_ok=0, accounts_failed=3,
        errors=[{"account_label": f"F{i}", "error": "x"} for i in range(3)],
    ))
    assert "Egyik fiókra sem sikerült" in text
