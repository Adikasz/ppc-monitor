"""
AI insight — Claude (Haiku) optimalizálási javaslat egy kampányra (18. lépés).

A szabály-alapú motor (insight_engine) fölé húzott 2. szint: ha a kliensnél
`insights_enabled=True` (ezt a HÍVÓ ellenőrzi, mert nála van a kliens), akkor
egy rövid, magyar, akcióorientált javaslatot kérünk Claude-tól a kampány
elmúlt 7 napi metrikáiból és a hatékony KPI-jaiból.

Költség-kontroll (mindkettő itt, a hívás ELŐTT):
    - 7 napos dedup: kampányonként max 1 AI javaslat / 7 nap
      (alerts, metric='ai_recommendation').
    - Napi globális limit: max `_AI_DAILY_LIMIT` (50) AI hívás / nap.

Visszatérés: a javaslat szövege (str), vagy None (hiba / limit / nincs kulcs).
A hívó a None-t kihagyja; a nem-None szöveget 'ai_recommendation' insight-ként
fűzi a kiküldendők közé.
"""
from __future__ import annotations

import asyncio
from typing import Any

from src.config import get_config
from src.storage import alerts as alerts_storage
from src.storage.insights_history import to_daily_series
from src.utils.logging import get_logger

log = get_logger(__name__)

# Napi globális AI-hívás limit (cost control). ~$0.004/nap 50 kampánynál.
_AI_DAILY_LIMIT = 50
# Kampányonkénti dedup ablak.
_DEDUP_DAYS = 7
_MAX_TOKENS = 300

_SYSTEM = (
    "Te egy PPC kampány elemző vagy. Rövid, konkrét, akcióorientált javaslatot "
    "adj magyarul. Maximum 2-3 mondat. Csak a legfontosabb 1 javaslatot add."
)

# Lazán inicializált AsyncAnthropic kliens (egyszer, folyamat-szinten).
_client: Any = None


def _get_client() -> Any:
    """AsyncAnthropic kliens (singleton). RuntimeError ha nincs API kulcs."""
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic  # lazy import — bot indul SDK-hiba nélkül is

        api_key = get_config().anthropic_api_key
        if not api_key:
            raise RuntimeError("Hiányzó ANTHROPIC_API_KEY")
        _client = AsyncAnthropic(api_key=api_key)
    return _client


async def generate_ai_insight(
    campaign: dict[str, Any],
    insights_history: list[dict[str, Any]],
    kpi: dict[str, Any],
) -> str | None:
    """Egy AI optimalizálási javaslat egy kampányra (lásd modul-docstring).

    A hívó felel az `insights_enabled` ellenőrzéséért. Itt a 7 napos dedup és a
    napi limit ellenőrzés fut (ha van campaign id), majd a Claude hívás.
    """
    cfg = get_config()
    if not cfg.anthropic_api_key:
        log.debug("AI insight: nincs ANTHROPIC_API_KEY — kihagyva")
        return None

    campaign_id = campaign.get("id")

    # Költség-kontroll csak akkor, ha van valódi kampány id (a hívások DB-t néznek).
    if campaign_id is not None:
        recent = await asyncio.to_thread(
            alerts_storage.recent_insight_metrics, campaign_id, days=_DEDUP_DAYS
        )
        if "ai_recommendation" in recent:
            log.debug("AI insight #%s: 7 napon belül már volt — skip", campaign_id)
            return None

        used_today = await asyncio.to_thread(
            alerts_storage.count_metric_today, "ai_recommendation"
        )
        if used_today >= _AI_DAILY_LIMIT:
            log.info("AI insight napi limit (%d) elérve — skip", _AI_DAILY_LIMIT)
            return None

    user_prompt = _build_user_prompt(campaign, insights_history, kpi)

    try:
        client = _get_client()
        msg = await client.messages.create(
            model=cfg.claude_model,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:  # noqa: BLE001 — az AI sosem buktathatja meg a scant
        log.error("AI insight hívás hiba (#%s): %s", campaign_id, exc)
        return None

    text = "".join(
        getattr(block, "text", "") for block in (msg.content or [])
        if getattr(block, "type", None) == "text"
    ).strip()
    return text or None


# ---------------------------------------------------------------------------
# Prompt összeállítás
# ---------------------------------------------------------------------------

def _build_user_prompt(
    campaign: dict[str, Any],
    insights_history: list[dict[str, Any]],
    kpi: dict[str, Any],
) -> str:
    """A user-prompt: kampány- és KPI-kontextus + napi metrika-tábla."""
    name = campaign.get("name") or "?"
    platform = (campaign.get("platform") or (campaign.get("ad_accounts") or {}).get("platform") or "?")
    client_name = campaign.get("client_name") or "?"

    table = _format_metrics_table(insights_history)

    return (
        f"Kampány: {name} ({platform})\n"
        f"Kliens: {client_name}\n"
        f"Platform: {platform}\n\n"
        f"KPI target-ek:\n"
        f"- ROAS target: {_kpi(kpi, 'target_roas')}\n"
        f"- Max CPA: {_kpi(kpi, 'max_cpa')} Ft\n"
        f"- Havi büdzsé: {_kpi(kpi, 'monthly_budget')} Ft\n"
        f"- Warning: -{_kpi(kpi, 'warning_pct')}% | Critical: -{_kpi(kpi, 'critical_pct')}%\n\n"
        f"Elmúlt 7 nap napi metrikái:\n"
        f"Dátum | Spend | ROAS | CPA | CTR | Clicks\n"
        f"{table}\n\n"
        f"Adj 1 konkrét optimalizálási javaslatot!"
    )


def _format_metrics_table(insights_history: list[dict[str, Any]]) -> str:
    """Napi metrika-tábla soronként. A ctr arány → százalék (×100)."""
    daily = to_daily_series(insights_history)
    if not daily:
        return "(nincs adat)"
    lines = []
    for row in daily:
        d = str(row.get("fetched_at") or "?")[:10]
        spend = _n(row.get("spend"))
        roas = _n(row.get("roas"))
        cpa = _n(row.get("cpa"))
        ctr = row.get("ctr")
        ctr_pct = f"{float(ctr) * 100:.1f}%" if _is_num(ctr) else "?"
        clicks = _n(row.get("clicks"))
        lines.append(f"{d} | {spend} | {roas} | {cpa} | {ctr_pct} | {clicks}")
    return "\n".join(lines)


def _kpi(kpi: dict[str, Any], key: str) -> str:
    v = kpi.get(key)
    return str(v) if v is not None else "—"


def _is_num(v: Any) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _n(v: Any) -> str:
    """Szám rövid formázása a táblához (egész → tizedes nélkül)."""
    if not _is_num(v):
        return "?"
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:.2f}"
