"""
Heti riport adatréteg — nyers insight-olvasás + heti aggregátum-cache.

Két dolgot csinál, mindkettőt a heti riport (src.monitoring.weekly_action_report)
számára:

1. `get_insight_rows_for_campaigns` — a megadott kampányok campaign_insights
   sorai egy időablakban. NYERS sorokat ad vissza (óránkénti, napon belül
   KUMULÁLT snapshotok) — a napi/heti redukciót a monitoring réteg végzi,
   hogy az offline tesztelhető maradjon.

2. `get_cached_week` / `upsert_cached_week` — a weekly_report_metrics tábla
   (0013 migration). Ez őrzi meg a heti számokat azután is, hogy az óránkénti
   ciklus a 7 napnál régebbi campaign_insights sorokat törli — enélkül az
   "előző hét" oszlop soha nem tudna kitöltődni. Lásd a migration fejlécét.

A cache-műveletek SOSEM dobnak: ha a 0013 migration még nem futott le, a
riport az összehasonlító oszlop nélkül, de elkészül (warning a logban).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

_INSIGHTS_TABLE = "campaign_insights"
_CACHE_TABLE = "weekly_report_metrics"

# A PostgREST egy kérésre max 1000 sort ad vissza — `range()`-dzsel lapozunk
# (ugyanaz a minta, mint a campaigns_storage.get_active_campaigns-ben).
_PAGE = 1000
# Ennyi campaign_id megy egy `in_()` szűrőbe. A szűrő a QUERY STRINGBE kerül,
# így több ezer ID egyetlen kérésben túllépné az URL-hosszt — ezért kötegelünk.
_ID_CHUNK = 200

# Csak a heti aggregációhoz kellő oszlopok. Egy nagy ügyfélnél ez tízezres
# nagyságrendű sor — a `select *` feleslegesen sokszorosára hízlalná a választ.
_INSIGHT_COLUMNS = (
    "campaign_id, fetched_at, impressions, clicks, spend, conversions, conversion_value"
)

log = get_logger(__name__)


def _chunks(seq: list[Any], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
# Nyers insight sorok
# ---------------------------------------------------------------------------

def get_insight_rows_for_campaigns(
    campaign_ids: list[int],
    from_dt: datetime,
    to_dt: datetime,
) -> list[dict[str, Any]]:
    """A megadott kampányok campaign_insights sorai a [from_dt, to_dt) ablakban.

    A határok fél-nyitottak (`.gte()` / `.lt()`) — ugyanaz a konvenció, mint a
    `summary.daily_range`-nél: két egymást követő hét nem fed át, és egyetlen
    mérés sem esik ki közülük.

    Rendezés: fetched_at NÖVEKVŐ, `id`-vel mint holtverseny-döntővel. Az `id`
    NEM kozmetika: a fetched_at nem egyedi (egy órában több kampány sora is
    ugyanazt kaphatja), márpedig egy nem-egyedi rendezőkulcs mellett a lapozás
    átugorhat vagy duplikálhat sorokat. A hívó a "napon belüli UTOLSÓ sor"
    szabályra épít, ezért itt a determinisztikus sorrend korrektségi kérdés.

    Két szinten kötegel:
      - `in_()` ID-koszorú `_ID_CHUNK`-onként (URL-hossz),
      - kötegen belül `range()` lapozás (PostgREST 1000-es sorlimit).
    """
    if not campaign_ids:
        return []

    sb = get_supabase()
    out: list[dict[str, Any]] = []

    for chunk in _chunks(list(campaign_ids), _ID_CHUNK):
        start = 0
        while True:
            rows = (
                sb.table(_INSIGHTS_TABLE)
                .select(_INSIGHT_COLUMNS)
                .in_("campaign_id", chunk)
                .gte("fetched_at", from_dt.isoformat())
                .lt("fetched_at", to_dt.isoformat())
                .order("fetched_at", desc=False)
                .order("id", desc=False)
                .range(start, start + _PAGE - 1)
                .execute()
                .data
                or []
            )
            out.extend(rows)
            if len(rows) < _PAGE:
                break
            start += _PAGE

    return out


# ---------------------------------------------------------------------------
# Heti aggregátum-cache (0013 migration)
# ---------------------------------------------------------------------------

_CACHE_METRICS = (
    "spend", "impressions", "clicks", "conversions", "conversion_value",
    "ctr", "cpa", "roas",
)


def get_cached_week(client_id: int, week_start: date) -> dict[str, Any] | None:
    """Egy ügyfél korábban elmentett heti aggregátuma. None, ha nincs.

    None-t ad (nem dob) akkor is, ha a 0013 migration még nem futott le — a
    riport ilyenkor az összehasonlító oszlop nélkül készül el.
    """
    try:
        res = (
            get_supabase()
            .table(_CACHE_TABLE)
            .select("*")
            .eq("client_id", client_id)
            .eq("week_start", week_start.isoformat())
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 — hiányzó tábla / DB hiba
        log.warning(
            "Heti cache olvasás sikertelen (ügyfél #%s, hét %s): %s — "
            "a riport előző heti összehasonlítás nélkül készül. "
            "Lefutott a 0013_weekly_report_metrics migration?",
            client_id, week_start, exc,
        )
        return None
    return res.data[0] if res.data else None


def upsert_cached_week(
    client_id: int,
    week_start: date,
    totals: dict[str, Any],
) -> bool:
    """A hét aggregátumának elmentése/frissítése (client_id + week_start kulcsra).

    Idempotens: a `uq_weekly_report_metrics_client_week` egyedi indexre
    upsertelünk, így a job kétszeri lefuttatása (hétfői cron + kézi
    `/report weekly-now`) nem hoz létre duplikátumot, csak felülírja a sort.

    A hívó ezt a NYERS AGGREGÁCIÓ UTÁN, de a Claude-hívás és a ClickUp Doc ELŐTT
    hívja: a következő heti összehasonlítás így akkor is megmarad, ha az AI vagy
    a ClickUp lépés elhasal.

    Visszatérés: True ha sikerült, False ha (logolt) hiba történt — az utóbbi
    sosem állítja meg a riportot.
    """
    payload: dict[str, Any] = {
        "client_id": client_id,
        "week_start": week_start.isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    for key in _CACHE_METRICS:
        payload[key] = totals.get(key)

    try:
        (
            get_supabase()
            .table(_CACHE_TABLE)
            .upsert(payload, on_conflict="client_id,week_start")
            .execute()
        )
        return True
    except Exception as exc:  # noqa: BLE001 — hiányzó tábla / DB hiba
        log.warning(
            "Heti cache írás sikertelen (ügyfél #%s, hét %s): %s — a riport "
            "elkészül, de JÖVŐ HÉTEN nem lesz mihez hasonlítani. "
            "Lefutott a 0013_weekly_report_metrics migration?",
            client_id, week_start, exc,
        )
        return False
