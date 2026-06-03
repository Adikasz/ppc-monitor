"""
KPI (campaign_kpis tábla) adathozzáférés.

A KPI-ok verziózottak: minden `set_kpis` hívás EGY ÚJ sort szúr be
(INSERT, nem UPDATE). A legújabb aktív sor az, amelyiknek `valid_to` NULL.
Az előző aktív sort lezárja a `set_kpis` (valid_to = NOW()), így megmarad
a historikus változáskövetés.

Struktúra (campaign_kpis tábla legfontosabb mezői):
    id                         — DB PK
    campaign_id                — FK campaigns.id
    valid_from                 — mikor lett aktív ez a KPI-verzió
    valid_to                   — NULL = jelenleg aktív; kitöltve = archivált
    target_roas                — célzott ROAS (%)
    max_cpa                    — max CPA (Ft)
    max_cpl                    — max CPL (Ft, lead kampányhoz)
    monthly_budget             — havi büdzsé (Ft)
    target_ctr                 — célzott CTR (%)
    max_cpc                    — max CPC (Ft)
    primary_conversion_event   — pl. "Purchase", "Lead"
    no_conversion_critical_days — X napja nincs konverzió → CRITICAL
    cpa_spike_critical_pct     — +X% CPA → CRITICAL
    trend_warning_days         — X napja csökken metrika → WARNING
"""
from __future__ import annotations

from typing import Any

from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

_TABLE = "campaign_kpis"

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Lekérdezések
# ---------------------------------------------------------------------------

def get_active_kpis(campaign_id: int) -> dict[str, Any] | None:
    """Az aktuálisan érvényes KPI-sor lekérdezése (valid_to IS NULL).

    Visszatér None-nal ha a kampányhoz még nincs beállítva KPI.
    """
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("campaign_id", campaign_id)
        .is_("valid_to", "null")
        .order("valid_from", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_kpi_history(campaign_id: int) -> list[dict[str, Any]]:
    """Összes KPI-verzió lekérdezése időrendi sorrendben (legújabb elől).

    Historikus elemzéshez és audithoz hasznos.
    """
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("campaign_id", campaign_id)
        .order("valid_from", desc=True)
        .execute()
    )
    return res.data or []


# ---------------------------------------------------------------------------
# Írás — verziózott INSERT
# ---------------------------------------------------------------------------

def set_kpis(
    campaign_id: int,
    *,
    target_roas: float | None = None,
    max_cpa: float | None = None,
    max_cpl: float | None = None,
    monthly_budget: float | None = None,
    target_ctr: float | None = None,
    max_cpc: float | None = None,
    primary_conversion_event: str | None = None,
    no_conversion_critical_days: int | None = None,
    cpa_spike_critical_pct: float | None = None,
    trend_warning_days: int | None = None,
) -> dict[str, Any]:
    """KPI beállítása verziózással.

    Működés:
        1. A meglévő aktív sort (valid_to IS NULL) lezárja: valid_to = NOW()
        2. Új sort szúr be a megadott értékekkel (valid_to NULL = aktív)

    Ha nincs meglévő aktív sor, csak az INSERT fut le — ez az első beállítás.

    A nem megadott mezők (None) kimaradnak a payloadból, azaz a Supabase
    az oszlop DEFAULT értékét használja (tipikusan NULL).

    Visszatérés: az újonnan létrehozott KPI sor.
    """
    sb = get_supabase()

    # 1) Lezárjuk a jelenlegi aktív verziót (ha van)
    existing = get_active_kpis(campaign_id)
    if existing:
        sb.table(_TABLE).update({"valid_to": "now()"}).eq("id", existing["id"]).execute()
        log.info(
            "KPI verzió lezárva: campaign_id=%s, kpi_id=%s",
            campaign_id, existing["id"],
        )

    # 2) Új verzió INSERT
    payload: dict[str, Any] = {"campaign_id": campaign_id}

    _optional_fields = {
        "target_roas": target_roas,
        "max_cpa": max_cpa,
        "max_cpl": max_cpl,
        "monthly_budget": monthly_budget,
        "target_ctr": target_ctr,
        "max_cpc": max_cpc,
        "primary_conversion_event": primary_conversion_event,
        "no_conversion_critical_days": no_conversion_critical_days,
        "cpa_spike_critical_pct": cpa_spike_critical_pct,
        "trend_warning_days": trend_warning_days,
    }
    for field, value in _optional_fields.items():
        if value is not None:
            payload[field] = value

    res = sb.table(_TABLE).insert(payload).execute()
    new_kpi = res.data[0]

    log.info(
        "Új KPI verzió létrehozva: campaign_id=%s, kpi_id=%s",
        campaign_id, new_kpi["id"],
    )
    return new_kpi
