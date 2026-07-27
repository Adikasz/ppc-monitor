"""
Insight-history olvasó réteg (18. lépés — INSIGHT motor).

A szabály-alapú és AI insight-motor innen olvassa a kampányok korábbi
metrikáit (campaign_insights) és a hatékony (öröklött) KPI-jait. Csak OLVAS —
nem ír DB-be és nem küld riasztást.

Fontos: a campaign_insights ÓRÁNKÉNTI, a napra halmozott (kumulált) snapshot
(a batch pull mindig az adott NAP eddigi összegét hozza). Ezért a „napi" idősorhoz
a `to_daily_series` naponta a LEGUTOLSÓ (legteljesebb) sort tartja meg.

Függvények:
    get_insights_history(campaign_id, days)   — nyers (óránkénti) sorok időrendben
    to_daily_series(history)                  — naponta a legutolsó sor (pure)
    get_latest_roas_map(campaign_ids)         — kampányonként a legutóbbi ROAS
    get_merged_kpis(campaign_id)              — campaign→account→client→default merge
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

_TABLE = "campaign_insights"

log = get_logger(__name__)


def _chunks(seq: list[Any], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def get_insights_history(campaign_id: int, days: int = 7) -> list[dict[str, Any]]:
    """A kampány utolsó `days` napi insight-sorai (óránkénti), fetched_at szerint NÖVEKVŐ.

    Üres lista, ha nincs adat. A motor a `to_daily_series`-szel redukálja napi
    idősorrá.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("campaign_id", campaign_id)
        .gt("fetched_at", cutoff)
        .order("fetched_at", desc=False)
        .execute()
    )
    return res.data or []


def to_daily_series(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Óránkénti (kumulált) sorokból napi idősor: naponta a LEGUTOLSÓ sor.

    Pure (nincs DB hívás) — így a motor offline tesztelhető. A bemenet
    fetched_at szerint növekvő; a kimenet dátum szerint növekvő. A fetched_at
    nélküli sorok (pl. mock teszt) mindegyike önálló „nap" marad (nem olvad össze).
    """
    by_day: dict[str, dict[str, Any]] = {}
    for idx, row in enumerate(history):
        fetched = row.get("fetched_at")
        # Napi kulcs: a fetched_at dátum-része; ha hiányzik, egyedi index-kulcs.
        day = str(fetched)[:10] if fetched else f"__{idx}"
        # A history növekvő sorrendű, így a később jövő (=aznap legutolsó) felülír.
        by_day[day] = row
    return [by_day[k] for k in sorted(by_day.keys())]


def get_latest_roas_map(campaign_ids: list[int]) -> dict[int, float | None]:
    """Kampányonként a LEGUTÓBBI ROAS (büdzsé-átcsoportosítás insighthoz, peer-összevetés).

    Kampányonként egy `limit(1)` lekérdezés (fetched_at desc) — a campaign_insights
    óránkénti, így csoportos „latest per group" PostgREST-tel a sor-limit miatt nem
    biztonságos. A hívó (scheduler) fiókonként cache-eli az eredményt.
    """
    out: dict[int, float | None] = {}
    sb = get_supabase()
    for cid in campaign_ids:
        res = (
            sb.table(_TABLE)
            .select("roas")
            .eq("campaign_id", cid)
            .order("fetched_at", desc=True)
            .limit(1)
            .execute()
        )
        out[cid] = (res.data[0].get("roas") if res.data else None)
    return out


def get_merged_kpis(campaign_id: int) -> dict[str, Any]:
    """A kampány hatékony KPI-ja: campaign_kpis → ad_account_kpis → client_kpis → default.

    Újrahasználja a detektor öröklési logikáját (mezőnként az első nem-None nyer),
    hogy az insight-motor és az anomália-detektor pontosan ugyanazt a „hatékony"
    KPI-t lássa. Üres dict, ha a kampány nem létezik.

    (A detektort lokálisan importáljuk, hogy elkerüljük a modul-betöltési ciklust.)
    """
    from src.monitoring import detector  # lokális import — réteg-ciklus elkerülése
    from src.storage import campaigns as campaigns_storage
    from src.storage import kpis as kpis_storage

    campaign = campaigns_storage.get_campaign(campaign_id)
    if not campaign:
        return {}

    campaign_kpis = kpis_storage.get_active_kpis(campaign_id) or {}
    account_kpis = detector._account_kpis_for_campaign(campaign) or {}
    client_kpis = detector._client_kpis_for_campaign(campaign) or {}
    return detector._merge_kpis(campaign_kpis, account_kpis, client_kpis)
