"""
Napi alert-dedup: EGY üzenet naponta, de a tárolt ÉRTÉK frissül.

Az éles adat mutatta meg a problémát: a nap első mérése ragadt bent egész
napra. A 02:00-kor mért "CPA 13 620 Ft" maradt az összefoglalóban akkor is,
amikor 15:00-ra 5 732 Ft-ra állt be — pedig az utóbbi a nap valós képe.

Az elvárás kettős, és a kettő könnyen ütközik:
  1. a Discord-üzenet naponta EGYSZER menjen ki (nincs óránkénti spam)
  2. az összefoglalóban a LEGFRISSEBB mérés szerepeljen
"""
from __future__ import annotations

from unittest import mock

from src.storage import alerts as alerts_storage


class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    """Minimális PostgREST-utánzat: rögzíti, mi történt a táblával."""

    def __init__(self, sink, existing):
        self._sink = sink
        self._existing = existing

    # -- select ág (dedup-ellenőrzés) --
    def select(self, *_a):
        self._sink["selected"] = True
        return self

    def limit(self, *_a):
        return self

    def eq(self, field, value):
        self._sink.setdefault("eq", []).append((field, value))
        return self

    # -- írás ágak --
    def insert(self, payload):
        self._sink["inserted"] = payload
        return self

    def update(self, payload):
        self._sink["updated"] = payload
        return self

    def execute(self):
        if "inserted" in self._sink or "updated" in self._sink:
            return _Resp([{"id": 42}])
        return _Resp([{"id": 42}] if self._existing else [])


def _run(*, existing: bool, **over):
    sink: dict = {}
    kw = {
        "campaign_id": 7,
        "severity": "critical",
        "metric": "cpa_spike",
        "observed_value": 5_732.23,
        "threshold_value": 4_000.0,
        "message": "CPA 5732.23 Ft a cél 4000 Ft felett",
    }
    kw.update(over)
    with mock.patch.object(
        alerts_storage, "get_supabase",
        return_value=mock.Mock(table=lambda _t: _Query(sink, existing)),
    ):
        result = alerts_storage.insert_alert(**kw)
    return result, sink


def test_first_alert_of_the_day_is_inserted_and_returned():
    """Új riasztás → beszúrás, és a hívó megkapja a sort (ebből lesz üzenet)."""
    result, sink = _run(existing=False)

    assert result is not None
    assert "inserted" in sink
    assert "updated" not in sink
    assert sink["inserted"]["observed_value"] == 5_732.23


def test_duplicate_refreshes_the_stored_measurement():
    """Ma már volt ilyen → a tárolt érték a FRISS mérésre cserélődik."""
    _, sink = _run(existing=True)

    assert "updated" in sink, "a duplikátumnak frissítenie kell a sort"
    assert "inserted" not in sink
    frissitett = sink["updated"]
    assert frissitett["observed_value"] == 5_732.23
    assert frissitett["threshold_value"] == 4_000.0
    assert frissitett["message"] == "CPA 5732.23 Ft a cél 4000 Ft felett"
    assert frissitett["severity"] == "critical"
    # A megjelenített SZÁMHOZ tartozó időpontnak kell látszania, nem a nap
    # első méréséének — különben az összefoglaló 15:00-s értéket mutatna
    # 02:00-s időbélyeggel.
    assert "detected_at" in frissitett


def test_duplicate_does_not_send_a_second_discord_message():
    """A frissítés None-t ad vissza → a router NEM küld újabb üzenetet.

    Ez a kettős elvárás lényege: az érték frissül, de naponta egy ping megy.
    """
    result, _ = _run(existing=True)
    assert result is None


def test_refresh_does_not_touch_the_routing_state():
    """A kiküldés-státuszt nem írjuk felül — az alert egyszer ment ki."""
    _, sink = _run(existing=True)

    for tilos in ("status", "sent_at", "discord_message_id", "routed_to_discord_user_id"):
        assert tilos not in sink["updated"], f"a frissítés nem nyúlhat ehhez: {tilos}"


def test_refresh_targets_the_existing_row_by_id():
    """A frissítés a MEGTALÁLT sorra megy (id=42), nem vaktában a dedup-kulcsra."""
    _, sink = _run(existing=True)
    assert ("id", 42) in sink["eq"]


def test_severity_escalation_is_stored():
    """Ha a helyzet romlik (warning → critical), a tárolt súlyosság követi."""
    _, sink = _run(existing=True, severity="critical")
    assert sink["updated"]["severity"] == "critical"


def test_a_failed_refresh_does_not_break_the_monitoring_cycle():
    """DB-hiba a frissítéskor nem buktathatja el az egész ciklust.

    Ilyenkor a korábbi (pontatlanabb) érték marad — rosszabb, de nem végzetes.
    """
    def _robban(_name):
        raise RuntimeError("DB elérhetetlen")

    with mock.patch.object(
        alerts_storage, "get_supabase",
        side_effect=[
            mock.Mock(table=lambda _t: _Query({}, True)),   # a dedup-select még megy
            mock.Mock(table=_robban),                        # az update elszáll
        ],
    ):
        # Nem dobhat — a hívó (scheduler) így folytatja a többi kampánnyal.
        assert alerts_storage.insert_alert(
            7, "critical", "cpa_spike", 1.0, 2.0, "üzenet",
        ) is None


def test_recent_insight_metrics_is_importable_and_runs():
    """Regresszió: a `timedelta` importja hiányzott, így ez NameError-t dobott.

    Két éles hívója van (`insight_engine` és `ai_insights`), tehát minden
    insight-ciklus elszállt rajta.
    """
    class _Q:
        def select(self, *_a):
            return self

        def eq(self, *_a):
            return self

        def gt(self, *_a):
            return self

        def execute(self):
            return _Resp([{"metric": "scaling_opportunity"}])

    with mock.patch.object(
        alerts_storage, "get_supabase",
        return_value=mock.Mock(table=lambda _t: _Q()),
    ):
        assert alerts_storage.recent_insight_metrics(7, days=7) == {"scaling_opportunity"}
