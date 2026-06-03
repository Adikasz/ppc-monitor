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

from typing import Any

from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.campaign import Campaign as FBCampaign
from facebook_business.exceptions import FacebookRequestError

from src.config import get_config
from src.utils.logging import get_logger

log = get_logger(__name__)


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

            result: dict[str, Any] = {
                "impressions": int(data.get("impressions", 0)),
                "clicks": int(data.get("clicks", 0)),
                "spend": float(data.get("spend", 0.0)),
                "conversions": conversions,
                "conversion_value": conversion_value,
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
