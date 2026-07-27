"""
Kliens-szintű KPI (client_kpis tábla) adathozzáférés.

A client_kpis a kliens KPI-default-jait és a warning/critical százalékokat tárolja
(egy aktív sor / kliens, UNIQUE(client_id)). A `/client kpi` ide ír, majd
lecsorgatja az értékeket a kliens kampányainak campaign_kpis sorába (lásd
`storage.kpis.cascade_client_kpis_to_campaigns`). A detektor innen örököl, ha
egy kampánynak nincs saját (campaign-szintű) értéke.

Megjegyzés: a táblát a 0009 migration hozza létre. A READ függvények
védettek — ha a tábla még nem létezik, None-t adnak (a detektor a default
küszöbökre esik vissza), nem dobnak kivételt. Az upsert kivételt dob, ha a
tábla hiányzik — ezt a parancs-réteg fordítja barátságos „futtasd a 0009-et"
üzenetre.
"""
from __future__ import annotations

from typing import Any

from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

_TABLE = "client_kpis"

log = get_logger(__name__)

# A campaign_kpis-ba lecsorgatható mezők (target_roi NINCS a campaign_kpis-ban,
# ezért az nem öröklődik kampány-szintre — csak a kliens-szintű KPI-ban él).
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
)


def _is_missing_relation(exc: Exception) -> bool:
    """True, ha a hiba a hiányzó client_kpis táblára utal (0009 még nem futott)."""
    msg = str(exc).lower()
    return _TABLE in msg and any(
        s in msg for s in ("does not exist", "could not find", "pgrst205", "relation", "schema cache")
    )


def get_client_kpis(client_id: int) -> dict[str, Any] | None:
    """A kliens aktív KPI-sora, vagy None ha nincs (ill. ha a tábla még nem létezik)."""
    try:
        res = (
            get_supabase()
            .table(_TABLE)
            .select("*")
            .eq("client_id", client_id)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        if _is_missing_relation(exc):
            log.warning("client_kpis tábla még nem létezik (0009 migration?) — None")
            return None
        raise
    return res.data[0] if res.data else None


def upsert_client_kpis(client_id: int, *, fields: dict[str, Any]) -> dict[str, Any]:
    """Kliens KPI beszúrása vagy frissítése (egy sor / kliens, UNIQUE(client_id)).

    Csak a `fields`-ben szereplő (nem-None) mezőket állítja. Új sornál a DB
    DEFAULT-ok töltik a warning_pct/critical_pct-t (20/40), ha nincsenek megadva.

    Hibát dob, ha a tábla még nem létezik — a hívó (parancs) kezeli.
    """
    sb = get_supabase()
    existing = (
        sb.table(_TABLE).select("id").eq("client_id", client_id).limit(1).execute()
    )
    if existing.data:
        res = (
            sb.table(_TABLE)
            .update(fields)
            .eq("id", existing.data[0]["id"])
            .execute()
        )
        return res.data[0] if res.data else {"id": existing.data[0]["id"], "client_id": client_id, **fields}

    payload = {"client_id": client_id, **fields}
    res = sb.table(_TABLE).insert(payload).execute()
    return res.data[0]


def list_client_ids_with_kpis() -> set[int]:
    """Azon kliens-ID-k halmaza, amelyeknek van aktív client_kpis soruk.

    A `/client list` KPI-jelzőjéhez (✅/❌), bulk. Védve: ha a tábla még nem
    létezik (0009 előtt), üres halmazt ad.
    """
    try:
        res = (
            get_supabase()
            .table(_TABLE)
            .select("client_id")
            .eq("is_active", True)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        if _is_missing_relation(exc):
            return set()
        raise
    return {r["client_id"] for r in (res.data or [])}
