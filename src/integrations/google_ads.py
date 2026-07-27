"""
Google Ads API integráció.

GoogleAdsClient — singleton osztály a google-ads SDK köré csomagolva.
Ugyanaz a pattern, mint a MetaAdsClient (src/integrations/meta_ads.py):
egyszeri inicializálás, majd az egész alkalmazás ebből az egy példányból
hívja a Google Ads API-t.

Használat:
    from src.integrations.google_ads import GoogleAdsClient

    client = GoogleAdsClient.get_instance()
    campaigns = client.get_campaigns("1234567890")
    metrics   = client.get_campaign_metrics("1234567890", "111222333",
                                            "2026-06-01", "2026-06-03")

Fontos — lazy import:
    A `google-ads` SDK importja NEM a modul tetején történik, hanem a
    GoogleAdsClient.__init__-ben. Így ennek a modulnak az importálása soha
    nem dob ImportError-t akkor sem, ha az SDK nincs telepítve (jelenleg
    Python 3.14 alatt nincs kompatibilis google-ads wheel). Ez biztosítja,
    hogy a discovery és a bot betöltése ne dőljön el a hiányzó SDK miatt —
    a hiba csak az első tényleges Google Ads hívásnál jelentkezik, kontrollált
    RuntimeError formájában.

Hibakezelés:
    - SDK nincs telepítve   → get_instance() warning + RuntimeError
    - Hiányzó token/config  → get_instance() warning + RuntimeError
    - Auth hiba             → log (kategorizálva) + re-raise
    - Rate limit (quota)    → log (kategorizálva) + re-raise
    - Hálózati/váratlan     → log + re-raise
    A get_instance() RuntimeError-ját a discovery elkapja és az errors listába
    teszi, így a hiányzó Google konfiguráció nem állítja le a Meta discovery-t.
"""
from __future__ import annotations

from typing import Any

from src.config import get_config
from src.utils.logging import get_logger

log = get_logger(__name__)


def _normalize_insight(
    external_campaign_id: str,
    impressions: Any,
    clicks: Any,
    spend: Any,
    conversions: Any,
    conversion_value: Any,
) -> dict[str, Any]:
    """Nyers metrikák → közös, normalizált insight-dict (származtatottakkal).

    A számított mezők (ctr, cpc, cpa, roas) PONTOSAN úgy állnak elő, mint a
    `src.monitoring.metrics`-ben és a Meta integrációban (arány-alapú ctr),
    hogy a detektor és a campaign_insights tábla egységesen viselkedjen.
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
    }


# Kampány-discovery GAQL lekérdezés. A nem törölt (REMOVED) kampányok jönnek.
_CAMPAIGNS_GAQL = """
    SELECT
        campaign.id,
        campaign.name,
        campaign.status
    FROM campaign
    WHERE campaign.status != 'REMOVED'
"""


class GoogleAdsClient:
    """Google Ads API kliens — singleton pattern (MetaAdsClient mintára).

    Egyszer inicializálódik (get_instance()), majd a folyamat alatt ugyanazt
    a hitelesített SDK klienst használja. Az SDK importja és a kliens
    felépítése lazy — csak az első get_instance() hívásnál történik.
    """

    _instance: "GoogleAdsClient | None" = None

    # Google Ads API kvóta: 600 lekérdezés/perc (referencia; nem kényszerített).
    RATE_LIMIT_PER_MIN = 600

    def __init__(self, credentials: dict[str, Any]) -> None:
        # Lazy import: az SDK csak itt töltődik be. Ha nincs telepítve,
        # ImportError-t dob — ezt a get_instance() RuntimeError-rá fordítja.
        try:
            from google.ads.googleads.client import (
                GoogleAdsClient as _SdkGoogleAdsClient,
            )
            from google.ads.googleads.errors import GoogleAdsException
        except ImportError as exc:  # SDK nincs telepítve (pl. Python 3.14)
            raise RuntimeError(
                "A google-ads SDK nincs telepítve. Telepítsd "
                "(Python 3.14-kompatibilis google-ads wheel szükséges), "
                "majd próbáld újra."
            ) from exc

        # Az SDK exception-osztályát eltesszük, hogy a metódusok elkaphassák.
        # Top-level importtal nem tehetnénk, mert az SDK hiányozhat.
        self._GoogleAdsException = GoogleAdsException
        self._client = _SdkGoogleAdsClient.load_from_dict(credentials)
        self._login_customer_id = credentials.get("login_customer_id")
        log.info(
            "GoogleAdsClient inicializálva (login_customer_id=%s)",
            self._login_customer_id or "—",
        )

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> "GoogleAdsClient":
        """Visszaadja az egyetlen GoogleAdsClient példányt.

        Raises:
            RuntimeError — ha a kötelező Google Ads konfiguráció hiányzik,
                           vagy ha a google-ads SDK nincs telepítve.
        """
        if cls._instance is None:
            config = get_config()

            # Kötelező mezők — bármelyik hiánya esetén nem indítható a kliens.
            missing = [
                name
                for name, value in (
                    ("GOOGLE_ADS_DEVELOPER_TOKEN", config.google_ads_developer_token),
                    ("GOOGLE_ADS_CLIENT_ID", config.google_ads_client_id),
                    ("GOOGLE_ADS_CLIENT_SECRET", config.google_ads_client_secret),
                    ("GOOGLE_ADS_REFRESH_TOKEN", config.google_ads_refresh_token),
                )
                if not value
            ]
            if missing:
                msg = (
                    "Google Ads konfiguráció hiányzik: "
                    + ", ".join(missing)
                    + ". Ellenőrizd a .env fájlt vagy a Railway beállításokat."
                )
                log.warning(msg)
                raise RuntimeError(msg)

            credentials: dict[str, Any] = {
                "developer_token": config.google_ads_developer_token,
                "client_id": config.google_ads_client_id,
                "client_secret": config.google_ads_client_secret,
                "refresh_token": config.google_ads_refresh_token,
                "use_proto_plus": True,
            }
            # MCC (manager) account ID — kötőjel nélkül, ha be van állítva.
            if config.google_ads_login_customer_id:
                credentials["login_customer_id"] = _normalize_customer_id(
                    config.google_ads_login_customer_id
                )

            cls._instance = cls(credentials)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Singleton törlése (teszteléshez / token frissítéshez)."""
        cls._instance = None

    # ------------------------------------------------------------------
    # Elérhető fiókok (autocomplete-hez)
    # ------------------------------------------------------------------

    def list_accessible_accounts(self) -> list[dict[str, Any]]:
        """A hitelesítéssel elérhető Google Ads fiókok (ID + NÉV).

        Két lépés:
          1. `CustomerService.list_accessible_customers` — a refresh tokennel
             elérhető gyökér-fiókok (jellemzően az MCC/manager).
          2. Ha van `login_customer_id` (MCC), a `customer_client` view-n
             keresztül lekérjük az MCC alá tartozó ÖSSZES nem-manager fiókot
             névvel együtt — ez adja az érdemi listát.

        Ha a 2. lépés bármiért nem megy, az 1. lépés nyers ID-listájával térünk
        vissza (név nélkül) — így az autocomplete akkor is ad valamit.

        Visszatérés: [{"id": "1234567890", "name": "Fiók neve"}, ...]
        Hiba esetén ÜRES lista (nem dob) — az autocomplete sosem törhet el.
        """
        try:
            customer_service = self._client.get_service("CustomerService")
            accessible = customer_service.list_accessible_customers()
            # resource_name formátum: "customers/1234567890"
            root_ids = [
                rn.split("/")[-1] for rn in accessible.resource_names
            ]
            log.info("Google Ads — elérhető gyökér-fiókok: %d", len(root_ids))
        except Exception as exc:  # noqa: BLE001
            log.error("Google Ads list_accessible_customers hiba: %s", exc)
            return []

        # Az MCC alatti fiókok névvel — a login_customer_id-t preferáljuk,
        # de ha nincs, minden elérhető gyökeret végigpróbálunk.
        manager_ids = [self._login_customer_id] if self._login_customer_id else root_ids

        found: dict[str, str] = {}
        for manager_id in manager_ids:
            if not manager_id:
                continue
            try:
                ga_service = self._client.get_service("GoogleAdsService")
                query = """
                    SELECT
                        customer_client.id,
                        customer_client.descriptive_name,
                        customer_client.manager,
                        customer_client.status
                    FROM customer_client
                    WHERE customer_client.status = 'ENABLED'
                """
                response = ga_service.search(customer_id=str(manager_id), query=query)
                for row in response:
                    cc = row.customer_client
                    # A manager (MCC) fiókokban nincsenek kampányok — kihagyjuk.
                    if cc.manager:
                        continue
                    cid = str(cc.id)
                    found[cid] = cc.descriptive_name or cid
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Google Ads customer_client lekérés hiba (manager=%s): %s",
                    manager_id, exc,
                )

        if not found:
            # Fallback: legalább a nyers gyökér-ID-k, név nélkül.
            log.warning(
                "Google Ads — customer_client lista üres, fallback a "
                "gyökér-ID-kre (%d db, név nélkül)", len(root_ids),
            )
            return [{"id": cid, "name": cid} for cid in root_ids]

        log.info("Google Ads — elérhető fiókok névvel: %d", len(found))
        return [{"id": cid, "name": name} for cid, name in sorted(found.items(), key=lambda kv: kv[1].lower())]

    def get_account_name(self, customer_id: str) -> str | None:
        """Egy fiók OLVASHATÓ NEVE (descriptive_name), KÖZVETLEN lekérdezéssel.

        A `list_accessible_accounts()`-tól eltérően ez NEM az MCC
        `customer_client` view-n megy át, hanem egyetlen GAQL sorral
        közvetlenül az adott customer resource-ot kérdezi le — működhet olyan
        fiókra is, ami valamiért nem jelenik meg az MCC-view-ban, feltéve hogy
        a refresh token hozzáfér.

        A `scripts/backfill_account_names.py` és a `/account add` kézi eset
        másodlagos próbálkozásaként használva. Hiba esetén None — sosem dob
        (a hívó felelős a fallback kezeléséért).
        """
        cid = _normalize_customer_id(customer_id)
        try:
            ga_service = self._client.get_service("GoogleAdsService")
            response = ga_service.search(
                customer_id=cid,
                query="SELECT customer.descriptive_name FROM customer LIMIT 1",
            )
            for row in response:
                return row.customer.descriptive_name or None
            return None
        except Exception as exc:  # noqa: BLE001 — a hívó (backfill) nem törhet el
            log.warning("Google Ads fiók név lekérés hiba (fiók=%s): %s", cid, exc)
            return None

    # ------------------------------------------------------------------
    # Kampányok
    # ------------------------------------------------------------------

    def get_campaigns(self, customer_id: str) -> list[dict[str, Any]]:
        """Összes (nem törölt) kampány lekérése egy Google Ads fiókból.

        Paraméterek:
            customer_id — Google Ads ügyfél ID, format: "1234567890"
                          (kötőjel nélkül; kötőjeles input automatikusan tisztul)

        Visszatérés: lista, minden elem:
            {
                "external_campaign_id": "111222333",
                "name":                 "Kampány neve",
                "status":               "ENABLED",     # ENABLED | PAUSED | ...
                "created_at":           None,          # a GAQL nem kéri le a dátumot
            }

        Raises:
            RuntimeError — auth hiba / SDK hiba (a hívó dönt)
            Exception    — rate limit / hálózati / váratlan hiba
        """
        cid = _normalize_customer_id(customer_id)
        try:
            ga_service = self._client.get_service("GoogleAdsService")
            response = ga_service.search(customer_id=cid, query=_CAMPAIGNS_GAQL)

            result: list[dict[str, Any]] = []
            for row in response:
                campaign = row.campaign
                result.append(
                    {
                        "external_campaign_id": str(campaign.id),
                        "name": campaign.name,
                        "status": campaign.status.name,  # proto enum → str
                        # A discovery nem használ dátumot; a campaign.start_date a
                        # GAQL-ből kikerült (a kezdő/záró dátumokat insightnál a
                        # segments.date adja). A kulcsot kompat. miatt tartjuk.
                        "created_at": None,
                    }
                )
            log.info(
                "Google Ads API — kampányok lekérve: fiók=%s, db=%d",
                cid, len(result),
            )
            return result

        except self._GoogleAdsException as exc:
            self._handle_google_ads_exception(exc, cid)
            raise
        except Exception as exc:
            log.error(
                "Hálózati/váratlan hiba a Google Ads API hívásakor (fiók=%s): %s",
                cid, exc,
            )
            raise

    # ------------------------------------------------------------------
    # Metrikák
    # ------------------------------------------------------------------

    def get_campaign_metrics(
        self,
        customer_id: str,
        campaign_id: str,
        date_from: str,
        date_to: str,
    ) -> dict[str, Any]:
        """Metrikák lekérése egy kampányra adott időszakra.

        Paraméterek:
            customer_id — Google Ads fiók ID ("1234567890")
            campaign_id — Google Ads kampány ID ("111222333")
            date_from   — időszak kezdete "YYYY-MM-DD"
            date_to     — időszak vége   "YYYY-MM-DD"

        Visszatérés (a teljes időszakra aggregálva):
            {
                "impressions":      10000,
                "clicks":           500,
                "cost":             25000.00,   # cost_micros / 1_000_000
                "conversions":      50.0,
                "conversion_value": 100000.00,  # conversions_value
            }

        Ha nincs adat az időszakra, nulla értékeket ad vissza (nem dob hibát).

        Raises:
            RuntimeError — auth hiba / SDK hiba
            Exception    — rate limit / hálózati / váratlan hiba
        """
        cid = _normalize_customer_id(customer_id)
        _empty: dict[str, Any] = {
            "impressions": 0,
            "clicks": 0,
            "cost": 0.0,
            "conversions": 0.0,
            "conversion_value": 0.0,
        }

        query = (
            "SELECT metrics.impressions, metrics.clicks, metrics.cost_micros, "
            "metrics.conversions, metrics.conversions_value "
            "FROM campaign "
            f"WHERE campaign.id = {int(campaign_id)} "
            f"AND segments.date BETWEEN '{date_from}' AND '{date_to}'"
        )

        try:
            ga_service = self._client.get_service("GoogleAdsService")
            response = ga_service.search(customer_id=cid, query=query)

            # Több sor jöhet (napi szegmentálás) — aggregáljuk az időszakra.
            agg = dict(_empty)
            found = False
            for row in response:
                found = True
                m = row.metrics
                agg["impressions"] += int(m.impressions)
                agg["clicks"] += int(m.clicks)
                agg["cost"] += m.cost_micros / 1_000_000
                agg["conversions"] += float(m.conversions)
                agg["conversion_value"] += float(m.conversions_value)

            if not found:
                log.debug(
                    "Google Ads metrics — nincs adat: kampány=%s, %s–%s",
                    campaign_id, date_from, date_to,
                )
                return _empty

            log.debug(
                "Google Ads metrics — kampány=%s, %s–%s: %s",
                campaign_id, date_from, date_to, agg,
            )
            return agg

        except self._GoogleAdsException as exc:
            self._handle_google_ads_exception(exc, cid)
            raise
        except Exception as exc:
            log.error(
                "Hálózati/váratlan hiba a Google Ads metrics hívásakor "
                "(kampány=%s): %s",
                campaign_id, exc,
            )
            raise

    # ------------------------------------------------------------------
    # Batch metrics — 1 GAQL query / customer (16. lépés)
    # ------------------------------------------------------------------

    def get_all_campaigns_metrics(
        self,
        customer_id: str,
        date_from: str,
        date_to: str,
    ) -> list[dict[str, Any]]:
        """Egy fiók ÖSSZES (ENABLED) kampányának metrikái EGY GAQL query-vel.

        Ez váltja ki a kampányonkénti `get_campaign_metrics` hívásokat.

        Paraméterek:
            customer_id — Google Ads fiók ID ("1234567890" v. kötőjeles)
            date_from   — időszak kezdete "YYYY-MM-DD"
            date_to     — időszak vége   "YYYY-MM-DD"

        Visszatérés: normalizált insight-dict lista (`external_campaign_id`
        kulccsal). Hiba (auth / kvóta / hálózat) esetén ÜRES lista — NEM dob
        kivételt, hogy a scheduler ciklusa ne álljon le.

        Megjegyzés: a `segments.date` csak szűrőként szerepel (nincs a SELECT-ben),
        így az API kampányonként a teljes időszakra aggregál. Biztonságból
        Pythonban is összegezzük campaign.id szerint (ha mégis több sor jönne).
        """
        cid = _normalize_customer_id(customer_id)
        query = (
            "SELECT campaign.id, campaign.name, metrics.impressions, "
            "metrics.clicks, metrics.cost_micros, metrics.conversions, "
            "metrics.conversions_value "
            "FROM campaign "
            "WHERE campaign.status = 'ENABLED' "
            f"AND segments.date BETWEEN '{date_from}' AND '{date_to}'"
        )

        # Aggregálás campaign.id szerint (id → összegzett nyers metrikák).
        agg: dict[str, dict[str, Any]] = {}
        try:
            ga_service = self._client.get_service("GoogleAdsService")
            response = ga_service.search(customer_id=cid, query=query)
            for row in response:
                camp_id = str(row.campaign.id)
                m = row.metrics
                bucket = agg.setdefault(
                    camp_id,
                    {"impressions": 0, "clicks": 0, "spend": 0.0,
                     "conversions": 0.0, "conversion_value": 0.0},
                )
                bucket["impressions"] += int(m.impressions)
                bucket["clicks"] += int(m.clicks)
                bucket["spend"] += m.cost_micros / 1_000_000
                bucket["conversions"] += float(m.conversions)
                bucket["conversion_value"] += float(m.conversions_value)

        except self._GoogleAdsException as exc:
            self._handle_google_ads_exception(exc, cid)
            return []
        except Exception as exc:  # noqa: BLE001 — sosem dobjuk ki a schedulert
            log.error(
                "Google Ads batch metrics váratlan hiba (fiók=%s): %s — üres lista",
                cid, exc,
            )
            return []

        results = [
            _normalize_insight(
                camp_id,
                b["impressions"], b["clicks"], b["spend"],
                b["conversions"], b["conversion_value"],
            )
            for camp_id, b in agg.items()
        ]
        log.info(
            "Google Ads batch metrics: fiók=%s, %d kampány (%s–%s)",
            cid, len(results), date_from, date_to,
        )
        return results

    # ------------------------------------------------------------------
    # Belső segédfüggvények
    # ------------------------------------------------------------------

    def _handle_google_ads_exception(self, exc: Any, customer_id: str) -> None:
        """GoogleAdsException naplózása kategorizálva (auth / rate limit / egyéb).

        A kategorizálás csak naplózási célú; a hívó minden esetben re-raise-eli
        az eredeti kivételt, hogy a discovery az errors listába tehesse.
        """
        is_auth = False
        is_rate_limit = False
        messages: list[str] = []

        failure = getattr(exc, "failure", None)
        for err in (getattr(failure, "errors", None) or []):
            error_code = getattr(err, "error_code", None)
            if error_code is not None:
                # A error_code egy oneof — csak az egyik alfield van beállítva.
                if getattr(error_code, "authentication_error", 0):
                    is_auth = True
                if getattr(error_code, "authorization_error", 0):
                    is_auth = True
                if getattr(error_code, "quota_error", 0):
                    is_rate_limit = True
            msg = getattr(err, "message", "") or ""
            if msg:
                messages.append(msg)

        detail = "; ".join(messages) or str(exc)
        if is_auth:
            log.error(
                "Google Ads API auth hiba (fiók=%s): %s", customer_id, detail,
            )
        elif is_rate_limit:
            log.error(
                "Google Ads API rate limit / kvóta (fiók=%s, limit=%d/perc): %s",
                customer_id, self.RATE_LIMIT_PER_MIN, detail,
            )
        else:
            log.error(
                "Google Ads API hiba (fiók=%s): %s", customer_id, detail,
            )


def _normalize_customer_id(customer_id: str) -> str:
    """Google Ads customer ID normalizálása: kötőjel + whitespace eltávolítása.

    '123-456-7890' → '1234567890'
    """
    return customer_id.replace("-", "").strip()
