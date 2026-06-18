"""
INSIGHT motor — szabály-alapú optimalizálási LEHETŐSÉGEK (18. lépés).

A WARNING/CRITICAL anomáliákkal szemben az insight nem baj, hanem lehetőség:
skálázás, alulköltés, büdzsé-átcsoportosítás, kedvező aukció, funnel-probléma,
plató, kreatív fáradás. A motor egy kampány elmúlt ~7 napi metrikáiból és a
hatékony (öröklött) KPI-jaiból állít elő insight-javaslatokat.

A kiértékelés MAGJA tiszta, szinkron, DB-mentes (`evaluate_insight_rules`), így
offline tesztelhető. A `detect_insights_for_campaign` ezt futtatja, majd a 7
napos deduplikációval (alerts tábla) kiszűri a közelmúltban már kiküldött
metrikákat.

A campaign_insights ÓRÁNKÉNTI, napra halmozott — ezért a motor előbb napi
idősorrá redukálja (`to_daily_series`). A trend-szabályok (CPC/CTR/impressions)
és a szórás-alapú plató ezen a napi idősoron dolgoznak.

Visszatérés (lista, üres ha nincs lehetőség) — ugyanaz a formátum, mint az
anomáliáknál:
    [{
        "campaign_id": int,
        "severity": "insight",
        "metric": str,                 # pl. "scaling_opportunity"
        "observed_value": float | None,
        "threshold_value": float | None,
        "message": str,                # 🔵 emberi olvasható (magyar)
    }, ...]
"""
from __future__ import annotations

import asyncio
import calendar
import math
import statistics
from datetime import date
from typing import Any

from src.storage import alerts as alerts_storage
from src.storage.insights_history import to_daily_series
from src.utils.logging import get_logger

log = get_logger(__name__)

# 7 napos dedup ablak (kampányonként + metrikánként ne spammeljen).
_DEDUP_DAYS = 7
# Büdzsé-átcsoportosításhoz ennyi kampány ROAS-a kell, hogy a „top 20%" értelmes legyen.
_MIN_PEERS_FOR_REALLOC = 5


# ---------------------------------------------------------------------------
# Publikus belépők
# ---------------------------------------------------------------------------

async def detect_insights_for_campaign(
    campaign: dict[str, Any],
    insights_history: list[dict[str, Any]],
    kpi: dict[str, Any],
    peer_campaigns: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Egy kampány szabály-alapú insightjai, 7 napos deduplikációval.

    1) Tiszta szabály-kiértékelés (`evaluate_insight_rules`).
    2) 7 napos dedup: a közelmúltban (alerts, severity='insight') már kiküldött
       metrikákat kiszűri. DB hívás `to_thread`-ben (event loop védelme).
    """
    rules = evaluate_insight_rules(campaign, insights_history, kpi, peer_campaigns, today=today)
    if not rules:
        return []

    campaign_id = campaign.get("id")
    if campaign_id is None:
        return rules

    recent = await asyncio.to_thread(
        alerts_storage.recent_insight_metrics, campaign_id, days=_DEDUP_DAYS
    )
    fresh = [r for r in rules if r["metric"] not in recent]
    if len(fresh) != len(rules):
        log.debug(
            "Insight dedup #%s: %d/%d kihagyva (7 napon belül már volt)",
            campaign_id, len(rules) - len(fresh), len(rules),
        )
    return fresh


def evaluate_insight_rules(
    campaign: dict[str, Any],
    insights_history: list[dict[str, Any]],
    kpi: dict[str, Any],
    peer_campaigns: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """A 7 insight-szabály tiszta kiértékelése (nincs DB hívás → tesztelhető)."""
    today = today or date.today()
    daily = to_daily_series(insights_history)
    if not daily:
        return []

    campaign_id = campaign.get("id")
    platform = _platform(campaign)
    cur = daily[-1]

    # Aktuális (legutolsó napi) metrikák
    roas = _f(cur, "roas")
    ctr_ratio = _f(cur, "ctr")
    ctr_pct = ctr_ratio * 100 if ctr_ratio is not None else None
    spend_cur = _f(cur, "spend")
    conversions = _f(cur, "conversions")

    # Hatékony KPI-k
    target_roas = _f(kpi, "target_roas")
    target_ctr = _f(kpi, "target_ctr")          # százalék
    max_cpa = _f(kpi, "max_cpa")
    monthly_budget = _f(kpi, "monthly_budget")

    # Hó eleje óta becsült költés: átlagos napi költés × eltelt napok.
    # (Csak 7 napi history van; ez egyenletes pacing-feltevés melletti becslés.)
    daily_spends = [v for v in (_f(d, "spend") for d in daily) if v is not None]
    avg_daily_spend = statistics.fmean(daily_spends) if daily_spends else None
    day_of_month = today.day
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    mtd_spend = avg_daily_spend * day_of_month if avg_daily_spend is not None else None

    out: list[dict[str, Any]] = []

    # 1) SKÁLÁZÁSI LEHETŐSÉG — kiváló ROAS + alacsony büdzsé-kihasználtság
    if (
        roas is not None and target_roas and target_roas > 0
        and monthly_budget and monthly_budget > 0 and mtd_spend is not None
        and roas > target_roas * 1.3 and mtd_spend < monthly_budget * 0.8
    ):
        pct = mtd_spend / monthly_budget * 100
        out.append(_mk(
            campaign_id, "scaling_opportunity", roas, target_roas,
            f"📈 Skálázási lehetőség: ROAS {roas:.1f}x (target {target_roas:.1f}x), "
            f"büdzsé csak {pct:.0f}%-ban kihasználva — emelj keretet",
        ))

    # 2) ALULKÖLTÉS — hónap >60%-ánál, alacsony költés, de jó ROAS
    if (
        day_of_month > 18
        and monthly_budget and monthly_budget > 0 and mtd_spend is not None
        and roas is not None and target_roas
        and mtd_spend < monthly_budget * 0.7 and roas > target_roas
    ):
        day_pct = day_of_month / days_in_month * 100
        spend_pct = mtd_spend / monthly_budget * 100
        out.append(_mk(
            campaign_id, "underspend", mtd_spend, monthly_budget,
            f"💰 Alulköltés: hónap {day_pct:.0f}%-ánál csak {spend_pct:.0f}% elköltve, "
            f"de ROAS jó ({roas:.1f}x) — növeld a napi limitet",
        ))

    # 3) BÜDZSÉ ÁTCSOPORTOSÍTÁS — top 20% ROAS a fiókon belül + van alulteljesítő
    realloc = _budget_reallocation(campaign_id, roas, target_roas, peer_campaigns)
    if realloc:
        out.append(realloc)

    # 4) KEDVEZŐ AUKCIÓ — utolsó 3 nap CPC le, impressions fel
    if len(daily) >= 3:
        cpc3 = [_f(d, "cpc") for d in daily[-3:]]
        imp3 = [_f(d, "impressions") for d in daily[-3:]]
        if all(v is not None for v in cpc3) and all(v is not None for v in imp3):
            if _trend_down(cpc3) and _trend_up(imp3):
                old, new = cpc3[0], cpc3[-1]
                out.append(_mk(
                    campaign_id, "favorable_auction", new, old,
                    f"🎯 Kedvező aukció: CPC {old:.0f}→{new:.0f} Ft, impressions nőtt "
                    f"— jó pillanat bid emelésre",
                ))

    # 5) FUNNEL PROBLÉMA — jó CTR, de gyenge konverzió (várt = költés / max_cpa)
    if (
        ctr_pct is not None and target_ctr and target_ctr > 0
        and conversions is not None and spend_cur and spend_cur > 0
        and max_cpa and max_cpa > 0
    ):
        expected = spend_cur / max_cpa
        if ctr_pct > target_ctr * 1.2 and conversions < expected * 0.5:
            out.append(_mk(
                campaign_id, "funnel_issue", ctr_pct, target_ctr,
                f"🔍 Funnel probléma: CTR jó ({ctr_pct:.1f}%) de konverzió alacsony "
                f"— landing page vagy ajánlat vizsgálata javasolt",
            ))

    # 6) PLATÓ DETEKTÁLÁS — stabil ROAS (CV < 5%), de target alatt
    roas_series = [v for v in (_f(d, "roas") for d in daily) if v is not None]
    if len(roas_series) >= 4 and target_roas and target_roas > 0:
        mean_roas = statistics.fmean(roas_series)
        if mean_roas > 0:
            cv = statistics.pstdev(roas_series) / mean_roas
            if cv < 0.05 and mean_roas < target_roas * 0.9:
                out.append(_mk(
                    campaign_id, "performance_plateau", mean_roas, target_roas,
                    f"📊 Teljesítmény plató: 7 napja stabil de target alatt "
                    f"— tesztelj új audience-t vagy kreatívot",
                ))

    # 7) KREATÍV FÁRADÁS — csak Meta; utolsó 5 nap CTR folyamatosan csökken
    if platform == "meta" and len(daily) >= 5:
        ctr5 = [_f(d, "ctr") for d in daily[-5:]]
        if all(v is not None for v in ctr5) and _strictly_decreasing(ctr5) and (spend_cur or 0) > 0:
            out.append(_mk(
                campaign_id, "creative_fatigue", ctr5[-1] * 100, ctr5[0] * 100,
                f"🎨 CTR 5 napja csökken — kreatív fáradás jele, "
                f"fontold meg a kreatív frissítését",
            ))

    if out:
        log.info(
            "Insight motor: #%s — %d lehetőség: %s",
            campaign_id, len(out), [o["metric"] for o in out],
        )
    return out


# ---------------------------------------------------------------------------
# 3. szabály külön (peer-összevetés)
# ---------------------------------------------------------------------------

def _budget_reallocation(
    campaign_id: int | None,
    roas: float | None,
    target_roas: float | None,
    peer_campaigns: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Top 20% ROAS a fiókon belül ÉS van target alatti kampány → átcsoportosítás."""
    if roas is None or not target_roas or target_roas <= 0:
        return None

    peer_roas = [
        r for r in (_f(p, "roas") for p in peer_campaigns if p.get("id") != campaign_id)
        if r is not None
    ]
    all_roas = peer_roas + [roas]
    if len(all_roas) < _MIN_PEERS_FOR_REALLOC:
        return None

    threshold_80 = _percentile(all_roas, 80)
    if threshold_80 is None or roas < threshold_80:
        return None  # nem top 20%

    if not any(r < target_roas for r in peer_roas):
        return None  # nincs alulteljesítő, akitől átcsoportosíthatnánk

    avg = statistics.fmean(peer_roas) if peer_roas else roas
    return _mk(
        campaign_id, "budget_reallocation", roas, avg,
        f"🔀 Legjobb ROAS a fiókon belül ({roas:.1f}x vs átlag {avg:.1f}x) "
        f"— csoportosíts át büdzsét az alulteljesítőktől",
    )


# ---------------------------------------------------------------------------
# Belső segédfüggvények
# ---------------------------------------------------------------------------

def _mk(
    campaign_id: int | None,
    metric: str,
    observed_value: float | None,
    threshold_value: float | None,
    message: str,
) -> dict[str, Any]:
    """Egységes insight-dict (severity mindig 'insight')."""
    return {
        "campaign_id": campaign_id,
        "severity": "insight",
        "metric": metric,
        "observed_value": observed_value,
        "threshold_value": threshold_value,
        "message": message,
    }


def _f(d: dict[str, Any], key: str) -> float | None:
    """Mező float-ként, vagy None ha hiányzik / nem szám."""
    v = d.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _platform(campaign: dict[str, Any]) -> str:
    """A kampány platformja kisbetűvel. A campaigns táblában NINCS platform —
    a beágyazott ad_accounts.platform-ból (get_active_campaigns) olvassuk; a
    közvetlen `platform` kulcsot is elfogadjuk (teszt/enriched dict)."""
    p = campaign.get("platform")
    if not p:
        p = (campaign.get("ad_accounts") or {}).get("platform")
    return str(p or "").lower()


def _percentile(values: list[float], p: float) -> float | None:
    """`p` percentilis (0–100) lineáris interpolációval. None üres listára."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _trend_down(seq: list[float]) -> bool:
    """Csökkenő trend: végig nem-növekvő ÉS az eleje szigorúan nagyobb a végénél."""
    return all(a >= b for a, b in zip(seq, seq[1:])) and seq[0] > seq[-1]


def _trend_up(seq: list[float]) -> bool:
    """Növekvő trend: végig nem-csökkenő ÉS a vége szigorúan nagyobb az elejénél."""
    return all(a <= b for a, b in zip(seq, seq[1:])) and seq[-1] > seq[0]


def _strictly_decreasing(seq: list[float]) -> bool:
    """Szigorúan monoton csökkenő (minden lépés kisebb az előzőnél)."""
    return all(a > b for a, b in zip(seq, seq[1:]))
