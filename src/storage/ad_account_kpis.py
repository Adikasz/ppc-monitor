"""
Fiók-szintű KPI (ad_account_kpis tábla) adathozzáférés (21. lépés).

A client_kpis fiók-szintű megfelelője: egy aktív sor / ad_account
(UNIQUE(ad_account_id)). A `/account kpi` ide ír, majd lecsorgatja az értékeket
a fiók kampányainak campaign_kpis sorába (lásd `storage.kpis.
cascade_account_kpis_to_campaigns`). A detektor öröklési lánca:
    campaign_kpis → ad_account_kpis → client_kpis → default.

A táblát a 0010 migration hozza létre. A READ függvények védettek: ha a tábla
még nem létezik, None / üres halmaz a válasz (a detektor a következő szintre
esik vissza), nem dobnak kivételt.
"""
from __future__ import annotations

from typing import Any

from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

_TABLE = "ad_account_kpis"

log = get_logger(__name__)

# A campaign_kpis-ba lecsorgatható mezők (target_roi NINCS a campaign_kpis-ban,
# ezért az nem öröklődik kampány-szintre — csak a fiók-szintű KPI-ban él).
CASCADE_FIELDS = (
    "target_roas",
    "max_cpa",
    "max_cpl",
    "monthly_budget",
    "target_ctr",
    "max_cpc",
    "primary_conversion_event",
    "warning_pct",
    "critical_pct",
    # Küszöb-alapú metrikák (0011 migration)
    "min_ctr",
    "max_cpm",
    "max_frequency",
    "min_impressions",
)


def _is_missing_relation(exc: Exception) -> bool:
    msg = str(exc).lower()
    return _TABLE in msg and any(
        s in msg for s in ("does not exist", "could not find", "pgrst205", "relation", "schema cache")
    )


def get_ad_account_kpis(ad_account_id: int) -> dict[str, Any] | None:
    """A fiók aktív KPI-sora, vagy None (ill. ha a tábla még nem létezik)."""
    try:
        res = (
            get_supabase()
            .table(_TABLE)
            .select("*")
            .eq("ad_account_id", ad_account_id)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        if _is_missing_relation(exc):
            log.warning("ad_account_kpis tábla még nem létezik (0010 migration?) — None")
            return None
        raise
    return res.data[0] if res.data else None


def upsert_ad_account_kpis(ad_account_id: int, *, fields: dict[str, Any]) -> dict[str, Any]:
    """Fiók KPI beszúrása vagy frissítése (egy sor / fiók, UNIQUE(ad_account_id)).

    Csak a `fields`-ben szereplő (nem-None) mezőket állítja. Új sornál a DB
    DEFAULT-ok töltik a warning_pct/critical_pct-t (20/40), ha nincsenek megadva.
    Hibát dob, ha a tábla még nem létezik — a hívó (parancs) kezeli.
    """
    sb = get_supabase()
    existing = (
        sb.table(_TABLE).select("id").eq("ad_account_id", ad_account_id).limit(1).execute()
    )
    if existing.data:
        res = (
            sb.table(_TABLE).update(fields).eq("id", existing.data[0]["id"]).execute()
        )
        return res.data[0] if res.data else {"id": existing.data[0]["id"], "ad_account_id": ad_account_id, **fields}

    payload = {"ad_account_id": ad_account_id, **fields}
    res = sb.table(_TABLE).insert(payload).execute()
    return res.data[0]


def list_account_ids_with_kpis() -> set[int]:
    """Azon ad_account-ID-k halmaza, amelyeknek van aktív KPI soruk (bulk, /account list).

    Védve: ha a tábla még nem létezik (0010 előtt), üres halmaz.
    """
    try:
        res = (
            get_supabase().table(_TABLE).select("ad_account_id").eq("is_active", True).execute()
        )
    except Exception as exc:  # noqa: BLE001
        if _is_missing_relation(exc):
            return set()
        raise
    return {r["ad_account_id"] for r in (res.data or [])}
