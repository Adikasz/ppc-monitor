"""
Kampány auto-discovery — Meta Ads + Google Ads (egységes).

A discovery lekéri az ügyfél hirdetési fiókjaihoz (Meta ÉS Google) tartozó
összes kampányt a platform API-ból, majd szinkronizálja a `campaigns` táblával:

  NEW campaign   → INSERT (lifecycle_state='new', is_monitored=true)
  EXISTING       → UPDATE (platform_status, last_seen_at)
  NOT SEEN 24h   → soft delete (is_monitored=false)

Szinkronizálási logika:
  1. Ügyfélhez rendelt aktív ad_accounts lekérdezése (Meta + Google)
  2. Mindegyik fiókra: a platformnak megfelelő API-ból kampányok lekérése,
     közös formátumra normalizálva
  3. DB upsert: új kampány INSERT, meglévő UPDATE (status + last_seen_at)
  4. Stale kampányok (24+ óra nem látott) soft-delete

Hibakezelés (fault isolation):
  - Egy platform kliens inicializálási hibája (pl. hiányzó Google token vagy
    nem telepített SDK) csak az adott platform fiókjait érinti — a másik
    platform discovery-je fut tovább.
  - Egy fiók API hibája NEM állítja le a teljes discovery-t; a hiba az errors
    listába kerül, a többi fiók folytatódik.
  - DB hiba esetén az adott kampány hibája logolva, discovery folytatódik.

Visszatérési érték:
  {
    "inserted":    5,
    "updated":     12,
    "deactivated": 1,
    "errors":      [{"account": "act_xxx", "error": "..."}],
  }
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from src.integrations.google_ads import GoogleAdsClient
from src.integrations.meta_ads import MetaAdsClient
from src.storage import account_assignments as account_assignments_storage
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import assignments as assignments_storage
from src.storage import campaigns as campaigns_storage
from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

log = get_logger(__name__)

# Ennyi óra elteltével kerül soft-delete-be egy kampány, ha nem látjuk az API-ban
_STALE_HOURS = 24


def discover_campaigns_for_client(client_id: int) -> dict[str, Any]:
    """Kampány-discovery egy ügyfél összes (Meta + Google) ad accountjára.

    Paraméterek:
        client_id — ügyfél belső DB ID-ja

    Visszatérés:
        {
            "inserted":    int,      — DB-be újonnan felvett kampányok
            "updated":     int,      — frissített kampányok (status, last_seen_at)
            "deactivated": int,      — soft-deleted (24h+ nem látott) kampányok
            "errors":      list,     — [{"account": "...", "error": "..."}]
        }
    """
    result: dict[str, Any] = {
        "inserted": 0,
        "updated": 0,
        "deactivated": 0,
        "errors": [],
    }

    # 1) Összes aktív ad_account (Meta + Google) ehhez az ügyfélhez
    accounts = ad_accounts_storage.get_ad_accounts_for_client(
        client_id,
        platform=None,
        active_only=True,
    )
    if not accounts:
        log.info(
            "Discovery: nincs aktív ad_account ehhez az ügyfélhez (client_id=%s)",
            client_id,
        )
        return result

    log.info(
        "Discovery indítva: client_id=%s, %d fiók (Meta + Google)",
        client_id, len(accounts),
    )

    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(hours=_STALE_HOURS)

    # Platform-kliensek lazy cache + init-hiba jegyzék.
    # Egy platform kliensét csak akkor (és csak egyszer) inicializáljuk, ha van
    # hozzá fiók. Ha az init elhasal, a platform összes fiókját kihagyjuk.
    client_cache: dict[str, Any] = {}
    failed_platforms: dict[str, str] = {}

    for account in accounts:
        platform: str = account["platform"]
        ext_account_id: str = account["external_account_id"]
        db_account_id: int = account["id"]

        # Platform kliens előállítása (cache-elve, init-hiba platformonként egyszer)
        if platform in failed_platforms:
            result["errors"].append({
                "account": ext_account_id,
                "error": failed_platforms[platform],
            })
            continue

        if platform not in client_cache:
            try:
                client_cache[platform] = _make_platform_client(platform)
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "Discovery: %s kliens inicializálási hiba: %s",
                    platform, exc,
                )
                failed_platforms[platform] = str(exc)
                result["errors"].append({
                    "account": ext_account_id,
                    "error": str(exc),
                })
                continue

        client = client_cache[platform]

        log.info(
            "Discovery: fiók=%s (%s, db_id=%s) feldolgozása…",
            ext_account_id, platform, db_account_id,
        )

        # 2) Platform API hívás → közös formátumú kampánylista
        try:
            api_campaigns = _fetch_campaigns(platform, client, ext_account_id)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Discovery: API hiba fiók=%s (%s): %s",
                ext_account_id, platform, exc,
            )
            result["errors"].append({
                "account": ext_account_id,
                "error": str(exc),
            })
            continue  # következő fiók

        api_campaign_ids: set[str] = set()

        # 3) Upsert: minden API kampányra
        for api_c in api_campaigns:
            ext_campaign_id: str = api_c["ext_campaign_id"]
            api_campaign_ids.add(ext_campaign_id)

            try:
                _upsert_campaign(
                    client_id=client_id,
                    db_account_id=db_account_id,
                    ext_campaign_id=ext_campaign_id,
                    api_campaign=api_c,
                    now=now,
                    result=result,
                )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "Discovery: DB upsert hiba kampány=%s: %s",
                    ext_campaign_id, exc,
                )
                result["errors"].append({
                    "account": ext_account_id,
                    "campaign": ext_campaign_id,
                    "error": str(exc),
                })

        # 4) Soft-delete: ennél régebben látott is_monitored kampányok
        _deactivate_stale(
            db_account_id=db_account_id,
            stale_cutoff=stale_cutoff,
            api_campaign_ids=api_campaign_ids,
            result=result,
        )

    log.info(
        "Discovery kész: client_id=%s | inserted=%d updated=%d deactivated=%d errors=%d",
        client_id,
        result["inserted"],
        result["updated"],
        result["deactivated"],
        len(result["errors"]),
    )
    return result


# ---------------------------------------------------------------------------
# Platform-absztrakció
# ---------------------------------------------------------------------------

def _make_platform_client(platform: str) -> Any:
    """Platform-specifikus API kliens (singleton) előállítása.

    Raises:
        RuntimeError — hiányzó token / nem telepített SDK (a kliens dobja)
        ValueError   — ismeretlen platform
    """
    if platform == "meta":
        return MetaAdsClient.get_instance()
    if platform == "google":
        return GoogleAdsClient.get_instance()
    raise ValueError(f"Ismeretlen platform: {platform!r}")


def _fetch_campaigns(
    platform: str,
    client: Any,
    ext_account_id: str,
) -> list[dict[str, Any]]:
    """Platform API kampányhívás + normalizálás közös formátumra.

    Közös (platform-független) formátum:
        {"ext_campaign_id": str, "name": str, "status": str}

    A Meta a "id"/"created_time", a Google a "external_campaign_id"/"created_at"
    kulcsokat adja vissza — ez a függvény egységesíti őket a discovery számára.
    """
    if platform == "meta":
        raw = client.get_campaigns(ext_account_id)
        return [
            {
                "ext_campaign_id": str(c["id"]),
                "name": c["name"],
                "status": c.get("status", "UNKNOWN"),
            }
            for c in raw
        ]
    if platform == "google":
        raw = client.get_campaigns(ext_account_id)
        return [
            {
                "ext_campaign_id": str(c["external_campaign_id"]),
                "name": c["name"],
                "status": c.get("status", "UNKNOWN"),
            }
            for c in raw
        ]
    raise ValueError(f"Ismeretlen platform: {platform!r}")


# ---------------------------------------------------------------------------
# Belső segédfüggvények (DB szinkron)
# ---------------------------------------------------------------------------

def _upsert_campaign(
    client_id: int,
    db_account_id: int,
    ext_campaign_id: str,
    api_campaign: dict[str, Any],
    now: datetime,
    result: dict[str, Any],
) -> None:
    """Egy kampány INSERT vagy UPDATE a DB-be (platform-független).

    ÚJ kampány beszúrásakor örökli a kliens-szintű hozzárendeléseket
    (15. lépés) — így a discovery után felfedezett kampányok automatikusan
    a megfelelő OM-ekhez kerülnek.
    """
    sb = get_supabase()

    # Létezik-e már?
    existing_res = (
        sb.table("campaigns")
        .select("id, platform_status, last_seen_at")
        .eq("ad_account_id", db_account_id)
        .eq("external_campaign_id", ext_campaign_id)
        .limit(1)
        .execute()
    )

    status = api_campaign.get("status", "UNKNOWN")

    if not existing_res.data:
        # --- INSERT ---
        new_camp = campaigns_storage.create_campaign(
            ad_account_id=db_account_id,
            external_campaign_id=ext_campaign_id,
            name=api_campaign["name"],
            platform_status=status,
            lifecycle_state="new",
            is_monitored=True,
        )
        result["inserted"] += 1
        log.info(
            "Discovery: ÚJ kampány → #%s '%s' (fiók_db=%s, status=%s)",
            ext_campaign_id, api_campaign["name"], db_account_id, status,
        )
        # Hozzárendelés-öröklés az új kampányra: elsődlegesen FIÓK-szintű
        # (21. lépés), majd a megmaradt kliens-szintű (visszafelé kompatibilis).
        # A hiba nem buktathatja meg a discovery-t (fault isolation).
        try:
            n_acct = account_assignments_storage.inherit_account_assignments_for_campaign(
                db_account_id, new_camp["id"]
            )
            n_client = assignments_storage.inherit_client_assignments_for_campaign(
                client_id, new_camp["id"]
            )
            if n_acct or n_client:
                log.info(
                    "Discovery: öröklött hozzárendelés az új kampányra #%s "
                    "(fiók=%d, kliens=%d)",
                    new_camp["id"], n_acct, n_client,
                )
        except Exception:  # noqa: BLE001
            log.exception(
                "Discovery: hozzárendelés-öröklés hiba (kampány #%s)", new_camp["id"]
            )
    else:
        # --- UPDATE ---
        db_id: int = existing_res.data[0]["id"]
        campaigns_storage.update_campaign_status(
            campaign_id=db_id,
            status=status,
            last_seen_at=now,
        )
        result["updated"] += 1
        log.debug(
            "Discovery: FRISSÍTVE kampány #%s (db_id=%s, status=%s)",
            ext_campaign_id, db_id, status,
        )


def _deactivate_stale(
    db_account_id: int,
    stale_cutoff: datetime,
    api_campaign_ids: set[str],
    result: dict[str, Any],
) -> None:
    """Soft-delete azokra a kampányokra, amelyeket nem láttunk 24+ óra óta."""
    sb = get_supabase()

    # Összes monitored kampány lekérése ehhez a fiókhoz
    monitored_res = (
        sb.table("campaigns")
        .select("id, external_campaign_id, last_seen_at")
        .eq("ad_account_id", db_account_id)
        .eq("is_monitored", True)
        .execute()
    )

    for row in (monitored_res.data or []):
        # Ha a most látott API kampányok között szerepel, hagyjuk
        if row["external_campaign_id"] in api_campaign_ids:
            continue

        # Ha a last_seen_at régebbi mint a cutoff → soft-delete
        last_seen_raw = row.get("last_seen_at")
        if last_seen_raw is None:
            continue
        try:
            last_seen = datetime.fromisoformat(last_seen_raw.replace("Z", "+00:00"))
        except ValueError:
            continue

        if last_seen < stale_cutoff:
            campaigns_storage.soft_delete_campaign(row["id"])
            result["deactivated"] += 1
            log.info(
                "Discovery: SOFT-DELETE kampány db_id=%s (last_seen=%s)",
                row["id"], last_seen_raw,
            )
