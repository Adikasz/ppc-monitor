"""
KPI (campaign_kpis tábla) adathozzáférés.

A KPI-ok verziózottak: minden `set_kpis` hívás EGY ÚJ sort szúr be
(INSERT, nem UPDATE). A legújabb aktív sor az, amelynek `is_active` = true.
Az előző aktív sort a `set_kpis` lezárja (is_active = false), így megmarad
a historikus változáskövetés.

Struktúra (campaign_kpis tábla legfontosabb mezői):
    id                         — DB PK
    campaign_id                — FK campaigns.id
    is_active                  — true = jelenleg aktív verzió; false = archivált
    created_at                 — mikor jött létre ez a KPI-verzió
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
    """Az aktuálisan érvényes KPI-sor lekérdezése (is_active = true).

    Visszatér None-nal ha a kampányhoz még nincs beállítva KPI.
    """
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .eq("campaign_id", campaign_id)
        .eq("is_active", True)
        .order("created_at", desc=True)
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
        .order("created_at", desc=True)
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
        1. A meglévő aktív sort (is_active = true) lezárja: is_active = false
        2. Új sort szúr be a megadott értékekkel (is_active = true = aktív)

    Ha nincs meglévő aktív sor, csak az INSERT fut le — ez az első beállítás.

    A nem megadott mezők (None) kimaradnak a payloadból, azaz a Supabase
    az oszlop DEFAULT értékét használja (tipikusan NULL).

    Visszatérés: az újonnan létrehozott KPI sor.
    """
    sb = get_supabase()

    # 1) Lezárjuk a jelenlegi aktív verziót (ha van)
    existing = get_active_kpis(campaign_id)
    if existing:
        sb.table(_TABLE).update({"is_active": False}).eq("id", existing["id"]).execute()
        log.info(
            "KPI verzió lezárva: campaign_id=%s, kpi_id=%s",
            campaign_id, existing["id"],
        )

    # 2) Új verzió INSERT (is_active = true = ez lesz az aktív)
    payload: dict[str, Any] = {"campaign_id": campaign_id, "is_active": True}

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


# ---------------------------------------------------------------------------
# Kliens-szintű KPI kaszkád (15. lépés)
# ---------------------------------------------------------------------------

def _chunks(seq: list[Any], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def cascade_client_kpis_to_campaigns(
    campaign_ids: list[int],
    *,
    values: dict[str, Any],
    set_by_discord_user_id: str | None = None,
) -> dict[str, int]:
    """Kliens KPI-értékek lecsorgatása a megadott kampányok aktív KPI-sorába.

    A `values` a campaign_kpis-kompatibilis mezők (target_roas, max_cpa, …,
    warning_pct, critical_pct). Kampányonként:
      - ha van aktív campaign_kpis sor ÉS az KÉZI override (inherited_from_client
        = false) → KIHAGYJUK (a per-kampány override megmarad);
      - ha van aktív, de örökölt sor → helyben UPDATE az új kliens-értékekkel;
      - ha nincs aktív sor → új sort szúrunk be (is_active=true,
        inherited_from_client=true).

    Visszatérés: {"updated", "inserted", "skipped_override", "total"}.
    """
    result = {"updated": 0, "inserted": 0, "skipped_override": 0, "total": len(campaign_ids)}
    if not campaign_ids:
        return result

    sb = get_supabase()

    # Aktív sorok bulk-lekérése a kampányokra (egy körrel, nem N+1).
    active_rows: list[dict[str, Any]] = []
    for chunk in _chunks(campaign_ids, 200):
        rows = (
            sb.table(_TABLE)
            .select("id, campaign_id, inherited_from_client")
            .in_("campaign_id", chunk)
            .eq("is_active", True)
            .execute()
            .data
            or []
        )
        active_rows.extend(rows)
    active_by_cid = {r["campaign_id"]: r for r in active_rows}

    to_insert: list[dict[str, Any]] = []
    for cid in campaign_ids:
        row = active_by_cid.get(cid)
        if row is not None:
            if not row.get("inherited_from_client", False):
                result["skipped_override"] += 1
                continue
            sb.table(_TABLE).update(
                {**values, "inherited_from_client": True}
            ).eq("id", row["id"]).execute()
            result["updated"] += 1
        else:
            payload: dict[str, Any] = {
                "campaign_id": cid,
                "is_active": True,
                "inherited_from_client": True,
                **values,
            }
            if set_by_discord_user_id is not None:
                payload["set_by_discord_user_id"] = set_by_discord_user_id
            to_insert.append(payload)

    for chunk in _chunks(to_insert, 500):
        if chunk:
            sb.table(_TABLE).insert(chunk).execute()
            result["inserted"] += len(chunk)

    return result


def cascade_account_kpis_to_campaigns(
    campaign_ids: list[int],
    *,
    values: dict[str, Any],
    set_by_discord_user_id: str | None = None,
) -> dict[str, int]:
    """Fiók-szintű KPI-értékek lecsorgatása a megadott kampányok aktív KPI-sorába (21. lépés).

    Kampányonként:
      - KÉZI override (inherited_from_account=false ÉS inherited_from_client=false)
        → KIHAGYJUK (a per-kampány /campaign kpi megmarad);
      - örökölt aktív sor → helyben UPDATE + inherited_from_account=true;
      - nincs aktív sor → új sor (is_active=true, inherited_from_account=true).

    Visszatérés: {"updated", "inserted", "skipped_override", "total"}.
    """
    result = {"updated": 0, "inserted": 0, "skipped_override": 0, "total": len(campaign_ids)}
    if not campaign_ids:
        return result

    sb = get_supabase()

    active_rows: list[dict[str, Any]] = []
    for chunk in _chunks(campaign_ids, 200):
        rows = (
            sb.table(_TABLE)
            .select("id, campaign_id, inherited_from_client, inherited_from_account")
            .in_("campaign_id", chunk)
            .eq("is_active", True)
            .execute()
            .data
            or []
        )
        active_rows.extend(rows)
    active_by_cid = {r["campaign_id"]: r for r in active_rows}

    to_insert: list[dict[str, Any]] = []
    for cid in campaign_ids:
        row = active_by_cid.get(cid)
        if row is not None:
            is_manual = not (
                row.get("inherited_from_client") or row.get("inherited_from_account")
            )
            if is_manual:
                result["skipped_override"] += 1
                continue
            sb.table(_TABLE).update(
                {**values, "inherited_from_account": True}
            ).eq("id", row["id"]).execute()
            result["updated"] += 1
        else:
            payload: dict[str, Any] = {
                "campaign_id": cid,
                "is_active": True,
                "inherited_from_account": True,
                **values,
            }
            if set_by_discord_user_id is not None:
                payload["set_by_discord_user_id"] = set_by_discord_user_id
            to_insert.append(payload)

    for chunk in _chunks(to_insert, 500):
        if chunk:
            sb.table(_TABLE).insert(chunk).execute()
            result["inserted"] += len(chunk)

    return result
