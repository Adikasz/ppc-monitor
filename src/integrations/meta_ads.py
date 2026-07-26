"""
Meta Ads API integráció.

MetaAdsClient — singleton osztály a facebook-business SDK köré csomagolva.
Egyszeri inicializálás, majd az egész alkalmazás ebből az egy példányból hívja
a Meta Graph API-t.

Használat:
    from src.integrations.meta_ads import MetaAdsClient

    client = MetaAdsClient.get_instance()
    campaigns = client.get_campaigns("act_123456789")
    insights  = client.get_campaign_insights("act_123456789", "987654321",
                                             "2026-06-01", "2026-06-03")

Hibakezelés:
    FacebookRequestError — API hiba (401 invalid token, 403 jogosultság)
    requests.exceptions.ConnectionError — hálózati hiba
    Mindkettő naplózva + újra dobva, hogy a hívó (discovery) dönthessen.
"""
from __future__ import annotations

import time
from typing import Any

from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.campaign import Campaign as FBCampaign
from facebook_business.exceptions import FacebookRequestError

from src.config import get_config
from src.utils.logging import get_logger

log = get_logger(__name__)

# Konverziónak számító Meta action típusok (purchase + lead variánsok).
_CONVERSION_TYPES = {
    "purchase",
    "lead",
    "omni_purchase",
    "offsite_conversion.fb_pixel_purchase",
    "offsite_conversion.fb_pixel_lead",
}

# Batch insights rate-limit kezelés: exponenciális backoff (1s, 2s, 4s).
_INSIGHTS_MAX_RETRIES = 3
# Meta rate-limit API hibakódok (app / user / account szintű korlátok).
_RATE_LIMIT_API_CODES = {4, 17, 32, 613}


def _normalize_insight(
    external_campaign_id: str,
    impressions: Any,
    clicks: Any,
    spend: Any,
    conversions: Any,
    conversion_value: Any,
    frequency: Any = None,
) -> dict[str, Any]:
    """Nyers metrikák → közös, normalizált insight-dict (származtatottakkal).

    A számított mezők (ctr, cpc, cpa, roas, cpm) PONTOSAN úgy állnak elő, mint a
    `src.monitoring.metrics`-ben (arány-alapú ctr, nem százalék), hogy a
    detektor és a campaign_insights tábla a batch- és a per-kampány úton
    azonosan viselkedjen. A `frequency` a Meta API-ból jön (Google-nál nincs).
    """
    impressions = int(impressions or 0)
    clicks = int(clicks or 0)
    spend = float(spend or 0.0)
    conversions = float(conversions or 0.0)
    conversion_value = float(conversion_value or 0.0)

    ctr = round(clicks / impressions, 4) if impressions else 0.0
    cpc = round(spend / clicks, 2) if clicks else 0.0
    cpa = round(spend / conversions, 2) if conversions else 0.0
    roas = round(conversion_value / spend, 4) if spend else 0.0
    cpm = round(spend / impressions * 1000, 2) if impressions else 0.0

    return {
        "external_campaign_id": str(external_campaign_id),
        "impressions": impressions,
        "clicks": clicks,
        "spend": round(spend, 2),
        "conversions": conversions,
        "conversion_value": round(conversion_value, 2),
        "ctr": ctr,
        "cpc": cpc,
        "cpa": cpa,
        "roas": roas,
        "cpm": cpm,
        "frequency": round(float(frequency), 2) if frequency not in (None, "") else None,
    }


def _conversions_from_actions(row: Any) -> tuple[int, float]:
    """(conversions, conversion_value) kiszámítása egy insights sor actions/
    action_values mezőiből (purchase + lead típusok összegezve)."""
    actions = row.get("actions") or []
    action_values = row.get("action_values") or []
    conversions = sum(
        int(float(a.get("value", 0)))
        for a in actions
        if a.get("action_type") in _CONVERSION_TYPES
    )
    conversion_value = sum(
        float(v.get("value", 0.0))
        for v in action_values
        if v.get("action_type") in _CONVERSION_TYPES
    )
    return conversions, conversion_value


class MetaAdsClient:
    """Facebook/Meta Ads API kliens — singleton pattern.

    Egyszer inicializálódik (get_instance()), majd az egész folyamat
    alatt ugyanazt a hitelesített kapcsolatot használja.
    """

    _instance: "MetaAdsClient | None" = None

    def __init__(
        self,
        access_token: str,
        app_id: str = "",
        app_secret: str = "",
    ) -> None:
        # A facebook_business SDK globális API inicializálása.
        # app_id / app_secret opcionális — access_token self is elegendő.
        FacebookAdsApi.init(
            app_id=app_id or None,
            app_secret=app_secret or None,
            access_token=access_token,
        )
        self._access_token = access_token
        log.info("MetaAdsClient inicializálva (app_id=%s)", app_id or "—")

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> "MetaAdsClient":
        """Visszaadja az egyetlen MetaAdsClient példányt.

        Raises:
            RuntimeError — ha a META_ACCESS_TOKEN nincs beállítva a konfigban.
        """
        if cls._instance is None:
            config = get_config()
            if not config.meta_access_token:
                raise RuntimeError(
                    "META_ACCESS_TOKEN nincs beállítva. "
                    "Ellenőrizd a .env fájlt vagy a Railway beállításokat."
                )
            cls._instance = cls(
                access_token=config.meta_access_token,
                app_id=config.meta_app_id,
                app_secret=config.meta_app_secret,
            )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Singleton törlése (teszteléshez / token frissítéshez)."""
        cls._instance = None

    # ------------------------------------------------------------------
    # Elérhető hirdetési fiókok (autocomplete-hez)
    # ------------------------------------------------------------------

    def list_accessible_accounts(self) -> list[dict[str, Any]]:
        """A tokennel elérhető Meta hirdetési fiókok (ID + NÉV).

        A `/account add` autocomplete ebből épít választható listát, hogy ne
        kelljen kézzel `act_...` ID-t begépelni.

        FONTOS KORLÁT: a `/me/adaccounts` csak azokat a fiókokat adja vissza,
        amelyek KÖZVETLENÜL a token tulajdonosához vannak rendelve. Ügynökségi /
        business hozzáférésű fiókok kimaradhatnak (éles mérésben 66 fiók jött
        vissza, miközben 71 Meta fiók van használatban) — ezért az autocomplete
        csak JAVASLAT, a kézi ID-beírás továbbra is működik.

        Visszatérés: [{"id": "act_123", "name": "Fiók neve", "status": 1}, ...]
        Hiba esetén ÜRES lista (nem dob) — az autocomplete sosem törhet el.
        """
        try:
            from facebook_business.adobjects.user import User

            accounts = User(fbid="me").get_ad_accounts(
                fields=["id", "name", "account_status"]
            )
            result = [
                {
                    "id": str(a.get("id")),
                    "name": a.get("name") or str(a.get("id")),
                    "status": a.get("account_status"),
                }
                for a in accounts
            ]
            log.info("Meta API — elérhető fiókok: %d", len(result))
            return result
        except Exception as exc:  # noqa: BLE001 — az autocomplete nem törhet el
            log.error("Meta elérhető fiókok lekérése sikertelen: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Kampányok
    # ------------------------------------------------------------------

    def get_campaigns(self, ad_account_id: str) -> list[dict[str, Any]]:
        """Összes kampány lekérése egy Meta hirdetési fiókból.

        Paraméterek:
            ad_account_id — Meta fiók ID, format: "act_123456789"

        Visszatérés: lista, minden elem tartalmazza:
            {
                "id":           "123456789",        # external_campaign_id
                "name":         "Kampány neve",
                "status":       "ACTIVE",            # ACTIVE | PAUSED | DELETED | ARCHIVED
                "created_time": "2026-01-01T10:00:00+0000",
                "updated_time": "2026-06-01T10:00:00+0000",
            }

        Raises:
            FacebookRequestError — API hiba (401 token, 403 jogosultság stb.)
            Exception            — hálózati hiba
        """
        try:
            account = AdAccount(ad_account_id)
            raw = account.get_campaigns(
                fields=["id", "name", "status", "created_time", "updated_time"]
            )
            result = [
                {
                    "id": str(c["id"]),
                    "name": c["name"],
                    "status": c.get("status", "UNKNOWN"),
                    "created_time": c.get("created_time"),
                    "updated_time": c.get("updated_time"),
                }
                for c in raw
            ]
            log.info(
                "Meta API — kampányok lekérve: fiók=%s, db=%d",
                ad_account_id, len(result),
            )
            return result

        except FacebookRequestError as exc:
            http_code = exc.http_status()
            if http_code == 401:
                log.error(
                    "Meta API 401 — érvénytelen hozzáférési token (fiók: %s): %s",
                    ad_account_id, exc,
                )
            elif http_code == 403:
                log.error(
                    "Meta API 403 — nincs jogosultság ehhez a fiókhoz (fiók: %s): %s",
                    ad_account_id, exc,
                )
            else:
                log.error(
                    "Meta API hiba %s (fiók: %s): %s",
                    http_code, ad_account_id, exc,
                )
            raise

        except Exception as exc:
            log.error(
                "Hálózati/váratlan hiba a Meta API hívásakor (fiók: %s): %s",
                ad_account_id, exc,
            )
            raise

    # ------------------------------------------------------------------
    # Insights (metrikák)
    # ------------------------------------------------------------------

    def get_campaign_insights(
        self,
        ad_account_id: str,
        campaign_id: str,
        date_from: str,
        date_to: str,
    ) -> dict[str, Any]:
        """Metrikák lekérése egy kampányra adott időszakra.

        Paraméterek:
            ad_account_id — Meta fiók ID ("act_123456789") — jelenleg unused,
                            de a jövőbeli batch lekérésekhez szükséges
            campaign_id   — Meta kampány ID (pl. "987654321")
            date_from     — időszak kezdete "YYYY-MM-DD" formátumban
            date_to       — időszak vége   "YYYY-MM-DD" formátumban

        Visszatérés:
            {
                "impressions":        10000,
                "clicks":             500,
                "spend":              25000.00,   # hirdetési költés (valuta egység)
                "conversions":        50,          # purchase + lead összesen
                "conversion_value":   100000.00,   # purchase_value összesen
            }

        Ha nincs adat az időszakra, nulla értékeket ad vissza (nem dob hibát).

        Raises:
            FacebookRequestError — API hiba
            Exception            — hálózati hiba
        """
        _empty: dict[str, Any] = {
            "impressions": 0,
            "clicks": 0,
            "spend": 0.0,
            "conversions": 0,
            "conversion_value": 0.0,
        }

        try:
            campaign = FBCampaign(campaign_id)
            insights = campaign.get_insights(
                fields=[
                    "impressions",
                    "clicks",
                    "spend",
                    "frequency",
                    "actions",
                    "action_values",
                ],
                params={
                    "time_range": {"since": date_from, "until": date_to},
                    "level": "campaign",
                },
            )

            if not insights:
                log.debug(
                    "Meta Insights — nincs adat: kampány=%s, %s–%s",
                    campaign_id, date_from, date_to,
                )
                return _empty

            data = insights[0]

            # Konverziók aggregálása: purchase + lead típusú action-ök
            _conversion_types = {
                "purchase",
                "lead",
                "omni_purchase",
                "offsite_conversion.fb_pixel_purchase",
                "offsite_conversion.fb_pixel_lead",
            }

            actions = data.get("actions") or []
            action_values = data.get("action_values") or []

            conversions = sum(
                int(float(a.get("value", 0)))
                for a in actions
                if a.get("action_type") in _conversion_types
            )
            conversion_value = sum(
                float(v.get("value", 0.0))
                for v in action_values
                if v.get("action_type") in _conversion_types
            )

            _freq = data.get("frequency")
            result: dict[str, Any] = {
                "impressions": int(data.get("impressions", 0)),
                "clicks": int(data.get("clicks", 0)),
                "spend": float(data.get("spend", 0.0)),
                "conversions": conversions,
                "conversion_value": conversion_value,
                "frequency": float(_freq) if _freq not in (None, "") else None,
            }
            log.debug(
                "Meta Insights — kampány=%s, %s–%s: %s",
                campaign_id, date_from, date_to, result,
            )
            return result

        except FacebookRequestError as exc:
            log.error(
                "Meta Insights API hiba (kampány=%s, %s–%s): %s",
                campaign_id, date_from, date_to, exc,
            )
            raise

        except Exception as exc:
            log.error(
                "Hálózati hiba a Meta Insights hívásakor (kampány=%s): %s",
                campaign_id, exc,
            )
            raise

    # ------------------------------------------------------------------
    # Batch insights — 1 API hívás / ad_account (16. lépés)
    # ------------------------------------------------------------------

    def get_all_campaigns_insights(
        self,
        ad_account_id: str,
        date_from: str,
        date_to: str,
    ) -> list[dict[str, Any]]:
        """Egy fiók ÖSSZES kampányának metrikái EGY hívással (level=campaign).

        Ez váltja ki a kampányonkénti `get_campaign_insights` hívásokat: 2000+
        helyett ~1 hívás / ad_account.

        Paraméterek:
            ad_account_id — Meta fiók ID ("act_123456789")
            date_from     — időszak kezdete "YYYY-MM-DD"
            date_to       — időszak vége   "YYYY-MM-DD"

        Visszatérés: normalizált insight-dict lista (lásd `_normalize_insight`),
        minden elem `external_campaign_id` kulccsal. Hiba esetén — a rate-limit
        retry kimerülése vagy bármilyen API/hálózati hiba után — ÜRES listát ad
        (NEM dob kivételt), hogy a scheduler ciklusa ne álljon le.

        Rate limit: 429 / rate-limit API kód esetén exponenciális backoff
        (1s, 2s, 4s; max 3 retry).
        """
        last_error: Exception | None = None
        for attempt in range(_INSIGHTS_MAX_RETRIES + 1):
            try:
                account = AdAccount(ad_account_id)
                cursor = account.get_insights(
                    fields=[
                        "campaign_id",
                        "campaign_name",
                        "impressions",
                        "clicks",
                        "spend",
                        "frequency",
                        "actions",
                        "action_values",
                    ],
                    params={
                        "time_range": {"since": date_from, "until": date_to},
                        "level": "campaign",
                    },
                )

                results: list[dict[str, Any]] = []
                for row in cursor:
                    conversions, conversion_value = _conversions_from_actions(row)
                    results.append(_normalize_insight(
                        str(row.get("campaign_id")),
                        row.get("impressions", 0),
                        row.get("clicks", 0),
                        row.get("spend", 0.0),
                        conversions,
                        conversion_value,
                        row.get("frequency"),
                    ))

                log.info(
                    "Meta batch insights: fiók=%s, %d kampány (%s–%s)",
                    ad_account_id, len(results), date_from, date_to,
                )
                return results

            except FacebookRequestError as exc:
                http_code = exc.http_status()
                api_code = exc.api_error_code()
                is_rate_limited = http_code == 429 or api_code in _RATE_LIMIT_API_CODES
                if is_rate_limited and attempt < _INSIGHTS_MAX_RETRIES:
                    backoff = 2 ** attempt  # 1s, 2s, 4s
                    log.warning(
                        "Meta batch rate limit (fiók=%s, http=%s, code=%s) — "
                        "retry %d/%d %ds múlva",
                        ad_account_id, http_code, api_code,
                        attempt + 1, _INSIGHTS_MAX_RETRIES, backoff,
                    )
                    last_error = exc
                    time.sleep(backoff)
                    continue
                log.error(
                    "Meta batch insights hiba (fiók=%s, http=%s, code=%s): %s — üres lista",
                    ad_account_id, http_code, api_code, exc,
                )
                return []

            except Exception as exc:  # noqa: BLE001 — sosem dobjuk ki a schedulert
                log.error(
                    "Meta batch insights váratlan hiba (fiók=%s): %s — üres lista",
                    ad_account_id, exc,
                )
                return []

        log.error(
            "Meta batch insights: rate-limit retry kimerült (fiók=%s): %s — üres lista",
            ad_account_id, last_error,
        )
        return []
