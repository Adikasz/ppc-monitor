"""
Napi/heti összefoglaló unit tesztek (13. lépés).

A storage réteget mockoljuk; a tiszta összesítő- és formázó-logikát teszteljük
(idő-ablakok, count-ok, healthy-számítás, top-issue rendezés, üres-eset szöveg).
"""
from __future__ import annotations

from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from src.integrations.discord_router import _format_summary
from src.monitoring import summary as s

TZ = ZoneInfo("Europe/Budapest")


def _alert(campaign_id, severity, name, message, detected_at, ad_account=None):
    campaigns: dict = {"name": name}
    if ad_account is not None:
        campaigns["ad_accounts"] = ad_account
    return {
        "campaign_id": campaign_id,
        "severity": severity,
        "message": message,
        "detected_at": detected_at,
        "campaigns": campaigns,
    }


# ---------------------------------------------------------------------------
# Idő-ablakok
# ---------------------------------------------------------------------------

def test_daily_range_is_yesterday_midnight_window():
    now = datetime(2026, 6, 16, 14, 0, tzinfo=TZ)  # kedd
    frm, to = s.daily_range(now)
    assert (frm.year, frm.month, frm.day, frm.hour) == (2026, 6, 15, 0)
    assert (to.year, to.month, to.day, to.hour) == (2026, 6, 16, 0)


def test_weekend_range_friday_2200_to_monday_0800():
    now = datetime(2026, 6, 16, 9, 0, tzinfo=TZ)  # kedd → előző hétvége
    frm, to = s.weekend_range(now)
    assert (frm.month, frm.day, frm.hour) == (6, 12, 22)  # péntek 22:00
    assert (to.month, to.day, to.hour) == (6, 15, 8)      # hétfő 08:00


def test_weekend_range_on_monday_morning():
    now = datetime(2026, 6, 15, 9, 0, tzinfo=TZ)  # hétfő reggel
    frm, to = s.weekend_range(now)
    assert (frm.month, frm.day, frm.hour) == (6, 12, 22)
    assert (to.month, to.day, to.hour) == (6, 15, 8)


# ---------------------------------------------------------------------------
# Összesítés
# ---------------------------------------------------------------------------

def _patch(stack, *, campaign_ids, alerts):
    stack.enter_context(mock.patch.object(
        s.assignments_storage, "get_campaign_ids_for_user", return_value=campaign_ids,
    ))
    stack.enter_context(mock.patch.object(
        s.alerts_storage, "get_alerts_for_user_in_range", return_value=alerts,
    ))


def test_build_summary_counts_and_healthy():
    import contextlib
    frm = datetime(2026, 6, 15, 0, 0, tzinfo=TZ)
    to = datetime(2026, 6, 16, 0, 0, tzinfo=TZ)
    alerts = [
        _alert(10, "critical", "A", "CPA +67%", "2026-06-15T10:00:00+02:00"),
        _alert(11, "warning", "B", "CTR drop", "2026-06-15T11:00:00+02:00"),
        _alert(11, "warning", "B", "CPC up", "2026-06-15T12:00:00+02:00"),
    ]
    with contextlib.ExitStack() as stack:
        _patch(stack, campaign_ids=[10, 11, 12], alerts=alerts)
        res = s._build_summary_sync(1, frm, to)

    assert res["total_campaigns"] == 3
    assert res["critical_count"] == 1
    assert res["warning_count"] == 2
    assert res["alert_count"] == 3
    # 10 és 11 kapott alertet → 12 egészséges
    assert res["healthy_campaigns"] == 1


def test_top_issues_critical_first():
    import contextlib
    frm = datetime(2026, 6, 15, 0, 0, tzinfo=TZ)
    to = datetime(2026, 6, 16, 0, 0, tzinfo=TZ)
    # DB sorrend: warning előbb, critical később — a rendezésnek a critical-t kell előre tennie
    alerts = [
        _alert(11, "warning", "B", "CTR drop", "2026-06-15T11:00:00+02:00"),
        _alert(10, "critical", "A", "CPA +67%", "2026-06-15T10:00:00+02:00"),
    ]
    with contextlib.ExitStack() as stack:
        _patch(stack, campaign_ids=[10, 11], alerts=alerts)
        res = s._build_summary_sync(1, frm, to)

    assert res["top_issues"][0]["severity"] == "critical"
    assert res["top_issues"][0]["campaign"] == "A"


def test_top_issues_include_client_and_no_account_label_for_single_account():
    import contextlib
    frm = datetime(2026, 6, 15, 0, 0, tzinfo=TZ)
    to = datetime(2026, 6, 16, 0, 0, tzinfo=TZ)
    ad_account = {
        "id": 1, "platform": "meta", "external_account_id": "act_111",
        "account_name": None, "client_id": 5, "clients": {"name": "Stopvill"},
    }
    alerts = [_alert(10, "critical", "A", "ROAS 0.00", "2026-06-15T10:00:00+02:00", ad_account)]
    with contextlib.ExitStack() as stack:
        _patch(stack, campaign_ids=[10], alerts=alerts)
        stack.enter_context(mock.patch.object(
            s.ad_accounts_storage, "get_ad_accounts_for_client", return_value=[ad_account],
        ))
        res = s._build_summary_sync(1, frm, to)

    issue = res["top_issues"][0]
    assert issue["client"] == "Stopvill"
    assert issue["platform"] == "meta"
    assert issue["account_label"] is None


def test_top_issues_show_account_label_when_client_has_multiple_accounts_same_platform():
    import contextlib
    frm = datetime(2026, 6, 15, 0, 0, tzinfo=TZ)
    to = datetime(2026, 6, 16, 0, 0, tzinfo=TZ)
    ad_account = {
        "id": 1, "platform": "meta", "external_account_id": "act_111",
        "account_name": None, "client_id": 5, "clients": {"name": "Stopvill"},
    }
    sibling = {**ad_account, "id": 2, "external_account_id": "act_222"}
    alerts = [_alert(10, "critical", "A", "ROAS 0.00", "2026-06-15T10:00:00+02:00", ad_account)]
    with contextlib.ExitStack() as stack:
        _patch(stack, campaign_ids=[10], alerts=alerts)
        stack.enter_context(mock.patch.object(
            s.ad_accounts_storage, "get_ad_accounts_for_client", return_value=[ad_account, sibling],
        ))
        res = s._build_summary_sync(1, frm, to)

    assert res["top_issues"][0]["account_label"] == "act_111"


def test_top_issues_account_label_prefers_readable_name_over_technical_id():
    """account_name jelenlétekor az OLVASHATÓ nevet mutatja, NEM a technikai
    ID-t — ez a fiók megkülönböztetés fő elvárása (lásd account_catalog +
    scripts/backfill_account_names.py: ezek töltik fel az account_name-t)."""
    import contextlib
    frm = datetime(2026, 6, 15, 0, 0, tzinfo=TZ)
    to = datetime(2026, 6, 16, 0, 0, tzinfo=TZ)
    ad_account = {
        "id": 1, "platform": "meta", "external_account_id": "act_111",
        "account_name": "Stopvill Ads", "client_id": 5, "clients": {"name": "Stopvill"},
    }
    sibling = {**ad_account, "id": 2, "external_account_id": "act_222",
               "account_name": "Stopvill Google Ads"}
    alerts = [_alert(10, "critical", "A", "ROAS 0.00", "2026-06-15T10:00:00+02:00", ad_account)]
    with contextlib.ExitStack() as stack:
        _patch(stack, campaign_ids=[10], alerts=alerts)
        stack.enter_context(mock.patch.object(
            s.ad_accounts_storage, "get_ad_accounts_for_client", return_value=[ad_account, sibling],
        ))
        res = s._build_summary_sync(1, frm, to)

    assert res["top_issues"][0]["account_label"] == "Stopvill Ads"
    assert "act_111" not in res["top_issues"][0]["account_label"]


def test_format_top_issue_line_with_client_and_account_label():
    out = _format_summary(
        {"total_campaigns": 1, "critical_count": 1, "warning_count": 0,
         "alert_count": 1, "healthy_campaigns": 0,
         "top_issues": [{
             "client": "Stopvill", "campaign": "Sales-LEGRAND", "platform": "meta",
             "account_label": "act_1657", "severity": "critical",
             "message": "ROAS 0.00 a cél 3.00 alatt",
         }],
         "from": "2026-06-15T00:00:00+02:00", "to": "2026-06-16T00:00:00+02:00"},
        is_weekly=False,
    )
    assert "Stopvill [META · act_1657] — Sales-LEGRAND — ROAS 0.00 a cél 3.00 alatt" in out


def test_build_summary_no_alerts():
    import contextlib
    frm = datetime(2026, 6, 15, 0, 0, tzinfo=TZ)
    to = datetime(2026, 6, 16, 0, 0, tzinfo=TZ)
    with contextlib.ExitStack() as stack:
        _patch(stack, campaign_ids=[10, 11], alerts=[])
        res = s._build_summary_sync(1, frm, to)

    assert res["alert_count"] == 0
    assert res["healthy_campaigns"] == 2
    assert res["top_issues"] == []


# ---------------------------------------------------------------------------
# Formázás
# ---------------------------------------------------------------------------

def test_format_no_alert_daily():
    out = _format_summary(
        {"total_campaigns": 2, "critical_count": 0, "warning_count": 0,
         "alert_count": 0, "healthy_campaigns": 2, "top_issues": [],
         "from": "2026-06-15T00:00:00+02:00", "to": "2026-06-16T00:00:00+02:00"},
        is_weekly=False,
    )
    assert out.startswith("✅ **Napi összefoglaló** — 2026-06-15")
    assert "nem volt anomália" in out


def test_format_weekly_with_alerts_has_range_and_title():
    out = _format_summary(
        {"total_campaigns": 12, "critical_count": 1, "warning_count": 3,
         "alert_count": 4, "healthy_campaigns": 10,
         "top_issues": [{"campaign": "Google_Brand", "severity": "critical", "message": "Büdzsé elfogyott"}],
         "from": "2026-06-13T22:00:00+02:00", "to": "2026-06-16T08:00:00+02:00"},
        is_weekly=True,
    )
    assert "📊 **Hétvégi összefoglaló** — 2026-06-13 → 2026-06-16" in out
    assert "Top problémák hétvégén" in out
    assert "Alertek hétvégén:** 4" in out
