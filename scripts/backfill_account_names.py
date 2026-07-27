"""
Visszamenőleges account_name kitöltés — a napi összefoglaló olvasható fiók-
megkülönböztetéséhez (lásd src.monitoring.summary._multi_account_label).

Háttér: a `/account add` és `/client onboard` mostantól automatikusan menti
a fiók API-ból kapott nevét (`ad_accounts.account_name`), de a REGI (a
javítás előtt felvett) fiókoknál ez a mező NULL. Amíg NULL, a napi
összefoglaló többfiókos klienseknél a technikai azonosítót (act_... / szám)
mutatja megkülönböztetőnek — ez a script ezt tölti ki visszamenőleg.

Forrás-sorrend fiókonként:
    1. account_catalog (a `/me/adaccounts` / Google `customer_client` cache-elt
       listája) — olcsó, egy API hívás / platform fedezi a legtöbb fiókot.
    2. Ha egy fiók nincs a katalógusban (pl. ügynökségi/business hozzáférésű
       Meta fiók), közvetlen egyedi lekérdezés
       (MetaAdsClient.get_account_name / GoogleAdsClient.get_account_name).

KORLÁT (élő méréssel igazolva 2026-07-27-én): az 5 ügynökségi Meta fiók
mindegyikére 403-at ad MINDKÉT forrás (a katalógus ÉS a közvetlen lekérdezés
is) — a tokennek semennyi mezőt nem enged olvasni ezekről. Ezekre az
account_name NULL MARAD; a napi összefoglaló ekkor is a technikai ID-re esik
vissza (nem regresszió — ugyanaz, mint most). Ha ezt is meg akarjuk oldani,
az admin Business Manager hozzáférését kellene bővíteni ads_read/basic
jogosultsággal — ez technikai korláton túli, üzleti döntés.

Futtatás:
    python -m scripts.backfill_account_names --dry-run   # csak riport, nem ír
    python -m scripts.backfill_account_names             # éles futás, megerősítést kér
    python -m scripts.backfill_account_names --yes        # éles, megerősítés nélkül

Visszatérési kód:
    0 — sikeres (részleges hiányok megengedettek, lásd a végső riportot)
    1 — fatális hiba (DB nem él, vagy egyik platform kliens sem inicializálható)
"""
from __future__ import annotations

import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.config import get_config  # noqa: F401 — .env betöltése
from src.integrations import account_catalog
from src.storage import ad_accounts as ad_accounts_storage
from src.utils.logging import get_logger

log = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ad_accounts.account_name visszamenőleges kitöltése API-ból",
    )
    parser.add_argument("--dry-run", action="store_true",
                         help="Csak megmutatja mit írna, DB-be nem ír")
    parser.add_argument("--yes", action="store_true",
                         help="Éles futás megerősítés-kérés nélkül")
    return parser


def _direct_fetch(platform: str, external_account_id: str) -> str | None:
    """Egyedi (nem-katalógus) API lekérdezés — csak akkor hívjuk, ha a fiók
    nincs a cache-elt katalógusban. Hiba esetén None, sosem dob."""
    try:
        if platform == "meta":
            from src.integrations.meta_ads import MetaAdsClient
            return MetaAdsClient.get_instance().get_account_name(external_account_id)
        if platform == "google":
            from src.integrations.google_ads import GoogleAdsClient
            return GoogleAdsClient.get_instance().get_account_name(external_account_id)
    except Exception as exc:  # noqa: BLE001 — pl. RuntimeError hiányzó tokenre
        log.warning(
            "Direkt névlekérés hiba (platform=%s, fiók=%s): %s",
            platform, external_account_id, exc,
        )
    return None


def main() -> int:
    args = _build_parser().parse_args()

    all_accounts = ad_accounts_storage.list_ad_accounts()
    missing = [a for a in all_accounts if not a.get("account_name")]

    print(f"Összes fiók: {len(all_accounts)} | account_name hiányzik: {len(missing)}")
    if not missing:
        print("Nincs tennivaló — minden fióknak van account_name-je.")
        return 0

    if not args.dry_run and not args.yes:
        answer = input(
            f"\n{len(missing)} fiók account_name-jét próbáljuk kitölteni API-ból, "
            f"majd DB-be írjuk. Folytatod? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes", "i", "igen"):
            print("Megszakítva.")
            return 0

    # Katalógus platformonként EGYSZER lekérve (cache-elve amúgy is, de itt
    # explicit force_refresh, hogy friss legyen egy admin-script futásakor).
    catalogs: dict[str, dict[str, str]] = {}
    for platform in ("meta", "google"):
        accounts = account_catalog.get_accounts(platform, force_refresh=True)
        catalogs[platform] = {str(a["id"]): a["name"] for a in accounts}
        print(f"Katalógus [{platform}]: {len(accounts)} fiók API-ból")

    updated: list[str] = []
    unresolved: list[str] = []

    for acct in missing:
        platform = acct["platform"]
        ext_id = acct["external_account_id"]
        label = f"#{acct['id']} {platform}/{ext_id}"

        name = catalogs.get(platform, {}).get(ext_id)
        source = "katalógus"
        if not name:
            name = _direct_fetch(platform, ext_id)
            source = "direkt lekérdezés"

        if not name:
            unresolved.append(label)
            print(f"  ⚠️  {label} — nem sikerült nevet szerezni (katalógus és direkt is 0 találat)")
            continue

        print(f"  ✅ {label} → {name!r} ({source})")
        if not args.dry_run:
            ad_accounts_storage.set_account_name(acct["id"], name)
        updated.append(label)

    print(
        f"\n{'[DRY-RUN] ' if args.dry_run else ''}"
        f"Kész: {len(updated)}/{len(missing)} fiók nevesítve, "
        f"{len(unresolved)} maradt hiányos (ügynökségi/nem elérhető hozzáférés)."
    )
    if unresolved:
        print(
            "\nHiányos fiókok (a napi összefoglaló ezekre a technikai ID-t "
            "mutatja továbbra is):"
        )
        for label in unresolved:
            print(f"  - {label}")
        print(
            "\nEzek üzleti (Business Manager jogosultság) korlátba futnak, nem "
            "kód-hibába — lásd a modul-docstring."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
