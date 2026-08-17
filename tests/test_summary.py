"""
Napi/heti összefoglaló unit tesztek (13. lépés).

A storage réteget mockoljuk; a tiszta összesítő- és formázó-logikát teszteljük
(idő-ablakok, count-ok, healthy-számítás, top-issue rendezés, üres-eset szöveg).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from src.integrations.discord_router import _format_summary, split_message
from src.monitoring import summary as s

TZ = ZoneInfo("Europe/Budapest")
UTC = timezone.utc


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
# A napi ablak PONTOS határai
#
# A napi összefoglaló a scheduleren KEDD–PÉNTEK 09:00-kor fut
# (scheduler.py: trigger="cron", hour=9, day_of_week="tue-fri", a scheduler
# időzónája a config.timezone). Az itt tesztelt elvárás: az ablak a TELJES
# ELŐZŐ NAPTÁRI NAP legyen a konfigurált időzónában — NEM "az elmúlt 24 óra a
# futás pillanatától", és NEM valamilyen "még nyitott probléma" szűrés.
# ---------------------------------------------------------------------------

def test_daily_range_starts_and_ends_at_exact_local_midnight():
    """09:00-s futás → tegnap 00:00:00.000000 → ma 00:00:00.000000 (helyi idő).

    A futás órája/perce NEM szivároghat be az ablakba: a `daily_range` a
    `now`-ból csak a NAPOT használja, az időt nullázza.
    """
    frm, to = s.daily_range(datetime(2026, 8, 18, 9, 0, 37, 123456, tzinfo=TZ))

    assert frm.isoformat() == "2026-08-17T00:00:00+02:00"
    assert to.isoformat() == "2026-08-18T00:00:00+02:00"
    # Explicit: minden idő-komponens nulla, és a konfigurált időzónában vagyunk.
    for boundary in (frm, to):
        assert (boundary.hour, boundary.minute, boundary.second, boundary.microsecond) == (0, 0, 0, 0)
        assert boundary.tzinfo is not None
        assert boundary.utcoffset() is not None


def test_daily_range_is_not_a_rolling_24h_window():
    """Ugyanaz a nap, más futásidő → UGYANAZ az ablak.

    Ha "az elmúlt 24 óra" logika futna, a 09:00-s és a 23:00-s futás ablaka
    eltérne — ez a teszt épp ezt zárja ki.
    """
    reggel = s.daily_range(datetime(2026, 8, 18, 9, 0, tzinfo=TZ))
    keso_este = s.daily_range(datetime(2026, 8, 18, 23, 59, 59, tzinfo=TZ))
    hajnal = s.daily_range(datetime(2026, 8, 18, 0, 0, 1, tzinfo=TZ))

    assert reggel == keso_este == hajnal


def test_daily_range_covers_the_whole_calendar_day_across_dst():
    """Óraátállításkor is a TELJES naptári napot fedi (25, illetve 23 óra).

    A `today0 - timedelta(days=1)` fali-óra aritmetika: a tegnapi 00:00-t adja,
    nem "24 órával korábbat" — így az óraátállítás napja sem csúszik el.
    """
    # 2026-10-25: óra vissza → a nap 25 órás
    frm, to = s.daily_range(datetime(2026, 10, 26, 9, 0, tzinfo=TZ))
    assert frm.isoformat() == "2026-10-25T00:00:00+02:00"
    assert to.isoformat() == "2026-10-26T00:00:00+01:00"
    assert (to.astimezone(UTC) - frm.astimezone(UTC)) == timedelta(hours=25)

    # 2026-03-29: óra előre → a nap 23 órás
    frm, to = s.daily_range(datetime(2026, 3, 30, 9, 0, tzinfo=TZ))
    assert frm.isoformat() == "2026-03-29T00:00:00+01:00"
    assert to.isoformat() == "2026-03-30T00:00:00+02:00"
    assert (to.astimezone(UTC) - frm.astimezone(UTC)) == timedelta(hours=23)


def test_daily_query_filter_is_gte_from_and_lt_to():
    """A tényleges WHERE feltétel: detected_at >= tegnap 00:00 ÉS < ma 00:00.

    Ez a teszt a storage rétegig megy le, és rögzíti a PostgREST-nek küldött
    KONKRÉT határértékeket — a felső határ szigorúan `lt` (nem `lte`), így a
    ma 00:00:00-kor keletkezett alert már a KÖVETKEZŐ napé.
    """
    from src.storage import alerts as alerts_storage

    calls: dict[str, tuple] = {}

    class _Query:
        def select(self, *a):
            return self

        def in_(self, *a):
            return self

        def gte(self, field, value):
            calls["gte"] = (field, value)
            return self

        def lt(self, field, value):
            calls["lt"] = (field, value)
            return self

        def order(self, *a, **k):
            return self

        def execute(self):
            return mock.Mock(data=[])

    frm, to = s.daily_range(datetime(2026, 8, 18, 9, 0, tzinfo=TZ))
    with mock.patch.object(
        alerts_storage, "get_supabase",
        return_value=mock.Mock(table=lambda _t: _Query()),
    ), mock.patch(
        "src.storage.assignments.get_campaign_ids_for_user", return_value=[10]
    ):
        alerts_storage.get_alerts_for_user_in_range(1, frm, to)

    # Alsó határ: tegnap 00:00 (inkluzív) — felső: ma 00:00 (EXKLUZÍV).
    assert calls["gte"] == ("detected_at", "2026-08-17T00:00:00+02:00")
    assert calls["lt"] == ("detected_at", "2026-08-18T00:00:00+02:00")
    assert "lte" not in calls  # `lte` felső határral a ma 00:00-s alert is beesne


def _in_daily_window(detected_at: datetime, now: datetime) -> bool:
    """A tárolt lekérdezés szemantikája: from <= detected_at < to."""
    frm, to = s.daily_range(now)
    return frm <= detected_at < to


def test_yesterday_2350_is_included_day_before_yesterday_2350_is_not():
    """A konkrét elvárás: a tegnap 23:50-es alert BENNE van a mai reggeli
    összefoglalóban, a tegnapelőtt 23:50-es NINCS."""
    reggeli_futas = datetime(2026, 8, 18, 9, 0, tzinfo=TZ)  # kedd 09:00

    tegnap_2350 = datetime(2026, 8, 17, 23, 50, tzinfo=TZ)
    tegnapelott_2350 = datetime(2026, 8, 16, 23, 50, tzinfo=TZ)

    assert _in_daily_window(tegnap_2350, reggeli_futas) is True
    assert _in_daily_window(tegnapelott_2350, reggeli_futas) is False


def test_daily_window_boundaries_are_inclusive_start_exclusive_end():
    """A fél-nyitott intervallum két széle — egyik nap se lógjon át a másikba."""
    reggeli_futas = datetime(2026, 8, 18, 9, 0, tzinfo=TZ)
    cases = [
        # (időpont,                                            benne van?)
        (datetime(2026, 8, 16, 23, 59, 59, 999999, tzinfo=TZ), False),  # tegnapelőtt vége
        (datetime(2026, 8, 17, 0, 0, 0, tzinfo=TZ), True),              # tegnap 00:00 — alsó határ
        (datetime(2026, 8, 17, 12, 0, 0, tzinfo=TZ), True),             # tegnap dél
        (datetime(2026, 8, 17, 23, 59, 59, 999999, tzinfo=TZ), True),   # tegnap utolsó pillanata
        (datetime(2026, 8, 18, 0, 0, 0, tzinfo=TZ), False),             # ma 00:00 — felső határ
        (datetime(2026, 8, 18, 8, 30, 0, tzinfo=TZ), False),            # ma reggel, a futás előtt
    ]
    for detected_at, expected in cases:
        assert _in_daily_window(detected_at, reggeli_futas) is expected, detected_at


def test_daily_summary_covers_yesterday_end_to_end():
    """Végponttól végpontig: a `_build_summary_sync` a kapott ablakkal dolgozik,
    és a tegnap 23:50-es alert megjelenik a listában."""
    import contextlib

    frm, to = s.daily_range(datetime(2026, 8, 18, 9, 0, tzinfo=TZ))
    osszes = [
        _alert(10, "critical", "Tegnap-2350", "ROAS 0.00", "2026-08-17T23:50:00+02:00"),
        _alert(11, "warning", "Tegnapelott-2350", "CTR drop", "2026-08-16T23:50:00+02:00"),
    ]
    # A storage a fenti fél-nyitott feltétellel szűr — itt ugyanezt szimuláljuk.
    szurt = [
        a for a in osszes
        if frm <= datetime.fromisoformat(a["detected_at"]) < to
    ]

    with contextlib.ExitStack() as stack:
        _patch(stack, campaign_ids=[10, 11], alerts=szurt)
        res = s._build_summary_sync(1, frm, to)

    kampanyok = [i["campaign"] for i in res["top_issues"]]
    assert "Tegnap-2350" in kampanyok
    assert "Tegnapelott-2350" not in kampanyok
    assert res["alert_count"] == 1


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
    assert "Problémák hétvégén" in out
    assert "Alertek hétvégén:** 4" in out


# ---------------------------------------------------------------------------
# Teljes problémalista (NINCS top-5 vágás)
# ---------------------------------------------------------------------------

def _many_alerts(n_critical: int, n_warning: int) -> list[dict]:
    """n_critical + n_warning alert, kampányonként külön ID-val."""
    alerts = []
    for i in range(n_critical):
        alerts.append(_alert(100 + i, "critical", f"C{i}", f"CPA +{i}%",
                             "2026-06-15T10:00:00+02:00"))
    for i in range(n_warning):
        alerts.append(_alert(200 + i, "warning", f"W{i}", f"CTR -{i}%",
                             "2026-06-15T11:00:00+02:00"))
    return alerts


def test_all_critical_and_warning_issues_are_listed_not_just_top5():
    """A fő regresszió: korábban fix top-5 volt, így 12 riasztásból 7 kimaradt."""
    import contextlib
    frm = datetime(2026, 6, 15, 0, 0, tzinfo=TZ)
    to = datetime(2026, 6, 16, 0, 0, tzinfo=TZ)
    alerts = _many_alerts(7, 5)  # 12 riasztás
    with contextlib.ExitStack() as stack:
        _patch(stack, campaign_ids=list(range(100, 112)), alerts=alerts)
        res = s._build_summary_sync(1, frm, to)

    assert len(res["top_issues"]) == 12
    assert res["issues_truncated"] == 0
    assert res["critical_count"] == 7
    assert res["warning_count"] == 5


def test_issue_list_is_capped_only_by_safety_ceiling_and_reports_the_rest():
    """A biztonsági plafon nem néma: az `issues_truncated` a kimaradt sorok száma."""
    import contextlib
    frm = datetime(2026, 6, 15, 0, 0, tzinfo=TZ)
    to = datetime(2026, 6, 16, 0, 0, tzinfo=TZ)
    alerts = _many_alerts(2, s._MAX_ISSUE_LINES + 8)
    with contextlib.ExitStack() as stack:
        _patch(stack, campaign_ids=[], alerts=alerts)
        res = s._build_summary_sync(1, frm, to)

    assert len(res["top_issues"]) == s._MAX_ISSUE_LINES
    assert res["issues_truncated"] == 10
    # Az összesített számok a TELJES halmazt tükrözik, nem a listát.
    assert res["warning_count"] == s._MAX_ISSUE_LINES + 8


def test_short_list_stays_flat():
    """15 alattinál marad a régi, lapos felsorolás (nincs fölösleges csoport-fejléc)."""
    issues = [{"client": "X", "campaign": f"K{i}", "severity": "critical", "message": "m"}
              for i in range(4)]
    out = _format_summary(
        {"total_campaigns": 10, "critical_count": 4, "warning_count": 0,
         "alert_count": 4, "healthy_campaigns": 6, "top_issues": issues,
         "from": "2026-06-15T00:00:00+02:00", "to": "2026-06-16T00:00:00+02:00"},
        is_weekly=False,
    )
    assert "KRITIKUS: 4 db" not in out
    for i in range(4):
        assert f"K{i}" in out


def test_long_list_is_grouped_by_severity_with_counts():
    issues = (
        [{"client": "X", "campaign": f"C{i}", "severity": "critical", "message": "m"}
         for i in range(12)]
        + [{"client": "X", "campaign": f"W{i}", "severity": "warning", "message": "m"}
           for i in range(8)]
    )
    out = _format_summary(
        {"total_campaigns": 30, "critical_count": 12, "warning_count": 8,
         "alert_count": 20, "healthy_campaigns": 10, "top_issues": issues,
         "from": "2026-06-15T00:00:00+02:00", "to": "2026-06-16T00:00:00+02:00"},
        is_weekly=False,
    )
    assert "🔴 **KRITIKUS: 12 db**" in out
    assert "🟡 **FIGYELMEZTETÉS: 8 db**" in out
    # A critical csoport a warning ELŐTT áll.
    assert out.index("KRITIKUS: 12 db") < out.index("FIGYELMEZTETÉS: 8 db")
    # Minden sor kint van, egy sem esett ki.
    for i in range(12):
        assert f"C{i}" in out
    for i in range(8):
        assert f"W{i}" in out


def test_aggregate_counts_are_totals_not_displayed_row_count():
    """"Kritikus: X | Figyelmeztetés: Y" az ÖSSZESÍTETT szám akkor is,
    ha a biztonsági plafon miatt kevesebb sor látszik."""
    issues = [{"client": "X", "campaign": f"C{i}", "severity": "critical", "message": "m"}
              for i in range(20)]
    out = _format_summary(
        {"total_campaigns": 60, "critical_count": 45, "warning_count": 30,
         "alert_count": 75, "healthy_campaigns": 5, "top_issues": issues,
         "issues_truncated": 55,
         "from": "2026-06-15T00:00:00+02:00", "to": "2026-06-16T00:00:00+02:00"},
        is_weekly=False,
    )
    assert "🔴 Kritikus: 45  |  🟡 Figyelmeztetés: 30" in out
    # A csoport-fejléc is a teljes darabszámot mutatja...
    assert "🔴 **KRITIKUS: 45 db**" in out
    # ...és jelzi, hogy 25 kritikus sor nem fért ki (45 - 20 kilistázott).
    assert "és még 25 további kritikus riasztás" in out


def test_grouped_and_overall_truncation_notes_do_not_double_count():
    """A csoportszinten már jelzett rejtett sorokat a záró megjegyzés nem ismétli."""
    issues = (
        [{"client": "X", "campaign": f"C{i}", "severity": "critical", "message": "m"}
         for i in range(10)]
        + [{"client": "X", "campaign": f"W{i}", "severity": "warning", "message": "m"}
           for i in range(10)]
    )
    out = _format_summary(
        {"total_campaigns": 40, "critical_count": 10, "warning_count": 14,
         "alert_count": 24, "healthy_campaigns": 16, "top_issues": issues,
         "issues_truncated": 4,
         "from": "2026-06-15T00:00:00+02:00", "to": "2026-06-16T00:00:00+02:00"},
        is_weekly=False,
    )
    # A 4 rejtett sor mind warning → csak a csoportnál jelenik meg.
    assert "*… és még 4 további figyelmeztetés*" in out
    assert "*… és még 4 további riasztás*" not in out


def test_unknown_severity_issues_are_not_dropped_from_grouped_list():
    issues = (
        [{"client": "X", "campaign": f"C{i}", "severity": "critical", "message": "m"}
         for i in range(16)]
        + [{"client": "X", "campaign": "ODD", "severity": "", "message": "m"}]
    )
    out = _format_summary(
        {"total_campaigns": 20, "critical_count": 16, "warning_count": 0,
         "alert_count": 17, "healthy_campaigns": 3, "top_issues": issues,
         "from": "2026-06-15T00:00:00+02:00", "to": "2026-06-16T00:00:00+02:00"},
        is_weekly=False,
    )
    assert "ODD" in out


# ---------------------------------------------------------------------------
# Üzenet-darabolás (Discord 2000 karakteres limit)
# ---------------------------------------------------------------------------

def test_detected_at_is_carried_into_the_issue_rows():
    import contextlib
    frm = datetime(2026, 6, 15, 0, 0, tzinfo=TZ)
    to = datetime(2026, 6, 16, 0, 0, tzinfo=TZ)
    alerts = [_alert(10, "critical", "A", "ROAS 0.00", "2026-06-15T10:32:00+02:00")]
    with contextlib.ExitStack() as stack:
        _patch(stack, campaign_ids=[10], alerts=alerts)
        res = s._build_summary_sync(1, frm, to)

    assert res["top_issues"][0]["detected_at"] == "2026-06-15T10:32:00+02:00"


def _one_issue_summary(detected_at, **over):
    base = {
        "total_campaigns": 1, "critical_count": 1, "warning_count": 0,
        "alert_count": 1, "healthy_campaigns": 0,
        "top_issues": [{
            "client": "Stopvill", "campaign": "Sales-LEGRAND", "platform": "meta",
            "account_label": None, "severity": "critical",
            "message": "ROAS 0.00 a cél alatt", "detected_at": detected_at,
        }],
        "from": "2026-06-15T00:00:00+02:00", "to": "2026-06-16T00:00:00+02:00",
    }
    base.update(over)
    return base


def test_issue_line_ends_with_the_detection_time():
    out = _format_summary(_one_issue_summary("2026-06-15T14:32:00+02:00"), is_weekly=False)
    assert "• Stopvill [META] — Sales-LEGRAND — ROAS 0.00 a cél alatt (14:32)" in out


def test_detection_time_is_converted_from_utc_to_local():
    """A DB `timestamptz`-t tárol, a PostgREST UTC-ben adja vissza — a 12:32 UTC
    magyar nyári idő szerint 14:32. Konverzió nélkül rossz órát mutatnánk."""
    out = _format_summary(_one_issue_summary("2026-06-15T12:32:00+00:00"), is_weekly=False)
    assert "(14:32)" in out
    assert "(12:32)" not in out


def test_naive_timestamp_is_treated_as_utc():
    out = _format_summary(_one_issue_summary("2026-06-15T12:32:00"), is_weekly=False)
    assert "(14:32)" in out


def test_missing_or_broken_timestamp_does_not_break_the_line():
    for value in (None, "", "nem-idobelyeg", "2026-13-45T99:99:99"):
        out = _format_summary(_one_issue_summary(value), is_weekly=False)
        assert "Sales-LEGRAND — ROAS 0.00 a cél alatt" in out
        assert "(" not in out.split("Sales-LEGRAND")[1].split("\n")[0]


def test_weekly_multi_day_list_shows_the_date_too():
    """Hétvégi ablakban (péntek 22:00 → hétfő 08:00) a puszta óra:perc
    félrevezető lenne — péntek 14:32 és vasárnap 14:32 egyformán nézne ki."""
    issues = [
        {"client": "A", "campaign": "P", "severity": "critical", "message": "m",
         "detected_at": "2026-06-13T14:32:00+02:00"},
        {"client": "B", "campaign": "Q", "severity": "warning", "message": "m",
         "detected_at": "2026-06-14T14:32:00+02:00"},
    ]
    out = _format_summary(
        {"total_campaigns": 5, "critical_count": 1, "warning_count": 1,
         "alert_count": 2, "healthy_campaigns": 3, "top_issues": issues,
         "from": "2026-06-13T22:00:00+02:00", "to": "2026-06-15T08:00:00+02:00"},
        is_weekly=True,
    )
    assert "(06-13 14:32)" in out
    assert "(06-14 14:32)" in out


def test_single_day_list_omits_the_date():
    issues = [
        {"client": "A", "campaign": "P", "severity": "critical", "message": "m",
         "detected_at": "2026-06-15T09:05:00+02:00"},
        {"client": "B", "campaign": "Q", "severity": "warning", "message": "m",
         "detected_at": "2026-06-15T14:32:00+02:00"},
    ]
    out = _format_summary(
        {"total_campaigns": 5, "critical_count": 1, "warning_count": 1,
         "alert_count": 2, "healthy_campaigns": 3, "top_issues": issues,
         "from": "2026-06-15T00:00:00+02:00", "to": "2026-06-16T00:00:00+02:00"},
        is_weekly=False,
    )
    assert "(09:05)" in out
    assert "(14:32)" in out
    assert "06-15" not in out.split("Problémák:")[1]


def test_grouped_list_also_gets_timestamps():
    issues = [
        {"client": "A", "campaign": f"C{i}", "severity": "critical", "message": "m",
         "detected_at": f"2026-06-15T10:{i:02d}:00+02:00"}
        for i in range(20)
    ]
    out = _format_summary(
        {"total_campaigns": 30, "critical_count": 20, "warning_count": 0,
         "alert_count": 20, "healthy_campaigns": 10, "top_issues": issues,
         "from": "2026-06-15T00:00:00+02:00", "to": "2026-06-16T00:00:00+02:00"},
        is_weekly=False,
    )
    assert "🔴 **KRITIKUS: 20 db**" in out
    for i in range(20):
        assert f"C{i} — m (10:{i:02d})" in out


def test_split_message_keeps_short_content_in_one_piece():
    assert split_message("rövid") == ["rövid"]


def test_split_message_respects_limit_and_loses_nothing():
    lines = [f"• sor {i} — " + "x" * 60 for i in range(120)]
    content = "\n".join(lines)
    parts = split_message(content)

    assert len(parts) > 1
    assert all(len(p) <= 1900 for p in parts)
    # Minden sor pontosan egyszer szerepel, sorrendhelyesen.
    assert "\n".join(parts).split("\n") == lines


def test_split_message_handles_a_single_overlong_line():
    parts = split_message("y" * 5000, limit=1900)
    assert all(len(p) <= 1900 for p in parts)
    assert "".join(parts) == "y" * 5000


def test_full_summary_of_a_noisy_day_survives_the_discord_limit():
    """Végponttól végpontig: 120 riasztás → több üzenet, egyik sem lépi túl a limitet."""
    issues = [
        {"client": "Ügyfél", "campaign": f"Kampány-{i}", "platform": "meta",
         "account_label": f"act_{i}", "severity": "critical" if i % 2 else "warning",
         "message": "ROAS 0.00 a cél 3.00 alatt"}
        for i in range(120)
    ]
    out = _format_summary(
        {"total_campaigns": 200, "critical_count": 60, "warning_count": 60,
         "alert_count": 120, "healthy_campaigns": 80, "top_issues": issues,
         "from": "2026-06-15T00:00:00+02:00", "to": "2026-06-16T00:00:00+02:00"},
        is_weekly=False,
    )
    parts = split_message(out)

    assert len(out) > 1900          # egy üzenetbe tényleg nem férne
    assert all(len(p) <= 1900 for p in parts)
    assert "\n".join(parts) == out  # semmi nem veszett el
