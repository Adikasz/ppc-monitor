"""
Google Ads kampany-discovery — RAILWAY-en futtatando.

A Google fiokok (platform='google') discovery-je lokalisan nem fut le, mert a
google-ads SDK Python 3.14 alatt nem telepul. Railway-en (Python 3.12) viszont
igen — ezt a scriptet OTT, a shell-bol futtasd:

    python -m scripts.run_google_discovery

Mit csinal:
    1. Lekeri az osszes AKTIV Google hirdetesi fiokot (platform='google').
    2. Az erintett ugyfelekre lefuttatja a discover_campaigns_for_client-et.
       A discovery UGYFEL-szintu (a kliens minden fiokjat nezi), ezert client_id
       szerint dedupolunk — egy kliens tobb Google fiokja eseten is csak egyszer
       fut. (A kliens esetleges Meta fiokjainak ujra-szinkronja idempotens UPDATE,
       igy artalmatlan.)
    3. Ugyfelenkent kiirja: "{client_name}: {inserted} kampany felfedezve".
    4. Ha a google-ads SDK nincs telepitve -> baratsagos hibauzenet, exit 2.

Visszateresi kod:
    0 — lefutott (reszleges hibak megengedettek)
    1 — fatalis hiba (nincs aktiv Google fiok / DB nem el)
    2 — a google-ads SDK nincs telepitve (rossz kornyezet — lokalis Python 3.14)
"""
from __future__ import annotations

import sys

from src.config import get_config  # noqa: F401 — .env betoltese import-mellekhataskent
from src.monitoring.discovery import discover_campaigns_for_client
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import clients as clients_storage
from src.utils.logging import get_logger

log = get_logger(__name__)


def _sdk_available() -> bool:
    """True, ha a google-ads SDK importalhato ebben a kornyezetben."""
    try:
        import google.ads.googleads  # noqa: F401
    except ImportError:
        return False
    return True


def main() -> int:
    print("=" * 55)
    print("  PPC Monitor - Google Ads kampany discovery")
    print("=" * 55)

    # SDK-pre-check: ASCII uzenet (ez tipikusan lokalisan, Windows-konzolon fut le).
    if not _sdk_available():
        print(
            "\n  A google-ads SDK nincs telepitve ebben a kornyezetben.\n"
            "  Ez a script RAILWAY-en (Python 3.12) futtatando, ahol az SDK\n"
            "  elerheto - lokalisan (Python 3.14) nincs kompatibilis wheel.\n\n"
            "  Railway shell:  python -m scripts.run_google_discovery"
        )
        return 2

    accounts = ad_accounts_storage.list_ad_accounts(active_only=True)
    google_accounts = [a for a in accounts if a.get("platform") == "google"]
    if not google_accounts:
        print("\n  Nincs aktiv Google (platform='google') fiok az adatbazisban.")
        return 1

    # Ugyfel-szintu discovery -> dedup client_id szerint (sorrendtarto).
    client_ids: list[int] = []
    seen: set[int] = set()
    for a in google_accounts:
        cid = a.get("client_id")
        if cid is not None and cid not in seen:
            seen.add(cid)
            client_ids.append(cid)

    print(
        f"\n  {len(google_accounts)} aktiv Google fiok, "
        f"{len(client_ids)} ugyfel discoveryje...\n"
    )

    total_i = total_u = total_d = 0
    total_errors: list = []
    fatal = False

    for cid in client_ids:
        client = clients_storage.get_client(cid)
        cname = (client or {}).get("name", f"#{cid}")
        try:
            res = discover_campaigns_for_client(cid)
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ {cname}: FATALIS HIBA — {exc}")
            log.exception("Google discovery fatalis hiba: client_id=%s", cid)
            fatal = True
            continue

        total_i += res.get("inserted", 0)
        total_u += res.get("updated", 0)
        total_d += res.get("deactivated", 0)
        errs = res.get("errors", [])
        total_errors.extend(errs)

        suffix = f" (frissitett: {res['updated']})" if res.get("updated") else ""
        if errs:
            suffix += f" [{len(errs)} hiba]"
        print(f"  ✅ {cname}: {res.get('inserted', 0)} kampany felfedezve{suffix}")

    print("\n" + "=" * 55)
    print("  OSSZESITO:")
    print(f"    Uj kampanyok:  {total_i}")
    print(f"    Frissitett:    {total_u}")
    print(f"    Deaktivalt:    {total_d}")
    print(f"    Hibak:         {len(total_errors)}")
    for err in total_errors[:20]:
        acct = err.get("account", "?")
        camp = err.get("campaign", "")
        target = f"fiok={acct}" + (f", kampany={camp}" if camp else "")
        print(f"      - {target}: {err.get('error', '?')}")
    print("=" * 55)

    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
