"""
Bulk onboarding CSV-ből (17. lépés).

71 Meta + 28 Google ügyfél bekötése egy lépésben, CSV fájlokból. Soronként:
  1. ügyfél upsert (név alapján; egy klienshez TÖBB sor = több fiók)
  2. hirdetési fiók regisztrálása (normalizált external id, idempotens)
  3. discovery (hacsak --skip-discovery)
  4. log: "✅ {name}: {X} kampány felfedezve"

CSV formátum (21. lépés — contact_email NÉLKÜL):
    data/clients_meta.csv     → client_name,meta_account_id
    data/clients_google.csv   → client_name,google_customer_id

Egy klienshez több fiók ugyanazon a platformon = több sor azonos client_name-mel
(mind külön ad_account, a kliens alá vonva). Az OM-eket később Discordon
rendelik hozzá (`/account assign`).

Futtatás:
    python -m scripts.bulk_onboard --meta data/clients_meta.csv --google data/clients_google.csv
    python -m scripts.bulk_onboard --dry-run --meta data/clients_meta.csv
    python -m scripts.bulk_onboard --skip-discovery --meta data/clients_meta.csv

Visszatérési kód: 0 = nem volt hiba, 1 = volt legalább egy hiba.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys

from src.config import get_config  # noqa: F401 — .env betöltése
from src.monitoring.discovery import discover_campaigns_for_client
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import clients as clients_storage
from src.utils.logging import get_logger

log = get_logger(__name__)

_REQUIRED = {
    "meta": ("client_name", "meta_account_id"),
    "google": ("client_name", "google_customer_id"),
}
_ID_COLUMN = {"meta": "meta_account_id", "google": "google_customer_id"}


def _read_text(path: str) -> str:
    """CSV szöveg beolvasása kódolás-toleránsan (Excel/Windows gyakran cp1250).

    Sorrend: utf-8-sig → cp1250 → latin-1 (utóbbi minden bájtot dekódol, így
    sosem dob UnicodeDecodeError-t). Az ügyfélnevek ékezetei így nem törnek el.
    """
    for enc in ("utf-8-sig", "cp1250", "latin-1"):
        try:
            with open(path, encoding=enc, newline="") as fh:
                return fh.read()
        except UnicodeDecodeError:
            continue
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()


def _read_csv(path: str, platform: str) -> list[dict[str, str]]:
    """CSV beolvasása + fejléc-validáció. Üres client_name-ű sorok kimaradnak.

    Az extra oszlopok (pl. egy maradék 'om' fejléc) figyelmen kívül maradnak.
    """
    required = _REQUIRED[platform]
    reader = csv.DictReader(io.StringIO(_read_text(path)))
    header = reader.fieldnames or []
    missing = [c for c in required if c not in header]
    if missing:
        raise ValueError(
            f"Hiányzó oszlop(ok) a {path} fájlban: {missing} (fejléc: {header})"
        )
    rows = []
    for row in reader:
        if not (row.get("client_name") or "").strip():
            continue
        rows.append({k: (v or "").strip() for k, v in row.items() if k is not None})
    return rows


def _upsert_client(name: str) -> tuple[dict, bool]:
    """Ügyfél upsert név alapján (contact_email nélkül — 21. lépés).

    Idempotens: meglévő névnél a meglévő sort adja vissza (created=False), így
    ugyanazon client_name-mel több CSV-sor mind ugyanahhoz a klienshez köt.
    """
    existing = clients_storage.get_client_by_name(name)
    if existing:
        return existing, False
    return clients_storage.create_client(name), True


def _process(path: str, platform: str, *, dry_run: bool, skip_discovery: bool) -> dict[str, int]:
    rows = _read_csv(path, platform)
    id_col = _ID_COLUMN[platform]
    print(f"\n=== {platform.upper()} — {path} ({len(rows)} sor) ===")

    stats = {"clients": 0, "accounts": 0, "campaigns": 0, "errors": 0}
    for row in rows:
        name = row["client_name"]
        raw_id = row.get(id_col, "")
        norm_id = ad_accounts_storage.normalize_external_account_id(platform, raw_id)

        if not raw_id:
            print(f"  ⚠️ {name}: hiányzó {id_col} — kihagyva")
            stats["errors"] += 1
            continue

        if dry_run:
            disc = "" if skip_discovery else " + discovery"
            print(f"  [DRY] client='{name}' {platform}=`{norm_id}`{disc}")
            continue

        try:
            client, created = _upsert_client(name)
            if created:
                stats["clients"] += 1
            _account, acc_created = ad_accounts_storage.get_or_create_ad_account(
                client["id"], platform, norm_id
            )
            if acc_created:
                stats["accounts"] += 1

            if skip_discovery:
                print(f"  ✅ {name}: ügyfél + fiók kész (discovery kihagyva)")
            else:
                result = discover_campaigns_for_client(client["id"])
                stats["campaigns"] += result["inserted"]
                extra = (f" (updated={result['updated']}, errors={len(result['errors'])})"
                         if (result["updated"] or result["errors"]) else "")
                print(f"  ✅ {name}: {result['inserted']} kampány felfedezve{extra}")
        except Exception as exc:  # noqa: BLE001 — egy sor hibája ne állítsa le a többit
            stats["errors"] += 1
            log.exception("Bulk onboard hiba (%s)", name)
            print(f"  ❌ {name}: {exc}")

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk onboarding CSV-ből (Meta/Google).")
    parser.add_argument("--meta", metavar="CSV", help="Meta CSV (client_name,meta_account_id)")
    parser.add_argument("--google", metavar="CSV", help="Google CSV (client_name,google_customer_id)")
    parser.add_argument("--dry-run", action="store_true", help="DB írás nélkül, csak listázás")
    parser.add_argument("--skip-discovery", action="store_true", help="Csak ügyfél + fiók, discovery nélkül")
    args = parser.parse_args()

    if not args.meta and not args.google:
        parser.error("Legalább a --meta vagy a --google fájlt add meg.")

    print("=" * 55)
    print("  PPC Monitor — Bulk onboarding" + ("  [DRY-RUN]" if args.dry_run else ""))
    print("=" * 55)

    totals = {"clients": 0, "accounts": 0, "campaigns": 0, "errors": 0}
    for path, platform in ((args.meta, "meta"), (args.google, "google")):
        if not path:
            continue
        try:
            stats = _process(path, platform, dry_run=args.dry_run, skip_discovery=args.skip_discovery)
        except FileNotFoundError:
            print(f"  ❌ Nincs ilyen fájl: {path}")
            totals["errors"] += 1
            continue
        except ValueError as exc:
            print(f"  ❌ {exc}")
            totals["errors"] += 1
            continue
        for k in totals:
            totals[k] += stats.get(k, 0)

    print("\n" + "=" * 55)
    print(f"  ÖSSZESÍTŐ: {totals['clients']} új ügyfél, {totals['accounts']} új fiók, "
          f"{totals['campaigns']} kampány, {totals['errors']} hiba")
    print("=" * 55)
    return 1 if totals["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
