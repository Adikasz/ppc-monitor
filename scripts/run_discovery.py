"""
Kézi kampány-discovery trigger.

Futtatás:
    python -m scripts.run_discovery --client-id 1
    python -m scripts.run_discovery --client-name Stopvill
    python -m scripts.run_discovery          (osszes aktiv ugyfél)

Visszaterési kód:
    0 — sikeres (részleges hibák megengedett)
    1 — fatális hiba (pl. token nincs, DB nem él)
"""
from __future__ import annotations

import argparse
import sys

from src.config import get_config  # noqa: F401  — .env betöltése
from src.monitoring.discovery import discover_campaigns_for_client
from src.storage import clients as clients_storage
from src.utils.logging import get_logger

log = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Meta Ads kampány auto-discovery kézi triggerelése",
    )
    parser.add_argument("--client-id", type=int, metavar="ID",
                        help="Ügyfél belső DB ID-ja")
    parser.add_argument("--client-name", type=str, metavar="NÉV",
                        help="Ügyfél neve (pontosan, ahogy a DB-ben szerepel)")
    return parser


def _run_for_client(client: dict) -> dict:
    label = f"#{client['id']} — {client['name']}"
    print(f"\n  Discovery: {label}")
    try:
        result = discover_campaigns_for_client(client["id"])
    except Exception as exc:  # noqa: BLE001
        print(f"  FATALIS HIBA: {exc}")
        log.exception("Discovery fatális hiba: client_id=%s", client["id"])
        return {"fatal": True, "error": str(exc)}

    print(f"  Uj kampanyok:  {result['inserted']}")
    print(f"  Frissitett:    {result['updated']}")
    print(f"  Deaktivalt:    {result['deactivated']}")
    if result["errors"]:
        print(f"  Hibak ({len(result['errors'])}):")
        for err in result["errors"]:
            acct = err.get("account", "?")
            camp = err.get("campaign", "")
            msg = err.get("error", "?")
            target = f"fok={acct}" + (f", kampany={camp}" if camp else "")
            print(f"    - {target}: {msg}")
    return result


def main() -> int:
    args = _build_parser().parse_args()

    print("=" * 55)
    print("  PPC Monitor — Meta Ads kampany discovery")
    print("=" * 55)

    if args.client_id:
        client = clients_storage.get_client(args.client_id)
        if client is None:
            print(f"\n  Nem talalhato ugyfél: #{args.client_id}")
            return 1
        targets = [client]

    elif args.client_name:
        client = clients_storage.get_client_by_name(args.client_name.strip())
        if client is None:
            print(f"\n  Nem talalhato ugyfél: '{args.client_name}'")
            return 1
        targets = [client]

    else:
        targets = clients_storage.list_clients(active_only=True)
        if not targets:
            print("\n  Nincs aktiv ugyfél az adatbazisban.")
            return 0
        print(f"\n  {len(targets)} aktiv ugyfél discoveryje...")

    total_i = total_u = total_d = 0
    total_errors: list = []

    for client in targets:
        res = _run_for_client(client)
        if res.get("fatal"):
            return 1
        total_i += res.get("inserted", 0)
        total_u += res.get("updated", 0)
        total_d += res.get("deactivated", 0)
        total_errors.extend(res.get("errors", []))

    print("\n" + "=" * 55)
    print(f"  OSSZESITO:")
    print(f"    Uj kampanyok:  {total_i}")
    print(f"    Frissitett:    {total_u}")
    print(f"    Deaktivalt:    {total_d}")
    print(f"    Hibak:         {len(total_errors)}")
    print("=" * 55)
    return 0


if __name__ == "__main__":
    sys.exit(main())
