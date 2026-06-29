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
