"""
Anomália-detektor — rules engine.

Egy kampány aktuális metrikáit (insights) összeveti a kampány KPI-jaival és
lifecycle-állapotával, majd visszaadja a detektált anomáliákat. NEM ír DB-be
és NEM küld riasztást — csak a szabály-kiértékelés. A perzisztálást az
`src.storage.alerts.insert_alert`, az ütemezést az `src.monitoring.scheduler`
végzi.

Belépő:
    anomalies = await detect_anomalies_for_campaign(campaign_id, insights)

KPI-öröklés (15. lépés):
    A "hatékony" (effective) KPI mezőnként:
        campaign_kpis aktív érték  →  client_kpis érték  →  default
    azaz a per-kampány érték felülírja a kliens-szintűt, és ha egyik sincs,
    a beépített default lép életbe (warning=20%, critical=40%).

Irány-érzékeny küszöbök (15. lépés):
    - "magasabb a jobb" (ROAS, ROI, CTR):
        WARNING  ha az érték < cél * (1 − warning_pct/100)
        CRITICAL ha az érték < cél * (1 − critical_pct/100)
    - "alacsonyabb a jobb" (CPA, CPL, CPC):
        WARNING  ha az érték > cél * (1 + warning_pct/100)
        CRITICAL ha az érték > cél * (1 + critical_pct/100)
    - büdzsé: WARNING 90%, CRITICAL 100%.

Mértékegységek (fontos a helyes összevetéshez):
    - target_roas a ROAS szorzó (pl. 3.0 = 3×), az insights `roas` is szorzó → direkt.
    - target_ctr százalék (pl. 3.5 = 3.5%); az insights `ctr` arány (0.035) → ×100.
    - CPA/CPL/CPC forint → direkt.
    - ROI: az insightsban jellemzően nincs `roi` mező, így az ROI-szabály csak
      akkor aktiválódik, ha a metrics réteg külön szolgáltatja.

Szűrés (lifecycle + mute + data_valid_from):
    - paused / ended            → nem jelez (skip)
    - new / learning            → CSAK CRITICAL (tanulási fázis, kevesebb zaj)
    - mature                    → teljes monitoring (CRITICAL + WARNING)
    - aktív mute (mutes tábla)  → skip
    - data_valid_from a jövőben → skip (még nincs érvényes mérés)

Visszatérés (lista, üres ha nincs anomália):
    [{
        "campaign_id":     int,
        "severity":        "critical" | "warning",
        "metric":          str,        # pl. "cpa_spike", "roas_drop", "budget_depleted"
        "observed_value":  float,
        "threshold_value": float,
        "message":         str,        # emberi olvasható
    }, ...]
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any

from src.storage import ad_account_kpis as ad_account_kpis_storage
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import client_kpis as client_kpis_storage
from src.storage import kpis as kpis_storage
from src.storage import campaigns as campaigns_storage
from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

log = get_logger(__name__)

# Lifecycle állapotok, amelyekre egyáltalán nem jelzünk
_SKIP_LIFECYCLE = {"paused", "ended"}
# Lifecycle állapotok, ahol csak CRITICAL megy (tanulási fázis)
_CRITICAL_ONLY_LIFECYCLE = {"new", "learning"}

# KPI default-ok
_DEFAULT_NO_CONVERSION_DAYS = 3
_DEFAULT_WARNING_PCT = 20.0
_DEFAULT_CRITICAL_PCT = 40.0
# Büdzsé-figyelmeztetés aránya (90%); a 100% már CRITICAL
_BUDGET_WARNING_RATIO = 0.9

# A campaign_kpis aktív sor + client_kpis közül „mergelt" mezők (öröklés).
_KPI_FIELDS = (
    "target_roas", "target_roi", "target_ctr",
    "max_cpa", "max_cpl", "max_cpc",
    "monthly_budget", "primary_conversion_event",
    "warning_pct", "critical_pct",
    "no_conversion_critical_days",
)


async def detect_anomalies_for_campaign(
    campaign_id: int,
    insights: dict[str, Any],
) -> list[dict[str, Any]]:
    """Egy kampány anomáliáinak detektálása (lásd modul-docstring)."""
    # 1) Kampány + alap-szűrés. A DB hívások to_thread-ben futnak, hogy ne
    #    blokkolják a bot/scheduler event loopját.
    campaign = await asyncio.to_thread(campaigns_storage.get_campaign, campaign_id)
    if campaign is None:
        log.warning("Detektor: nincs ilyen kampány: #%s", campaign_id)
        return []

    lifecycle = (campaign.get("lifecycle_state") or "new").lower()
    if lifecycle in _SKIP_LIFECYCLE:
        return []

    # Aktív némítás → skip
    if await asyncio.to_thread(_is_muted, campaign_id):
        log.debug("Detektor: #%s némítva — skip", campaign_id)
        return []

    # data_valid_from a jövőben → még nincs érvényes mérés
    if _before_data_valid_from(campaign):
        log.debug("Detektor: #%s data_valid_from előtt — skip", campaign_id)
        return []

    # 2) Hatékony KPI-k: campaign_kpis → ad_account_kpis → client_kpis öröklés.
    campaign_kpis = await asyncio.to_thread(kpis_storage.get_active_kpis, campaign_id) or {}
    account_kpis = await asyncio.to_thread(_account_kpis_for_campaign, campaign) or {}
    client_kpis = await asyncio.to_thread(_client_kpis_for_campaign, campaign) or {}
    effective = _merge_kpis(campaign_kpis, account_kpis, client_kpis)

    only_critical = lifecycle in _CRITICAL_ONLY_LIFECYCLE

    # 3) Szabály-kiértékelés (tiszta, szinkron, DB-mentes logika → tesztelhető).
    anomalies = _evaluate_rules(campaign_id, insights, effective, only_critical)

    if anomalies:
        log.info(
            "Detektor: #%s (%s) — %d anomália: %s",
            campaign_id, lifecycle, len(anomalies), [a["metric"] for a in anomalies],
        )
    return anomalies


# ---------------------------------------------------------------------------
# KPI öröklés (campaign → ad_account → client → default) — 21. lépés
# ---------------------------------------------------------------------------

def _account_kpis_for_campaign(campaign: dict[str, Any]) -> dict[str, Any] | None:
    """A kampány fiókjának KPI-sora (ad_account_kpis). None ha nincs / nincs tábla."""
    ad_account_id = campaign.get("ad_account_id")
    if not ad_account_id:
        return None
    return ad_account_kpis_storage.get_ad_account_kpis(ad_account_id)


def _client_kpis_for_campaign(campaign: dict[str, Any]) -> dict[str, Any] | None:
    """A kampány kliensének KPI-sora (campaign → ad_account → client → client_kpis).

    Másodlagos fallback a FIÓK-szintű KPI mögött. A `campaigns` táblában NINCS
    közvetlen client_id, ezért az ad_account-on keresztül oldjuk fel. None, ha
    nincs (vagy ha a client_kpis tábla nem létezik — a storage elnyeli).
    """
    ad_account_id = campaign.get("ad_account_id")
    if not ad_account_id:
        return None
    account = ad_accounts_storage.get_ad_account(ad_account_id)
    if not account:
        return None
    return client_kpis_storage.get_client_kpis(account["client_id"])


def _merge_kpis(
    campaign_kpis: dict[str, Any],
    account_kpis: dict[str, Any],
    client_kpis: dict[str, Any],
) -> dict[str, Any]:
    """Mezőnkénti öröklés: campaign → ad_account → client (az első nem-None nyer)."""
    effective: dict[str, Any] = {}
    for field in _KPI_FIELDS:
        value = None
        for source in (campaign_kpis, account_kpis, client_kpis):
            v = source.get(field)
            if v is not None:
                value = v
                break
        effective[field] = value
    return effective


# ---------------------------------------------------------------------------
# Szabály-kiértékelés (PURE — nincs DB hívás, így önállóan tesztelhető)
# ---------------------------------------------------------------------------

def _evaluate_rules(
    campaign_id: int,
    insights: dict[str, Any],
    eff: dict[str, Any],
    only_critical: bool,
) -> list[dict[str, Any]]:
    """A szabályok kiértékelése a hatékony KPI-k alapján."""
    out: list[dict[str, Any]] = []

    warning_pct = _num(eff, "warning_pct")
    warning_pct = warning_pct if warning_pct is not None else _DEFAULT_WARNING_PCT
    critical_pct = _num(eff, "critical_pct")
    critical_pct = critical_pct if critical_pct is not None else _DEFAULT_CRITICAL_PCT

    impressions = _num(insights, "impressions")
    spend = _num(insights, "spend")
    conversions = _num(insights, "conversions")
    days_no_conv = _num(insights, "days_without_conversion")
    monthly_budget = _num(eff, "monthly_budget")

    # 1) Leálltak a hirdetések: 0 megjelenés, de van költés (CRITICAL)
    if impressions is not None and spend is not None and impressions == 0 and spend > 0:
        out.append(_mk(
            campaign_id, "critical", "ads_stopped", spend, 0.0,
            f"Leálltak a hirdetések: {_fmt(spend)} Ft költés, 0 megjelenés",
        ))

    # 2) X nap óta nincs konverzió (CRITICAL)
    threshold_days = _int(eff.get("no_conversion_critical_days"), _DEFAULT_NO_CONVERSION_DAYS)
    if (
        conversions is not None and conversions == 0
        and days_no_conv is not None and days_no_conv >= threshold_days
    ):
        out.append(_mk(
            campaign_id, "critical", "no_conversion", days_no_conv, float(threshold_days),
            f"{int(days_no_conv)} nap óta nincs konverzió (küszöb: {threshold_days} nap)",
        ))

    # 3) "Magasabb a jobb" metrikák (ROAS, CTR, ROI)
    for obs_key, target_key, metric, label, scale, decimals, unit in (
        ("roas", "target_roas", "roas_drop", "ROAS", 1.0, 2, ""),
        ("ctr", "target_ctr", "ctr_low", "CTR", 100.0, 2, "%"),
        ("roi", "target_roi", "roi_low", "ROI", 1.0, 2, ""),
    ):
        anomaly = _higher_is_better(
            campaign_id, insights, eff, obs_key, target_key, metric, label,
            scale, decimals, unit, warning_pct, critical_pct, only_critical,
        )
        if anomaly:
            out.append(anomaly)

    # 4) "Alacsonyabb a jobb" metrikák (CPA, CPL, CPC) — forint
    for obs_key, target_key, metric, label in (
        ("cpa", "max_cpa", "cpa_spike", "CPA"),
        ("cpl", "max_cpl", "cpl_high", "CPL"),
        ("cpc", "max_cpc", "cpc_high", "CPC"),
    ):
        anomaly = _lower_is_better(
            campaign_id, insights, eff, obs_key, target_key, metric, label,
            warning_pct, critical_pct, only_critical,
        )
        if anomaly:
            out.append(anomaly)

    # 5) Büdzsé (fix 90% / 100% küszöb)
    if spend is not None and monthly_budget is not None and monthly_budget > 0:
        if spend >= monthly_budget:
            out.append(_mk(
                campaign_id, "critical", "budget_depleted", spend, monthly_budget,
                f"Büdzsé 100%-ban elfogyott ({_fmt(spend)} / {_fmt(monthly_budget)} Ft)",
            ))
        elif not only_critical and spend >= monthly_budget * _BUDGET_WARNING_RATIO:
            warn_th = monthly_budget * _BUDGET_WARNING_RATIO
            out.append(_mk(
                campaign_id, "warning", "budget_warning", spend, warn_th,
                f"Büdzsé 90%-on ({_fmt(spend)} / {_fmt(monthly_budget)} Ft)",
            ))

    return out


def _higher_is_better(
    campaign_id: int,
    insights: dict[str, Any],
    eff: dict[str, Any],
    obs_key: str,
    target_key: str,
    metric: str,
    label: str,
    scale: float,
    decimals: int,
    unit: str,
    warning_pct: float,
    critical_pct: float,
    only_critical: bool,
) -> dict[str, Any] | None:
    """ROAS/ROI/CTR jellegű: a cél ALATT WARNING/CRITICAL."""
    obs = _num(insights, obs_key)
    target = _num(eff, target_key)
    if obs is None or target is None or target <= 0:
        return None

    value = obs * scale
    crit_th = target * (1 - critical_pct / 100)
    warn_th = target * (1 - warning_pct / 100)

    if value < crit_th:
        return _mk(
            campaign_id, "critical", metric, value, crit_th,
            f"{label} {_fmtn(value, decimals)}{unit} a cél {_fmtn(target, decimals)}{unit} alatt "
            f"(−{_pct(critical_pct)}% kritikus küszöb: {_fmtn(crit_th, decimals)}{unit})",
        )
    if not only_critical and value < warn_th:
        return _mk(
            campaign_id, "warning", metric, value, warn_th,
            f"{label} {_fmtn(value, decimals)}{unit} a cél {_fmtn(target, decimals)}{unit} alatt "
            f"(−{_pct(warning_pct)}% warning küszöb: {_fmtn(warn_th, decimals)}{unit})",
        )
    return None


def _lower_is_better(
    campaign_id: int,
    insights: dict[str, Any],
    eff: dict[str, Any],
    obs_key: str,
    target_key: str,
    metric: str,
    label: str,
    warning_pct: float,
    critical_pct: float,
    only_critical: bool,
) -> dict[str, Any] | None:
    """CPA/CPL/CPC jellegű (forint): a cél FÖLÖTT WARNING/CRITICAL."""
    obs = _num(insights, obs_key)
    target = _num(eff, target_key)
    if obs is None or target is None or target <= 0:
        return None

    crit_th = target * (1 + critical_pct / 100)
    warn_th = target * (1 + warning_pct / 100)

    if obs > crit_th:
        return _mk(
            campaign_id, "critical", metric, obs, crit_th,
            f"{label} {_fmt(obs)} Ft a cél {_fmt(target)} Ft felett "
            f"(+{_pct(critical_pct)}% kritikus küszöb: {_fmt(crit_th)} Ft)",
        )
    if not only_critical and obs > warn_th:
        return _mk(
            campaign_id, "warning", metric, obs, warn_th,
            f"{label} {_fmt(obs)} Ft a cél {_fmt(target)} Ft felett "
            f"(+{_pct(warning_pct)}% warning küszöb: {_fmt(warn_th)} Ft)",
        )
    return None


# ---------------------------------------------------------------------------
# Belső segédfüggvények
# ---------------------------------------------------------------------------

def _mk(
    campaign_id: int,
    severity: str,
    metric: str,
    observed_value: float,
    threshold_value: float,
    message: str,
) -> dict[str, Any]:
    """Egységes anomália-dict összeállítása."""
    return {
        "campaign_id": campaign_id,
        "severity": severity,
        "metric": metric,
        "observed_value": observed_value,
        "threshold_value": threshold_value,
        "message": message,
    }


def _num(d: dict[str, Any], key: str) -> float | None:
    """Mező float-ként, vagy None ha hiányzik / nem szám."""
    v = d.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _fmt(v: float | None) -> str:
    """Szám emberi formázása: egész → tizedesek nélkül."""
    if v is None:
        return "—"
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}"


def _fmtn(v: float, decimals: int) -> str:
    """Formázás adott tizedesjeggyel (decimals=0 → egész-barát _fmt)."""
    if decimals <= 0:
        return _fmt(v)
    return f"{v:.{decimals}f}"


def _pct(x: float) -> str:
    """Százalék formázása: egész → tizedes nélkül."""
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


def _is_muted(campaign_id: int) -> bool:
    """Van-e aktív, le nem járt némítás erre a kampányra (mutes tábla)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    res = (
        get_supabase()
        .table("mutes")
        .select("id")
        .eq("campaign_id", campaign_id)
        .eq("is_active", True)
        .gt("muted_until", now_iso)
        .limit(1)
        .execute()
    )
    return bool(res.data)


def _before_data_valid_from(campaign: dict[str, Any]) -> bool:
    """True, ha a kampány data_valid_from dátuma a jövőben van (még nincs adat)."""
    dvf = campaign.get("data_valid_from")
    if not dvf:
        return False
    try:
        valid_from = date.fromisoformat(str(dvf)[:10])
    except ValueError:
        return False
    return date.today() < valid_from
