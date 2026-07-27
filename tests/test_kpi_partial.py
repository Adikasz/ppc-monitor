"""
KPI parciális update + detektor NULL-kihagyás (FELADAT 2).

Két elvárás:
  1) A detektor egy KPI mezőt CSAK akkor értékel ki, ha az nem None. Ha csak
     target_roas van beállítva, egyedül a roas_drop-ot vizsgálja — a CPA/CPM/CTR/
     frequency/impressions/budget szabályok NEM tüzelnek, hiába „rossz" a metrika.
  2) Az `upsert_ad_account_kpis` csak a ténylegesen megadott (non-None) mezőket
     írja az UPDATE-be — a meg nem adott mezők érintetlenek maradnak (nem NULL-ra).
"""
from __future__ import annotations

from src.monitoring import detector
from src.storage import ad_account_kpis as ak


# ---------------------------------------------------------------------------
# 1) Detektor: csak a beállított (non-None) KPI-mezőket értékeli ki
# ---------------------------------------------------------------------------

def _eff(**kw):
    # Minden KPI mező None kivéve amit átadunk — warning/critical default marad None,
    # a detektor a 20/40 default-ra esik vissza a pct-eknél.
    base = {
        "target_roas": None, "target_roi": None, "target_ctr": None,
        "max_cpa": None, "max_cpl": None, "max_cpc": None,
        "monthly_budget": None, "primary_conversion_event": None,
        "warning_pct": None, "critical_pct": None,
        "no_conversion_critical_days": None,
        "min_ctr": None, "max_cpm": None, "max_frequency": None, "min_impressions": None,
    }
    base.update(kw)
    return base


def test_only_roas_configured_only_roas_evaluated():
    # Csak target_roas van megadva. Az insights MINDEN más metrikában „rossz",
    # de mivel a többi KPI None → egyedül a roas_drop tüzelhet.
    insights = {
        "roas": 1.0,            # cél 3.0 alatt → roas_drop
        "cpa": 999_999,         # ha max_cpa lenne, cpa_spike lenne — de None
        "cpc": 999_999,
        "cpl": 999_999,
        "ctr": 0.0001,          # ha min_ctr lenne, ctr_drop
        "impressions": 1,       # ha min_impressions lenne, impressions_drop
        "spend": 999_999,       # ha monthly_budget lenne, budget_depleted
        "frequency": 99.0,      # ha max_frequency lenne, frequency_spike
        "conversions": 1,
    }
    out = detector._evaluate_rules(1, insights, _eff(target_roas=3.0), False)
    metrics = {a["metric"] for a in out}
    assert metrics == {"roas_drop"}, f"csak roas_drop várt, kaptunk: {metrics}"


def test_no_kpi_configured_no_anomalies():
    # Egyetlen KPI mező sincs beállítva → semmilyen küszöb-szabály nem tüzel.
    insights = {
        "roas": 0.1, "cpa": 999_999, "ctr": 0.0001, "impressions": 1,
        "spend": 999_999, "frequency": 99.0, "conversions": 1,
    }
    out = detector._evaluate_rules(1, insights, _eff(), False)
    # A „leálltak a hirdetések" (ads_stopped) és „nincs konverzió" nem KPI-függő —
    # itt impressions>0 és conversions>0, így egyik sem tüzel.
    assert out == [], f"nem várt anomáliák: {[a['metric'] for a in out]}"


def test_cpm_configured_still_fires():
    # Kontroll: ha a max_cpm BE van állítva, a cpm_spike tüzel (a guard nem néma).
    out = detector._evaluate_rules(
        1, {"impressions": 1000, "spend": 700, "conversions": 1}, _eff(max_cpm=500), False
    )
    assert {a["metric"] for a in out} == {"cpm_spike"}


# ---------------------------------------------------------------------------
# 2) upsert_ad_account_kpis — csak a megadott mezőket írja az UPDATE-be
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, data):
        self.data = data


class _FakeUpdate:
    def __init__(self, sink):
        self._sink = sink

    def eq(self, *_a):
        return self

    def execute(self):
        return _FakeResp([{"id": 1, **self._sink["fields"]}])


class _FakeSelect:
    def __init__(self, existing):
        self._existing = existing

    def eq(self, *_a):
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        return _FakeResp([{"id": 1}] if self._existing else [])


class _FakeTable:
    def __init__(self, existing, sink):
        self._existing = existing
        self._sink = sink

    def select(self, *_a):
        return _FakeSelect(self._existing)

    def update(self, fields):
        self._sink["fields"] = fields
        return _FakeUpdate(self._sink)

    def insert(self, payload):
        self._sink["fields"] = payload
        return _FakeUpdate(self._sink)


class _FakeSupabase:
    def __init__(self, existing, sink):
        self._existing = existing
        self._sink = sink

    def table(self, _name):
        return _FakeTable(self._existing, self._sink)


def test_partial_update_only_sets_given_fields(monkeypatch):
    # Meglévő sor van; csak max_cpm-et adunk meg → az UPDATE payload CSAK max_cpm.
    sink: dict = {}
    monkeypatch.setattr(ak, "get_supabase", lambda: _FakeSupabase(existing=True, sink=sink))
    ak.upsert_ad_account_kpis(1, fields={"max_cpm": 500})
    assert sink["fields"] == {"max_cpm": 500}
    assert "target_roas" not in sink["fields"]  # a meglévő target_roas érintetlen
