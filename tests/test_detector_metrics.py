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


# ---------------------------------------------------------------------------
# A HÁROM KÜLÖN KAPU
#
# Az éles adat mutatta meg, hogy egyetlen megjelenés-küszöb nem elég:
#   - CTR-nél 100 megjelenés kevés (0.99^100 = 36,6% esély a véletlen 0
#     kattintásra egy egészséges, 1%-os kampányon)
#   - ROAS/CPA-nál a megjelenés ROSSZ MÉRCE: reggel 6-kor 300 megjelenés
#     mellett is természetes, hogy a konverziók még nem estek be
# ---------------------------------------------------------------------------

def test_ctr_gate_needs_far_more_impressions_than_the_generic_one():
    """A CTR-kapu szigorúbb — különben a véletlen 0 kattintás CRITICAL lenne."""
    assert detector._MIN_IMPRESSIONS_FOR_CTR > detector._MIN_IMPRESSIONS_FOR_RATIOS


def test_ctr_not_evaluated_below_its_own_threshold():
    """Az élesben látott eset: pár száz megjelenés, 0 kattintás → NEM riasztás.

    300 megjelenésnél egy egészséges 1%-os kampányon is ~5% az esélye a csupa
    véletlen 0 kattintásnak — ebből nem lehet CRITICAL-t csinálni.
    """
    out = detector._evaluate_rules(
        1,
        {"impressions": 300, "ctr": 0.0, "spend": 50_000.0, "conversions": 10},
        _eff(min_ctr=1.0, target_ctr=1.0),
        False,
    )
    assert _metrics(out, "ctr_drop") == []
    assert _metrics(out, "ctr_low") == []


def test_ctr_gate_boundary_is_inclusive():
    """A CTR-küszöb INKLUZÍV: pontosan annyi megjelenés már elég."""
    kozos = {"ctr": 0.001, "spend": 50_000.0, "conversions": 10}
    belul = detector._evaluate_rules(
        1, {**kozos, "impressions": detector._MIN_IMPRESSIONS_FOR_CTR},
        _eff(min_ctr=1.0), False,
    )
    kivul = detector._evaluate_rules(
        1, {**kozos, "impressions": detector._MIN_IMPRESSIONS_FOR_CTR - 1},
        _eff(min_ctr=1.0), False,
    )
    assert _metrics(belul, "ctr_drop") != []
    assert _metrics(kivul, "ctr_drop") == []


def test_roas_is_gated_by_spend_not_by_impressions():
    """A pontosan az élesben látott 06:00-s eset: sok megjelenés, alig költés.

    A ROAS 0.00 itt nem teljesítmény-gond, hanem az, hogy a konverziók még nem
    estek be. Megjelenésből akármennyi lehet — a kapu a KÖLTÉST nézi.
    """
    sok_megjelenes_keves_koltes = {
        "impressions": 50_000, "roas": 0.0, "spend": 900.0, "conversions": 0,
    }
    out = detector._evaluate_rules(
        1, sok_megjelenes_keves_koltes, _eff(target_roas=5.0, max_cpa=4_000.0), False,
    )
    assert _metrics(out, "roas_drop") == []

    # Ugyanaz a kampány a nap folyamán, egy konverziónyi költés fölött → riaszt.
    kesobb = {**sok_megjelenes_keves_koltes, "spend": 4_000.0}
    out = detector._evaluate_rules(
        1, kesobb, _eff(target_roas=5.0, max_cpa=4_000.0), False,
    )
    assert _metrics(out, "roas_drop") != []


def test_conversion_gate_self_calibrates_from_the_campaign_kpi():
    """A küszöb a kampány SAJÁT max_cpa-ja — nem egy globális szám.

    Két kampány, ugyanaz a költés: amelyiknek drágább a konverziója, annál még
    nem várható eredmény, tehát nem riasztunk.
    """
    insights = {"impressions": 50_000, "roas": 0.0, "spend": 5_000.0, "conversions": 0}

    olcso_konverzio = detector._evaluate_rules(
        1, insights, _eff(target_roas=5.0, max_cpa=2_000.0), False,
    )
    draga_konverzio = detector._evaluate_rules(
        1, insights, _eff(target_roas=5.0, max_cpa=20_000.0), False,
    )

    assert _metrics(olcso_konverzio, "roas_drop") != []   # 5000 Ft > 2000 Ft
    assert _metrics(draga_konverzio, "roas_drop") == []   # 5000 Ft < 20 000 Ft


def test_conversion_gate_uses_cpl_when_there_is_no_cpa():
    """Lead-gen kampányon a max_cpl adja a konverzió költség-skáláját."""
    insights = {"impressions": 50_000, "roas": 0.0, "spend": 3_000.0, "conversions": 0}
    out = detector._evaluate_rules(
        1, insights, _eff(target_roas=5.0, max_cpl=10_000.0), False,
    )
    assert _metrics(out, "roas_drop") == []


def test_conversion_gate_falls_back_to_the_default_without_kpi():
    """KPI nélküli kampányon is véd — csak fix összeggel, nem önhangolón."""
    kozos = {"impressions": 50_000, "roas": 0.0, "conversions": 0}
    kevés = detector._evaluate_rules(
        1, {**kozos, "spend": detector._DEFAULT_CONVERSION_COST - 1},
        _eff(target_roas=5.0), False,
    )
    elég = detector._evaluate_rules(
        1, {**kozos, "spend": detector._DEFAULT_CONVERSION_COST},
        _eff(target_roas=5.0), False,
    )
    assert _metrics(kevés, "roas_drop") == []
    assert _metrics(elég, "roas_drop") != []


def test_cpa_is_gated_by_spend_too():
    """A CPA is konverzió-alapú: egy konverziónyi költés alatt nem ítélünk."""
    kozos = {"impressions": 50_000, "cpa": 9_000.0, "conversions": 1}
    korán = detector._evaluate_rules(
        1, {**kozos, "spend": 1_000.0}, _eff(max_cpa=4_000.0), False,
    )
    később = detector._evaluate_rules(
        1, {**kozos, "spend": 9_000.0}, _eff(max_cpa=4_000.0), False,
    )
    assert _metrics(korán, "cpa_spike") == []
    assert _metrics(később, "cpa_spike") != []


def test_impression_based_metrics_keep_the_generic_threshold():
    """A CPM és a frequency közvetlenül megjelenésből számol — ott a 100 marad,
    és NEM függ a költés-kaputól (itt a költés messze a konverzió-ár alatt van).
    """
    # A CPM-et a detektor maga számolja (spend / impressions * 1000), ezért a
    # költést adjuk meg: 500 Ft / 100 megjelenés → 5 000 Ft-os CPM, a 2 000-es
    # max fölött. Ugyanez az 500 Ft messze a 4 000 Ft-os konverzió-ár alatt van,
    # tehát a költés-kapu ZÁRVA — mégis riasztania kell.
    kozos = {"frequency": 9.0, "spend": 500.0, "conversions": 0}
    belul = detector._evaluate_rules(
        1, {**kozos, "impressions": detector._MIN_IMPRESSIONS_FOR_RATIOS},
        _eff(max_cpm=2_000.0, max_frequency=3.0, max_cpa=4_000.0), False,
    )
    kivul = detector._evaluate_rules(
        1, {**kozos, "impressions": detector._MIN_IMPRESSIONS_FOR_RATIOS - 1},
        _eff(max_cpm=2_000.0, max_frequency=3.0, max_cpa=4_000.0), False,
    )
    assert {a["metric"] for a in belul} == {"cpm_spike", "frequency_spike"}
    assert kivul == []


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
