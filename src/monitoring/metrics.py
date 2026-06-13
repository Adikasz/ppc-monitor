"""
Kampány metrika-lekérés — valódi Meta/Google insights.

A scheduler óránként ezzel kéri le egy kampány aktuális metrikáit a megfelelő
platform API-ból (Meta vagy Google), és közös, normalizált formátumban adja
vissza. A detektor és az insights-tárolás ebből dolgozik.

Belépő:
    insights = await get_campaign_metrics(campaign)   # campaign = campaigns sor (dict)

Logika:
    1. A kampány ad_account-jának lekérése (platform + external_account_id)
    2. Platform szerinti API hívás:
        - meta   → MetaAdsClient.get_campaign_insights(account, campaign, from, to)
        - google → GoogleAdsClient.get_campaign_metrics(customer, campaign, from, to)
    3. Normalizálás + származtatott metrikák (ctr, cpc, cpa, roas)

Időablak:
    Alapból a MAI nap (date_from = date_to = ma). A scheduler óránként hívja,
    így ez "a mai nap eddigi teljesítménye". Paraméterrel felülírható.

Hibatűrés (sosem állítja le a schedulert):
    - hiányzó token / nem telepített SDK (pl. Google) → warning, return None
    - API hiba / hálózat                              → error, return None
    - hiányos kampányadat / nincs ad_account          → warning, return None
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any

from src.integrations.google_ads import GoogleAdsClient
from src.integrations.meta_ads import MetaAdsClient
from src.storage import ad_accounts as ad_accounts_storage
from src.utils.logging import get_logger

log = get_logger(__name__)


async def get_campaign_metrics(
    campaign: dict[str, Any],
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any] | None:
    """Egy kampány aktuális, normalizált metrikái — vagy None hiba esetén.

    Visszatérés (siker esetén):
        {
            "impressions": int, "clicks": int, "spend": float,
            "conversions": float, "conversion_value": float,
            "ctr": float, "cpc": float, "cpa": float, "roas": float,
            "fetched_at": str (ISO),
        }
    """
    campaign_id = campaign.get("id")
    ad_account_id = campaign.get("ad_account_id")
    ext_campaign_id = campaign.get("external_campaign_id")

    if ad_account_id is None or not ext_campaign_id:
        log.warning("Metrics: hiányos kampányadat (#%s) — skip", campaign_id)
        return None

    # 1) ad_account → platform + external_account_id
    account = await asyncio.to_thread(ad_accounts_storage.get_ad_account, ad_account_id)
    if account is None:
        log.warning(
            "Metrics: nincs ad_account (#%s, ad_account_id=%s) — skip",
            campaign_id, ad_account_id,
        )
        return None

    platform = account.get("platform")
    ext_account_id = account.get("external_account_id")

    # Időablak — alapból a mai nap
    today = date.today().isoformat()
    d_from = date_from or today
    d_to = date_to or today

    # 2) Platform API hívás (a kliens get_instance hiányzó token/SDK esetén
    #    RuntimeError-t dob — ezt elkapjuk, hogy a scheduler ne álljon le).
    try:
        if platform == "meta":
            client = MetaAdsClient.get_instance()
            raw = await asyncio.to_thread(
                client.get_campaign_insights,
                ext_account_id, str(ext_campaign_id), d_from, d_to,
            )
            spend = _f(raw.get("spend"))
        elif platform == "google":
            client = GoogleAdsClient.get_instance()
            raw = await asyncio.to_thread(
                client.get_campaign_metrics,
                ext_account_id, str(ext_campaign_id), d_from, d_to,
            )
            spend = _f(raw.get("cost"))  # Google: "cost" → spend
        else:
            log.warning("Metrics: ismeretlen platform %r (#%s) — skip", platform, campaign_id)
            return None
    except RuntimeError as exc:
        # hiányzó token / nem telepített SDK → ne dobjuk ki a schedulert
        log.warning("Metrics: %s kliens nem elérhető (#%s): %s", platform, campaign_id, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — API/hálózati hiba
        log.error("Metrics: API hiba (#%s, %s): %s", campaign_id, platform, exc)
        return None

    impressions = _i(raw.get("impressions"))
    clicks = _i(raw.get("clicks"))
    conversions = _f(raw.get("conversions"))
    conversion_value = _f(raw.get("conversion_value"))

    # 3) Származtatott metrikák (0-osztás védve)
    ctr = (clicks / impressions) if impressions else 0.0
    cpc = (spend / clicks) if clicks else 0.0
    cpa = (spend / conversions) if conversions else 0.0
    roas = (conversion_value / spend) if spend else 0.0

    return {
        "impressions": impressions,
        "clicks": clicks,
        "spend": round(spend, 2),
        "conversions": conversions,
        "conversion_value": round(conversion_value, 2),
        "ctr": round(ctr, 4),
        "cpc": round(cpc, 2),
        "cpa": round(cpa, 2),
        "roas": round(roas, 4),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Típus-biztos konverterek
# ---------------------------------------------------------------------------

def _f(v: Any) -> float:
    """float, vagy 0.0 ha None/érvénytelen."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _i(v: Any) -> int:
    """int, vagy 0 ha None/érvénytelen."""
    if v is None:
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0
