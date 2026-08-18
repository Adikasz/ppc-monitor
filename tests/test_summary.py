"""
Napi/heti összefoglaló unit tesztek (13. lépés).

A storage réteget mockoljuk; a tiszta összesítő- és formázó-logikát teszteljük
(idő-ablakok, count-ok, healthy-számítás, top-issue rendezés, üres-eset szöveg).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from discord import app_commands

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


# ---------------------------------------------------------------------------
# A heti MUNKANAPI ablak (hétfő 00:00 → szombat 00:00)
#
# A job péntek 17:05-kor fut (scheduler.py: cron hour=17, minute=5,
# day_of_week="fri") — közvetlenül a csendes idő kezdete (17:00) után —
# a hét utolsó összefoglalója, a pénteki napi összefoglaló MELLÉ, külön
# üzenetben. Ugyanaz az elvárás, mint a `daily_range`-nél: fix naptári ablak,
# nem gördülő "utolsó 5×24 óra".
# ---------------------------------------------------------------------------

def test_workweek_range_is_monday_midnight_to_saturday_midnight():
    """Péntek 17:05-ös futás → hétfő 00:00 → szombat 00:00 (5 teljes nap)."""
    frm, to = s.workweek_range(datetime(2026, 8, 21, 17, 5, tzinfo=TZ))  # péntek

    assert frm.isoformat() == "2026-08-17T00:00:00+02:00"  # hétfő
    assert to.isoformat() == "2026-08-22T00:00:00+02:00"   # szombat
    assert frm.weekday() == 0 and to.weekday() == 5
    for boundary in (frm, to):
        assert (boundary.hour, boundary.minute, boundary.second, boundary.microsecond) == (0, 0, 0, 0)


def test_workweek_range_is_not_a_rolling_5_day_window():
    """Ugyanazon a pénteken bármikor futtatva UGYANAZ az ablak.

    Ha "az utolsó 5×24 óra" logika futna, a 17:05-ös és a reggeli lekérés
    ablaka eltérne. (A korábbi 16:00-s ütemezés is szerepel: az ütemezés
    átmozgatása NEM változtathat az ablakon.)
    """
    utemezett = s.workweek_range(datetime(2026, 8, 21, 17, 5, tzinfo=TZ))
    regi_utemezes = s.workweek_range(datetime(2026, 8, 21, 16, 0, tzinfo=TZ))
    reggel = s.workweek_range(datetime(2026, 8, 21, 9, 5, tzinfo=TZ))
    ejfel_utan = s.workweek_range(datetime(2026, 8, 21, 0, 0, 1, tzinfo=TZ))
    keso = s.workweek_range(datetime(2026, 8, 21, 23, 59, 59, tzinfo=TZ))

    assert utemezett == regi_utemezes == reggel == ejfel_utan == keso


def test_workweek_range_anchors_to_the_current_week_on_any_weekday():
    """A hét BÁRMELY napján ugyanannak a hétnek a hétfőjétől számol."""
    for nap in range(17, 24):  # hétfő (17.) … vasárnap (23.)
        frm, _ = s.workweek_range(datetime(2026, 8, nap, 12, 0, tzinfo=TZ))
        assert frm.isoformat() == "2026-08-17T00:00:00+02:00", nap


def test_workweek_range_is_the_full_week_from_friday_onwards():
    """Péntektől vasárnapig a TELJES hétfő–péntek ablak.

    Ez az ütemezett job esete (péntek 17:05) — a felső határ levágása nem
    érintheti, különben a heti jelentés elveszítené a pénteki napot.
    """
    for nap in (21, 22, 23):  # péntek, szombat, vasárnap
        frm, to = s.workweek_range(datetime(2026, 8, nap, 12, 0, tzinfo=TZ))
        assert (frm.isoformat(), to.isoformat()) == (
            "2026-08-17T00:00:00+02:00", "2026-08-22T00:00:00+02:00",
        ), nap


def test_workweek_range_stops_at_the_end_of_today_before_friday():
    """Hétfő–csütörtökön a hét még tart: az ablak csak az ELTELT napokat fedi.

    Így a kézi `/summary type:workweek` nem állít semmit olyan napokról, amikről
    még nincs adat — és a fejléc ebből a határból tudja levezetni, hogy
    részleges összefoglalót mutat (`discord_router._workweek_period_label`).
    """
    vart = {
        17: "2026-08-18T00:00:00+02:00",  # hétfőn   → kedd 00:00
        18: "2026-08-19T00:00:00+02:00",  # kedden   → szerda 00:00
        19: "2026-08-20T00:00:00+02:00",  # szerdán  → csütörtök 00:00
        20: "2026-08-21T00:00:00+02:00",  # csütörtökön → péntek 00:00
    }
    for nap, to_iso in vart.items():
        frm, to = s.workweek_range(datetime(2026, 8, nap, 12, 0, tzinfo=TZ))
        assert frm.isoformat() == "2026-08-17T00:00:00+02:00", nap
        assert to.isoformat() == to_iso, nap


def test_workweek_range_mid_week_still_covers_whole_calendar_days():
    """A levágott felső határ is éjfélre esik — nem a lekérés percére.

    Ugyanazon a szerdán reggel és este lekérve UGYANAZ az ablak; a kedd
    23:59:59.999999-kor keletkezett riasztás pedig benne van.
    """
    reggel = s.workweek_range(datetime(2026, 8, 19, 8, 0, tzinfo=TZ))
    este = s.workweek_range(datetime(2026, 8, 19, 22, 30, tzinfo=TZ))
    assert reggel == este

    frm, to = reggel
    assert (to.hour, to.minute, to.second, to.microsecond) == (0, 0, 0, 0)
    assert frm <= datetime(2026, 8, 18, 23, 59, 59, 999999, tzinfo=TZ) < to
    assert not (frm <= datetime(2026, 8, 20, 0, 0, 0, tzinfo=TZ) < to)


def test_workweek_range_covers_five_calendar_days_across_dst():
    """Óraátállítás hetében is 5 TELJES naptári nap (nem 5×24 óra).

    2026-03-29 (vasárnap) az óraátállítás — az azt KÖVETŐ hét munkanapjai már
    nyári időszámításban vannak, a hetet záró szombat 00:00 is.
    """
    # Az óraátállítás hetét záró péntek: 2026-03-27 (még téli idő).
    frm, to = s.workweek_range(datetime(2026, 3, 27, 17, 5, tzinfo=TZ))
    assert frm.isoformat() == "2026-03-23T00:00:00+01:00"
    assert to.isoformat() == "2026-03-28T00:00:00+01:00"
    assert (to.astimezone(UTC) - frm.astimezone(UTC)) == timedelta(days=5)

    # Az őszi átállítás (2026-10-25, vasárnap) UTÁNI hét: hétfőtől szombatig
    # végig téli idő, de az átállítás a hét ELŐTT volt → sima 5 nap.
    frm, to = s.workweek_range(datetime(2026, 10, 30, 17, 5, tzinfo=TZ))
    assert frm.isoformat() == "2026-10-26T00:00:00+01:00"
    assert to.isoformat() == "2026-10-31T00:00:00+01:00"
    assert (to.astimezone(UTC) - frm.astimezone(UTC)) == timedelta(days=5)


def _in_workweek_window(detected_at: datetime, now: datetime) -> bool:
    frm, to = s.workweek_range(now)
    return frm <= detected_at < to


def test_workweek_window_boundaries_are_inclusive_start_exclusive_end():
    """A hétfő eleje benne, a szombat eleje már nem — és a péntek TELJESEN benne."""
    pentek_1705 = datetime(2026, 8, 21, 17, 5, tzinfo=TZ)
    cases = [
        # (időpont,                                            benne van?)
        (datetime(2026, 8, 16, 23, 59, 59, 999999, tzinfo=TZ), False),  # előző vasárnap vége
        (datetime(2026, 8, 17, 0, 0, 0, tzinfo=TZ), True),              # hétfő 00:00 — alsó határ
        (datetime(2026, 8, 17, 23, 50, tzinfo=TZ), True),               # hétfő este
        (datetime(2026, 8, 19, 12, 0, tzinfo=TZ), True),                # szerda dél
        (datetime(2026, 8, 21, 16, 30, tzinfo=TZ), True),               # péntek, az UTOLSÓ aktív óra
        (datetime(2026, 8, 21, 17, 4, 59, tzinfo=TZ), True),            # péntek, közvetlenül a futás előtt
        (datetime(2026, 8, 21, 23, 59, 59, 999999, tzinfo=TZ), True),   # péntek utolsó pillanata
        (datetime(2026, 8, 22, 0, 0, 0, tzinfo=TZ), False),             # szombat 00:00 — felső határ
    ]
    for detected_at, expected in cases:
        assert _in_workweek_window(detected_at, pentek_1705) is expected, detected_at


def test_workweek_query_filter_is_gte_monday_and_lt_saturday():
    """A tényleges WHERE feltétel a storage rétegig lemenve."""
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

    frm, to = s.workweek_range(datetime(2026, 8, 21, 17, 5, tzinfo=TZ))
    with mock.patch.object(
        alerts_storage, "get_supabase",
        return_value=mock.Mock(table=lambda _t: _Query()),
    ), mock.patch(
        "src.storage.assignments.get_campaign_ids_for_user", return_value=[10]
    ):
        alerts_storage.get_alerts_for_user_in_range(1, frm, to)

    assert calls["gte"] == ("detected_at", "2026-08-17T00:00:00+02:00")
    assert calls["lt"] == ("detected_at", "2026-08-22T00:00:00+02:00")
    assert "lte" not in calls


def test_workweek_summary_covers_monday_to_friday_end_to_end():
    """Végponttól végpontig: a hét mind az 5 munkanapja benne, az előző hét nem."""
    import contextlib

    frm, to = s.workweek_range(datetime(2026, 8, 21, 17, 5, tzinfo=TZ))
    osszes = [
        _alert(1, "critical", "Elozo-vasarnap", "m", "2026-08-16T23:50:00+02:00"),
        _alert(2, "critical", "Hetfo", "m", "2026-08-17T00:00:00+02:00"),
        _alert(3, "warning", "Kedd", "m", "2026-08-18T10:00:00+02:00"),
        _alert(4, "warning", "Szerda", "m", "2026-08-19T10:00:00+02:00"),
        _alert(5, "warning", "Csutortok", "m", "2026-08-20T10:00:00+02:00"),
        _alert(6, "warning", "Pentek-delelott", "m", "2026-08-21T09:30:00+02:00"),
        _alert(7, "critical", "Szombat", "m", "2026-08-22T00:00:00+02:00"),
    ]
    szurt = [a for a in osszes if frm <= datetime.fromisoformat(a["detected_at"]) < to]

    with contextlib.ExitStack() as stack:
        _patch(stack, campaign_ids=list(range(1, 8)), alerts=szurt)
        res = s._build_summary_sync(1, frm, to)

    kampanyok = {i["campaign"] for i in res["top_issues"]}
    assert kampanyok == {
        "Hetfo", "Kedd", "Szerda", "Csutortok", "Pentek-delelott",
    }
    assert "Elozo-vasarnap" not in kampanyok
    assert "Szombat" not in kampanyok
    assert res["alert_count"] == 5


def test_workweek_window_does_not_overlap_the_weekend_window():
    """A munkanapi ablak szombat 00:00-kor zár, a hétvégi péntek 22:00-kor nyit —
    az átfedés tudatos: a péntek esti riasztás mindkettőben szerepel, de a
    munkanapi összefoglaló 17:05-kor megy ki, tehát akkor még nem is létezik."""
    pentek = datetime(2026, 8, 21, 17, 5, tzinfo=TZ)
    munkanapi_to = s.workweek_range(pentek)[1]
    hetvege_from = s.weekend_range(datetime(2026, 8, 24, 9, 0, tzinfo=TZ))[0]  # köv. hétfő

    assert munkanapi_to.isoformat() == "2026-08-22T00:00:00+02:00"
    assert hetvege_from.isoformat() == "2026-08-21T22:00:00+02:00"
    # A hétvégi ablak a munkanapi ablak VÉGE előtt nyit — nincs lefedetlen rés.
    assert hetvege_from < munkanapi_to


# ---------------------------------------------------------------------------
# A munkanapi összefoglaló FORMÁTUMA (megkülönböztetés a hétvégitől)
# ---------------------------------------------------------------------------

def _workweek_summary(**over):
    base = {
        "total_campaigns": 40, "critical_count": 1, "warning_count": 1,
        "alert_count": 2, "healthy_campaigns": 38,
        "top_issues": [
            {"client": "Stopvill", "campaign": "Sales", "platform": "meta",
             "account_label": None, "severity": "critical", "message": "ROAS 0.00",
             "detected_at": "2026-08-17T10:15:00+02:00"},
            {"client": "Marquard", "campaign": "Brand", "platform": "google",
             "account_label": None, "severity": "warning", "message": "CTR -34%",
             "detected_at": "2026-08-20T14:32:00+02:00"},
        ],
        "from": "2026-08-17T00:00:00+02:00", "to": "2026-08-22T00:00:00+02:00",
    }
    base.update(over)
    return base


def test_workweek_format_header_differs_from_the_weekend_one():
    out = _format_summary(_workweek_summary(), kind="workweek")

    assert "📊 **Heti összefoglaló — munkanapok (hétfő–péntek)**" in out
    assert "Hétvégi összefoglaló" not in out
    assert "Problémák a héten:" in out
    assert "Alertek a héten:** 2" in out


def test_workweek_format_header_shows_friday_not_saturday():
    """A felső határ EXKLUZÍV szombat 00:00 — a fejlécben az utolsó BENNE lévő
    nap (péntek) dátuma szerepeljen, különben szombatot írnánk ki."""
    out = _format_summary(_workweek_summary(), kind="workweek")

    assert "2026-08-17 → 2026-08-21" in out   # hétfő → péntek
    assert "2026-08-22" not in out            # szombat sehol


def test_workweek_format_keeps_timestamps_and_grouping():
    """Ugyanaz a rendering, mint a többi nézetben: időbélyeg + csoportosítás."""
    # Rövid lista → időbélyegek, több napot átfog → dátummal.
    out = _format_summary(_workweek_summary(), kind="workweek")
    assert "(08-17 10:15)" in out
    assert "(08-20 14:32)" in out

    # Hosszú lista → súlyosság szerinti csoportosítás.
    sok = [
        {"client": "A", "campaign": f"C{i}", "platform": "meta", "account_label": None,
         "severity": "critical", "message": "m",
         "detected_at": f"2026-08-18T10:{i:02d}:00+02:00"}
        for i in range(20)
    ]
    out = _format_summary(
        _workweek_summary(top_issues=sok, critical_count=20, warning_count=0, alert_count=20),
        kind="workweek",
    )
    assert "🔴 **KRITIKUS: 20 db**" in out
    for i in range(20):
        assert f"C{i}" in out


def test_workweek_format_no_alerts_branch():
    out = _format_summary(
        _workweek_summary(critical_count=0, warning_count=0, alert_count=0, top_issues=[]),
        kind="workweek",
    )
    assert out.startswith("✅ **Heti összefoglaló — munkanapok (hétfő–péntek)**")
    assert "A héten nem volt anomália" in out
    assert "2026-08-17 → 2026-08-21" in out


def test_workweek_format_header_flags_a_partial_week():
    """Hét közben kézzel lekérve a fejléc az UTOLSÓ MEGLÉVŐ napig szól.

    Szerdán a `workweek_range` felső határa csütörtök 00:00 → a fejléc
    "hétfő–szerda", és kimondja, hogy a hét még nem zárult le. Enélkül a
    részleges adat teljes hetinek látszana.
    """
    out = _format_summary(
        _workweek_summary(to="2026-08-20T00:00:00+02:00"),  # szerdai lekérés
        kind="workweek",
    )

    assert "📊 **Heti összefoglaló — munkanapok (hétfő–szerda, részleges — " \
           "a hét még nem zárult le)**" in out
    assert "2026-08-17 → 2026-08-19" in out   # hétfő → szerda
    assert "hétfő–péntek" not in out


def test_workweek_format_partial_label_follows_the_actual_window():
    """Minden hét közbeni nap a saját napnevét kapja — a fejléc az ablakot követi."""
    vart = {
        "2026-08-18T00:00:00+02:00": "hétfő–hétfő",       # hétfői lekérés
        "2026-08-19T00:00:00+02:00": "hétfő–kedd",
        "2026-08-20T00:00:00+02:00": "hétfő–szerda",
        "2026-08-21T00:00:00+02:00": "hétfő–csütörtök",
        "2026-08-22T00:00:00+02:00": "hétfő–péntek",      # teljes hét (pénteken)
    }
    for to_iso, label in vart.items():
        out = _format_summary(_workweek_summary(to=to_iso), kind="workweek")
        assert f"munkanapok ({label}" in out, to_iso
        # A "részleges" jelzés PONTOSAN a nem teljes heteken jelenjen meg.
        assert ("részleges" in out) is (label != "hétfő–péntek"), to_iso


def test_workweek_format_partial_no_alerts_branch_does_not_claim_the_whole_week():
    """Anomália nélkül sem állíthatjuk, hogy "a héten" nem volt gond, ha a hét tart."""
    out = _format_summary(
        _workweek_summary(
            to="2026-08-20T00:00:00+02:00",
            critical_count=0, warning_count=0, alert_count=0, top_issues=[],
        ),
        kind="workweek",
    )

    assert out.startswith("✅ **Heti összefoglaló — munkanapok (hétfő–szerda, részleges")
    assert "Eddig a héten nem volt anomália" in out
    assert "A héten nem volt anomália" not in out


def test_workweek_format_falls_back_to_the_full_week_on_a_broken_upper_bound():
    """Hiányzó/hibás felső határnál NEM bélyegzünk részlegesnek egy teljes hetet."""
    for rossz in (None, "", "nem-datum"):
        out = _format_summary(_workweek_summary(to=rossz), kind="workweek")
        assert "munkanapok (hétfő–péntek)" in out, rossz
        assert "részleges" not in out, rossz


def test_scheduled_friday_run_still_reports_the_full_week_end_to_end():
    """A pénteki job ablaka → fejléc: a lánc egésze a TELJES hetet adja.

    Ez a regresszió-védelem a felső határ levágására: ha az a pénteki futásra
    is lecsapna, itt "részleges" jelenne meg.
    """
    frm, to = s.workweek_range(datetime(2026, 8, 21, 17, 5, tzinfo=TZ))  # péntek
    out = _format_summary(
        _workweek_summary(**{"from": frm.isoformat(), "to": to.isoformat()}),
        kind="workweek",
    )

    assert "munkanapok (hétfő–péntek)" in out
    assert "részleges" not in out
    assert "2026-08-17 → 2026-08-21" in out


def test_format_summary_kind_defaults_stay_backwards_compatible():
    """`kind` nélkül az `is_weekly` dönt — a régi hívások nem törnek el."""
    napi = _format_summary(_workweek_summary(), is_weekly=False)
    hetvegi = _format_summary(_workweek_summary(), is_weekly=True)

    assert "Napi összefoglaló" in napi
    assert "Hétvégi összefoglaló" in hetvegi


# ---------------------------------------------------------------------------
# Ütemezés: a munkanapi job regisztrálása és a kiküldés
# ---------------------------------------------------------------------------

def _registered_jobs():
    """A `start_scheduler` által regisztrált jobok: {id: (függvény, kwargs)}."""
    from types import SimpleNamespace

    from src.monitoring import scheduler as sched

    jobs: dict[str, tuple] = {}
    fake = mock.Mock()
    fake.add_job.side_effect = lambda func, **kw: jobs.__setitem__(kw.get("id"), (func, kw))

    with mock.patch.object(sched, "AsyncIOScheduler", return_value=fake), \
         mock.patch.object(
             sched, "get_config",
             return_value=SimpleNamespace(timezone="Europe/Budapest"),
         ):
        sched._scheduler = None
        try:
            sched.start_scheduler()
        finally:
            sched._scheduler = None
    return jobs


def test_workweek_job_runs_on_friday_at_1705():
    """Péntek 17:05 — a munkanap vége, üzleti döntés (lásd a scheduler kommentjét).

    FIGYELEM: ez NEM esik egybe a csendes idő kezdetével — a `.env.example`
    szerint `QUIET_HOURS_START=18`, tehát a 17:05–18:00 sáv még aktív. Az ebben
    a sávban keletkező riasztásokról valós időben megy értesítés, de a heti
    munkanapi összefoglalóba már nem kerülnek bele. Ez tudatosan vállalt.
    """
    from src.monitoring import scheduler as sched

    jobs = _registered_jobs()

    assert "workweek_summary" in jobs
    func, kw = jobs["workweek_summary"]
    assert func is sched.workweek_summary_job
    assert (kw["hour"], kw["minute"], kw["day_of_week"]) == (17, 5, "fri")


def test_workweek_job_does_not_replace_the_friday_daily_summary():
    """A pénteki napi összefoglaló VÁLTOZATLANUL kimegy 09:00-kor — a munkanapi
    összefoglaló MELLÉ jön, külön üzenetben (külön job, külön id)."""
    jobs = _registered_jobs()

    _, napi = jobs["daily_summary"]
    assert (napi["hour"], napi["minute"], napi["day_of_week"]) == (9, 0, "tue-fri")

    _, hetvegi = jobs["weekly_summary"]
    assert (hetvegi["hour"], hetvegi["minute"], hetvegi["day_of_week"]) == (9, 0, "mon")

    # Három külön job, három külön id — egyik sem írja felül a másikat.
    assert len({"daily_summary", "weekly_summary", "workweek_summary"} & set(jobs)) == 3


@pytest.mark.asyncio
async def test_workweek_job_sends_the_workweek_kind_to_every_user():
    """A job a munkanapi generátort hívja, és `kind="workweek"`-kel küld —
    ez viszi tovább a megkülönböztetett fejlécet a formázóig."""
    from src.monitoring import scheduler as sched

    users = [{"id": 1}, {"id": 2}]
    generated: list[int] = []
    sent: list[tuple] = []

    async def _fake_generate(uid):
        generated.append(uid)
        return {"alert_count": 0, "total_campaigns": 0, "critical_count": 0,
                "warning_count": 0, "healthy_campaigns": 0, "top_issues": []}

    async def _fake_send(user, summary, *, is_weekly=False, kind=None):
        sent.append((user["id"], kind))
        return {"channel_id": 1, "message_id": 1}

    with mock.patch.object(sched.users_storage, "list_users", return_value=users), \
         mock.patch.dict(
             sched.SUMMARY_KINDS,
             {"workweek": (_fake_generate, "Heti munkanapi")},
         ), \
         mock.patch.object(sched, "send_summary_to_user", new=_fake_send):
        await sched.workweek_summary_job()

    assert generated == [1, 2]
    assert sent == [(1, "workweek"), (2, "workweek")]


# ---------------------------------------------------------------------------
# `/summary` és `/my summary` — a manuális parancsok
# (a workweek péntekig való várakozás nélkül is tesztelhető)
# ---------------------------------------------------------------------------

def _type_choices(command) -> list[str]:
    """Egy app command `type:` paraméterének választható értékei."""
    params = {p.name: p for p in command.parameters}
    return [c.value for c in params["type"].choices]


def test_both_summary_commands_offer_the_workweek_type():
    from src.bot.commands import alerts as alerts_cmd
    from src.bot.commands import my_commands as my_cmd

    assert _type_choices(alerts_cmd.SummaryCog.summary) == ["daily", "weekly", "workweek"]
    assert _type_choices(my_cmd.MyCommandsCog.summary) == ["daily", "weekly", "workweek"]


def test_command_choices_match_the_supported_types():
    """A Discord `choices` és a leképezés-tábla ne csússzon szét: minden
    felkínált érték feloldható legyen."""
    from src.bot.commands import alerts as alerts_cmd
    from src.bot.commands import my_commands as my_cmd

    for command in (alerts_cmd.SummaryCog.summary, my_cmd.MyCommandsCog.summary):
        assert set(_type_choices(command)) == set(s.SUMMARY_TYPE_TO_KIND)


def test_resolve_summary_type_maps_to_the_right_generator_and_kind():
    """A `type` értéke és a formázó `kind`-ja nem mindenhol azonos: a
    felhasználói "weekly" a hétvégi ("weekend") formátumra megy."""
    assert s.resolve_summary_type("daily")[:2] == (s.generate_daily_summary, "daily")
    assert s.resolve_summary_type("weekly")[:2] == (s.generate_weekly_summary, "weekend")
    assert s.resolve_summary_type("workweek")[:2] == (
        s.generate_workweek_summary, "workweek",
    )

    # Hiányzó/ismeretlen érték → napi (védőháló, a choices amúgy is korlátoz).
    assert s.resolve_summary_type(None)[:2] == (s.generate_daily_summary, "daily")
    assert s.resolve_summary_type("nincs-ilyen")[:2] == (s.generate_daily_summary, "daily")


def test_both_commands_and_the_scheduler_share_one_generator_table():
    """A `/summary`, a `/my summary` és az ütemezett job UGYANAZT a generátort
    hívja fajtánként — különben a kézi "tesztelés" mást bizonyítana, mint ami
    pénteken ténylegesen kimegy.

    A `scheduler.SUMMARY_KINDS` és a parancsok feloldása is a
    `summary.SUMMARY_KINDS` egyetlen forrásából jön, ezt rögzítjük itt.
    """
    from src.monitoring import scheduler as sched

    # A scheduler ugyanazt a táblaobjektumot használja (nem másolatot).
    assert sched.SUMMARY_KINDS is s.SUMMARY_KINDS

    # A parancsok `type:` értékei ugyanoda oldódnak fel, mint a job `kind`-jai.
    parancs_szerint = {
        kind: gen
        for gen, kind, _ in (
            s.resolve_summary_type(t) for t in s.SUMMARY_TYPE_TO_KIND
        )
    }
    utemezett = {kind: gen for kind, (gen, _) in s.SUMMARY_KINDS.items()}
    assert parancs_szerint == utemezett


@pytest.mark.asyncio
async def test_my_summary_workweek_calls_the_same_generator_as_the_scheduler_job():
    """A `/my summary type:workweek` a munkanapi generátort hívja a HÍVÓ user
    id-jával, és `kind="workweek"`-kel küld — ugyanaz, mint a `/summary
    type:workweek` és a pénteki job, csak az OM saját kampányaira szűkítve
    (a szűkítést a generátor user_id paramétere adja, nem külön logika).
    """
    from src.bot.commands import my_commands as my_cmd

    hivott: list[tuple] = []
    kuldott: list[tuple] = []

    async def _fake_generate(uid):
        hivott.append(("generate", uid))
        return {"alert_count": 0, "total_campaigns": 0, "critical_count": 0,
                "warning_count": 0, "healthy_campaigns": 0, "top_issues": []}

    async def _fake_send(user, summary, *, is_weekly=False, kind=None):
        kuldott.append((user["id"], kind))
        return {"channel_id": 1, "message_id": 1}

    interaction = mock.Mock()
    interaction.response.defer = mock.AsyncMock()
    interaction.followup.send = mock.AsyncMock()

    cog = my_cmd.MyCommandsCog.__new__(my_cmd.MyCommandsCog)
    valasz = app_commands.Choice(name="workweek", value="workweek")

    with mock.patch.object(
        my_cmd.MyCommandsCog, "_owner_or_reject",
        new=mock.AsyncMock(return_value={"id": 42}),
    ), mock.patch.dict(
        s.SUMMARY_KINDS, {"workweek": (_fake_generate, "Heti munkanapi")},
    ), mock.patch.object(
        my_cmd.discord_router, "send_summary_to_user", new=_fake_send,
    ):
        await my_cmd.MyCommandsCog.summary.callback(cog, interaction, type=valasz)

    # A HÍVÓ user id-jával futott (ez szűkíti a saját fiókokra), és a
    # munkanapi formátummal ment ki.
    assert hivott == [("generate", 42)]
    assert kuldott == [(42, "workweek")]


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
