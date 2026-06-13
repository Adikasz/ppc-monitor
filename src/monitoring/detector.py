"""
Anomália-detektor — rules engine.

Egy kampány aktuális metrikáit (insights) összeveti a kampány KPI-jaival és
lifecycle-állapotával, majd visszaadja a detektált anomáliákat. NEM ír DB-be
és NEM küld riasztást — csak a szabály-kiértékelés. A perzisztálást az
`src.storage.alerts.insert_alert`, az ütemezést az `src.monitoring.scheduler`
végzi.

Belépő:
    anomalies = await detect_anomalies_for_campaign(campaign_id, insights)

Bemeneti `insights` (a 9. lépésben a Meta/Google metrics adja; addig stub):
    {
        "impressions": int, "clicks": int, "spend": float,
        "conversions": float, "conversion_value": float,
        "ctr": float, "cpc": float, "cpa": float, "roas": float,
        # opcionális, ha a metrics réteg számolja (no-conversion szabályhoz):
        "days_without_conversion": int,
    }

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
        "metric":          str,        # pl. "cpa_spike", "budget_depleted"
        "observed_value":  float,
        "threshold_value": float,
        "message":         str,        # emberi olvasható
    }, ...]

Megjegyzés a KPI-lekérésről:
    A `kpis.get_active_kpis()` jelenleg a nem létező valid_to/valid_from
    oszlopokra hivatkozik (DB-séma eltérés), ezért ITT saját, a valós sémához
    (is_active + created_at) igazodó lekérést használunk (_get_active_kpis).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any

from src.storage import campaigns as campaigns_storage
from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

log = get_logger(__name__)

# Lifecycle állapotok, amelyekre egyáltalán nem jelzünk
_SKIP_LIFECYCLE = {"paused", "ended"}
# Lifecycle állapotok, ahol csak CRITICAL megy (tanulási fázis)
_CRITICAL_ONLY_LIFECYCLE = {"new", "learning"}

# KPI default-ok, ha a campaign_kpis sorban az adott mező NULL
_DEFAULT_NO_CONVERSION_DAYS = 3
_DEFAULT_CPA_SPIKE_PCT = 50.0
# Büdzsé-figyelmeztetés aránya (90%); a 100% már CRITICAL
_BUDGET_WARNING_RATIO = 0.9


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

    # 2) Aktív KPI-k (lehet None, ha még nincs beállítva — a szabályok kezelik)
    kpis = await asyncio.to_thread(_get_active_kpis, campaign_id) or {}

    only_critical = lifecycle in _CRITICAL_ONLY_LIFECYCLE

    # 3) Szabály-kiértékelés (tiszta, szinkron logika)
    anomalies: list[dict[str, Any]] = []
    anomalies.extend(_check_critical(campaign_id, insights, kpis))
    if not only_critical:
        anomalies.extend(_check_warning(campaign_id, insights, kpis))

    if anomalies:
        log.info(
            "Detektor: #%s (%s) — %d anomália: %s",
            campaign_id, lifecycle, len(anomalies), [a["metric"] for a in anomalies],
        )
    return anomalies


# ---------------------------------------------------------------------------
# Szabály-kategóriák
# ---------------------------------------------------------------------------

def _check_critical(
    campaign_id: int,
    insights: dict[str, Any],
    kpis: dict[str, Any],
) -> list[dict[str, Any]]:
    """CRITICAL szabályok (minden monitorozott lifecycle-re)."""
    out: list[dict[str, Any]] = []

    impressions = _num(insights, "impressions")
    spend = _num(insights, "spend")
    conversions = _num(insights, "conversions")
    cpa = _num(insights, "cpa")
    days_no_conv = _num(insights, "days_without_conversion")

    max_cpa = _num(kpis, "max_cpa")
    monthly_budget = _num(kpis, "monthly_budget")

    # 1) Leálltak a hirdetések: 0 megjelenés, de van költés
    if impressions is not None and spend is not None and impressions == 0 and spend > 0:
        out.append(_mk(
            campaign_id, "critical", "ads_stopped", spend, 0.0,
            f"Leálltak a hirdetések: {_fmt(spend)} Ft költés, 0 megjelenés",
        ))

    # 2) X nap óta nincs konverzió (csak ha a metrics réteg adja a napszámot)
    threshold_days = _int(kpis.get("no_conversion_critical_days"), _DEFAULT_NO_CONVERSION_DAYS)
    if (
        conversions is not None and conversions == 0
        and days_no_conv is not None and days_no_conv >= threshold_days
    ):
        out.append(_mk(
            campaign_id, "critical", "no_conversion", days_no_conv, float(threshold_days),
            f"{int(days_no_conv)} nap óta nincs konverzió (küszöb: {threshold_days} nap)",
        ))

    # 3) CPA spike: cpa > max_cpa * (1 + spike%/100)
    if cpa is not None and max_cpa is not None and max_cpa > 0:
        pct = _num(kpis, "cpa_spike_critical_pct")
        pct = pct if pct is not None else _DEFAULT_CPA_SPIKE_PCT
        spike_threshold = max_cpa * (1 + pct / 100)
        if cpa > spike_threshold:
            over_pct = round((cpa / max_cpa - 1) * 100)
            out.append(_mk(
                campaign_id, "critical", "cpa_spike", cpa, max_cpa,
                f"CPA +{over_pct}% ({_fmt(cpa)} Ft vs cél {_fmt(max_cpa)} Ft)",
            ))

    # 4) Büdzsé 100%-ban elfogyott
    if spend is not None and monthly_budget is not None and monthly_budget > 0 and spend >= monthly_budget:
        out.append(_mk(
            campaign_id, "critical", "budget_depleted", spend, monthly_budget,
            f"Büdzsé 100%-ban elfogyott ({_fmt(spend)} / {_fmt(monthly_budget)} Ft)",
        ))

    return out


def _check_warning(
    campaign_id: int,
    insights: dict[str, Any],
    kpis: dict[str, Any],
) -> list[dict[str, Any]]:
    """WARNING szabályok (csak mature lifecycle-re)."""
    out: list[dict[str, Any]] = []

    spend = _num(insights, "spend")
    monthly_budget = _num(kpis, "monthly_budget")

    # Büdzsé 90%-on (de még nem 100% — azt a CRITICAL viszi)
    if spend is not None and monthly_budget is not None and monthly_budget > 0:
        warn_threshold = monthly_budget * _BUDGET_WARNING_RATIO
        if warn_threshold <= spend < monthly_budget:
            out.append(_mk(
                campaign_id, "warning", "budget_warning", spend, warn_threshold,
                f"Büdzsé 90%-on ({_fmt(spend)} / {_fmt(monthly_budget)} Ft)",
            ))

    # Trend-alapú WARNING-ok (ctr↓, cpc↑, roas↓): metrika-history kell hozzá,
    # ami a napi metrics tárolásával (9. lépés+) lesz elérhető. Akkor ide kerül.

    return out


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


def _get_active_kpis(campaign_id: int) -> dict[str, Any] | None:
    """Aktuális KPI-sor a valós séma szerint (is_active + created_at desc).

    Szándékosan NEM a kpis.get_active_kpis()-t hívja, mert az a nem létező
    valid_to oszlopra hivatkozik (lásd modul-docstring).
    """
    res = (
        get_supabase()
        .table("campaign_kpis")
        .select("*")
        .eq("campaign_id", campaign_id)
        .eq("is_active", True)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None
