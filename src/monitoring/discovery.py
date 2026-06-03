"""
Kampány auto-discovery — Meta Ads.

A discovery lekéri az ügyfél Meta hirdetési fiókjaihoz tartozó összes
kampányt a Meta API-ból, majd szinkronizálja a `campaigns` táblával:

  NEW campaign   → INSERT (discovered_at = now, is_monitored = true)
  EXISTING       → UPDATE (platform_status, last_seen_at)
  NOT SEEN 24h   → soft delete (is_monitored = false)

Szinkronizálási logika:
  1. Ügyfélhez rendelt Meta ad_accounts lekérdezése (ad_accounts tábla)
  2. Mindegyik fiókra: Meta API-ból kampányok lekérése
  3. DB upsert: új kampány INSERT, meglévő UPDATE (status + last_seen_at)
  4. Stale kampányok (24+ óra nem látott) soft-delete

Hibakezelés:
  - Egy fiók API hibája NEM állítja le a teljes discovery-t;
    a hiba az errors listába kerül, a többi fiók folytatódik.
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

from src.integrations.meta_ads import MetaAdsClient
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import campaigns as campaigns_storage
from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

log = get_logger(__name__)

# Ennyi óra elteltével kerül soft-delete-be egy kampány, ha nem látjuk az API-ban
_STALE_HOURS = 24


def discover_campaigns_for_client(client_id: int) -> dict[str, Any]:
    """Kampány-discovery egy ügyfél összes Meta ad accountjára.

    Paraméterek:
        client_id — ügyfél belső DB ID-ja

    Visszatérés:
        {
            "inserted":    int,      — DB-be újonnan felvett kampányok
            "updated":     int,      — frissített kampányok (status, last_seen_at)
            "deactivated": int,      — soft-deleted (24h+ nem látott) kampányok
            "errors":      list,     — [{"account": "act_xxx", "error": "..."}]
        }
    """
    result: dict[str, Any] = {
        "inserted": 0,
        "updated": 0,
        "deactivated": 0,
        "errors": [],
    }

    # 1) Meta ad_accountok lekérdezése ehhez az ügyfélhez
    accounts = ad_accounts_storage.get_ad_accounts_for_client(
        client_id,
        platform="meta",
        active_only=True,
    )
    if not accounts:
        log.info(
            "Discovery: nincs aktív Meta ad_account ehhez az ügyfélhez (client_id=%s)",
            client_id,
        )
        return result

    log.info(
        "Discovery indítva: client_id=%s, %d Meta fiók",
        client_id, len(accounts),
    )

    # Meta kliens (singleton)
    try:
        meta_client = MetaAdsClient.get_instance()
    except RuntimeError as exc:
        log.error("MetaAdsClient inicializálási hiba: %s", exc)
        result["errors"].append({"account": "all", "error": str(exc)})
        return result

    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(hours=_STALE_HOURS)

    for account in accounts:
        ext_account_id: str = account["external_account_id"]
        db_account_id: int = account["id"]

        log.info(
            "Discovery: fiók=%s (db_id=%s) feldolgozása…",
            ext_account_id, db_account_id,
        )

        # 2) Meta API hívás
        try:
            api_campaigns = meta_client.get_campaigns(ext_account_id)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Discovery: API hiba fiók=%s: %s",
                ext_account_id, exc,
            )
            result["errors"].append({
                "account": ext_account_id,
                "error": str(exc),
            })
            continue  # következő fiók

        api_campaign_ids: set[str] = set()

        # 3) Upsert: minden API kampányra
        for api_c in api_campaigns:
            ext_campaign_id: str = api_c["id"]
            api_campaign_ids.add(ext_campaign_id)

            try:
                _upsert_campaign(
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
# Belső segédfüggvények
# ---------------------------------------------------------------------------

def _upsert_campaign(
    db_account_id: int,
    ext_campaign_id: str,
    api_campaign: dict[str, Any],
    now: datetime,
    result: dict[str, Any],
) -> None:
    """Egy Meta kampány INSERT vagy UPDATE a DB-be."""
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
        campaigns_storage.create_campaign(
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
