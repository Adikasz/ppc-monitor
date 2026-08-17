"""
Detektor — küszöb-alapú metrikák (CTR / CPM / Frequency / Impressions) unit tesztek.

A `_evaluate_rules` tiszta (nincs DB), így a 0011-es új szabályokat izoláltan
ellenőrizzük. A `warning_pct=20`, `critical_pct=40` küszöböket explicit adjuk.
"""
from __future__ import annotations

from src.monitoring import detector


def _eff(**kw):
    base = {"warning_pct": 20.0, "critical_pct": 40.0}
    base.update(kw)
    return base


def _metrics(out, metric):
    return [a for a in out if a["metric"] == metric]


# --- CTR drop (min_ctr) -----------------------------------------------------

def test_ctr_drop_critical():
    # ctr 1% (arány 0.01) a 2% cél alatt → −50% ≤ −40 → critical
    out = detector._evaluate_rules(1, {"ctr": 0.01, "impressions": 5000}, _eff(min_ctr=2.0), False)
    hits = _metrics(out, "ctr_drop")
    assert len(hits) == 1 and hits[0]["severity"] == "critical"
    assert "CTR 1.00%" in hits[0]["message"]


def test_ctr_drop_warning():
    # ctr 1.7% a 2% cél alatt → −15% → nincs (köszöb 20%). 1.55% → −22.5% → warning
    out = detector._evaluate_rules(1, {"ctr": 0.0155, "impressions": 5000}, _eff(min_ctr=2.0), False)
    hits = _metrics(out, "ctr_drop")
    assert len(hits) == 1 and hits[0]["severity"] == "warning"


def test_ctr_drop_within_threshold_no_alert():
    # ctr 1.9% a 2% alatt, de csak −5% → nincs riasztás
    out = detector._evaluate_rules(1, {"ctr": 0.019, "impressions": 5000}, _eff(min_ctr=2.0), False)
    assert _metrics(out, "ctr_drop") == []


def test_ctr_drop_not_configured():
    out = detector._evaluate_rules(1, {"ctr": 0.0001, "impressions": 5000}, _eff(), False)
    assert _metrics(out, "ctr_drop") == []


# --- CPM spike (max_cpm) ----------------------------------------------------

def test_cpm_spike_critical():
    # spend 700 / impressions 1000 → cpm 700, a max 500 felett → +40% → critical
    out = detector._evaluate_rules(1, {"impressions": 1000, "spend": 700, "conversions": 1}, _eff(max_cpm=500), False)
    hits = _metrics(out, "cpm_spike")
    assert len(hits) == 1 and hits[0]["severity"] == "critical"
    assert "CPM 700 Ft" in hits[0]["message"]


def test_cpm_spike_within_threshold_no_alert():
    # cpm 550 a max 500 felett, de csak +10% → nincs
    out = detector._evaluate_rules(1, {"impressions": 1000, "spend": 550, "conversions": 1}, _eff(max_cpm=500), False)
    assert _metrics(out, "cpm_spike") == []


# --- Frequency spike (max_frequency) ----------------------------------------

def test_frequency_spike_warning():
    # frequency 3.7 a max 3.0 felett → +23% → warning (nem éri el a +40-et)
    out = detector._evaluate_rules(1, {"impressions": 1000, "frequency": 3.7}, _eff(max_frequency=3.0), False)
    hits = _metrics(out, "frequency_spike")
    assert len(hits) == 1 and hits[0]["severity"] == "warning"
    assert "telítettség" in hits[0]["message"]


def test_frequency_spike_only_critical_suppresses_warning():
    out = detector._evaluate_rules(1, {"impressions": 1000, "frequency": 3.7}, _eff(max_frequency=3.0), True)
    assert _metrics(out, "frequency_spike") == []


def test_frequency_missing_no_alert():
    # Google-nál nincs frequency → a szabály nem tüzel
    out = detector._evaluate_rules(1, {"impressions": 1000}, _eff(max_frequency=3.0), False)
    assert _metrics(out, "frequency_spike") == []


# --- Impressions drop (min_impressions) -------------------------------------

def test_impressions_drop_critical():
    out = detector._evaluate_rules(1, {"impressions": 500, "conversions": 1}, _eff(min_impressions=1000), False)
    hits = _metrics(out, "impressions_drop")
    assert len(hits) == 1 and hits[0]["severity"] == "critical"
    assert "1,000" in hits[0]["message"] and "500" in hits[0]["message"]


# --- Minimum-adat kapu az arány-metrikákhoz (issue #5) ----------------------
#
# A nap elején (02:00-s ciklus) még alig van aznapi adat: 0 megjelenés mellett a
# CTR és a ROAS is 0.00 → a detektor CRITICAL-nak látná. A napi dedup miatt ez
# EGÉSZ NAPRA elnyomná a később keletkező VALÓDI riasztást ugyanarra a
# kampány+metrika párra, ezért az arány-alapú szabályok csak elég adat mellett
# futnak.

_NULLA_ADAT = {"roas": 0.0, "ctr": 0.0, "cpa": 0.0, "cpm": 0.0, "frequency": 0.0}
_KPIK = {
    "target_roas": 5.0, "target_ctr": 1.0, "max_cpa": 4000.0,
    "min_ctr": 1.0, "max_cpm": 2000.0, "max_frequency": 3.0,
}


def test_no_data_at_all_produces_no_ratio_alerts():
    """A pontosan az éles kimenetben látott eset: 0 megjelenés → CTR/ROAS 0.00."""
    out = detector._evaluate_rules(
        1, {**_NULLA_ADAT, "impressions": 0, "spend": 0.0}, _eff(**_KPIK), False,
    )
    assert out == [], f"nulla adatnál nem lehet arány-riasztás, kaptunk: {out}"


def test_few_impressions_produce_no_ratio_alerts():
    """Néhány megjelenés még nem elég — a 0.00-s arányok itt is adathiányt jelentenek."""
    out = detector._evaluate_rules(
        1,
        {**_NULLA_ADAT, "impressions": detector._MIN_IMPRESSIONS_FOR_RATIOS - 1,
         "spend": 500.0},
        _eff(**_KPIK),
        False,
    )
    assert {a["metric"] for a in out} == set()


def test_enough_impressions_still_alert_normally():
    """A kapu csak az adathiányt szűri — valós adatnál a szabályok futnak."""
    out = detector._evaluate_rules(
        1,
        {"impressions": 5_000, "ctr": 0.001, "roas": 0.5, "spend": 5_000.0},
        _eff(target_roas=5.0, min_ctr=1.0),
        False,
    )
    metrikak = {a["metric"] for a in out}
    assert "roas_drop" in metrikak
    assert "ctr_drop" in metrikak


def test_boundary_exactly_at_the_threshold_is_enough():
    """A küszöb INKLUZÍV: pontosan annyi megjelenés már elég."""
    kozos = {"ctr": 0.001, "roas": 0.5, "spend": 5_000.0}
    kapun_belul = detector._evaluate_rules(
        1, {**kozos, "impressions": detector._MIN_IMPRESSIONS_FOR_RATIOS},
        _eff(target_roas=5.0), False,
    )
    kapun_kivul = detector._evaluate_rules(
        1, {**kozos, "impressions": detector._MIN_IMPRESSIONS_FOR_RATIOS - 1},
        _eff(target_roas=5.0), False,
    )
    assert _metrics(kapun_belul, "roas_drop") != []
    assert _metrics(kapun_kivul, "roas_drop") == []


def test_ads_stopped_still_fires_without_impressions():
    """A kapu NEM némíthatja el az `ads_stopped`-ot: 0 megjelenés + van költés
    épp a valódi kritikus eset (leálltak a hirdetések, de fogy a pénz)."""
    out = detector._evaluate_rules(
        1, {"impressions": 0, "spend": 12_000.0, **_NULLA_ADAT}, _eff(**_KPIK), False,
    )
    hits = _metrics(out, "ads_stopped")
    assert len(hits) == 1 and hits[0]["severity"] == "critical"


def test_budget_and_no_conversion_rules_are_not_gated():
    """A nem arány-alapú szabályok adathiány mellett is futnak."""
    out = detector._evaluate_rules(
        1,
        {"impressions": 0, "spend": 100_000.0, "conversions": 0,
         "days_without_conversion": 5},
        _eff(monthly_budget=50_000.0, no_conversion_critical_days=3),
        False,
    )
    metrikak = {a["metric"] for a in out}
    assert "budget_depleted" in metrikak
    assert "no_conversion" in metrikak


def test_impressions_drop_is_not_gated_by_the_ratio_threshold():
    """Az `impressions_drop` magát a darabszámot nézi — ha a megjelenés-küszöb
    elnémítaná, pont a legfontosabb esetét veszítenénk el (alig van megjelenés).
    """
    out = detector._evaluate_rules(
        1,
        {"impressions": detector._MIN_IMPRESSIONS_FOR_RATIOS - 1, "conversions": 1},
        _eff(min_impressions=10_000),
        False,
    )
    hits = _metrics(out, "impressions_drop")
    assert len(hits) == 1 and hits[0]["severity"] == "critical"
