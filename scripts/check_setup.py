"""
Setup ellenőrző szkript.

Lefuttatva ellenőrzi, hogy a környezet helyesen van-e beállítva:
  - betöltődnek-e a kötelező környezeti változók
  - él-e a Supabase kapcsolat
  - léteznek-e a táblák

Használat:
    python -m scripts.check_setup
"""
from __future__ import annotations

import sys


def main() -> int:
    # Windows default console (cp1252) can't render Hungarian/Unicode output.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("PPC Monitor — Setup ellenőrzés")
    print("=" * 60)

    # 1. Konfiguráció betöltése
    print("\n[1/3] Környezeti változók betöltése...")
    try:
        from src.config import get_config
        config = get_config()
        print(f"  ✓ Supabase URL: {config.supabase_url[:30]}...")
        print(f"  ✓ Discord token: {'beállítva' if config.discord_bot_token else 'HIÁNYZIK'}")
        print(f"  ✓ Időzóna: {config.timezone}")
        print(f"  ✓ Csendes idő: {config.quiet_hours_start}:00 – {config.quiet_hours_end}:00")
    except Exception as e:
        print(f"  ✗ HIBA a konfiguráció betöltésekor: {e}")
        return 1

    # 2. Supabase kapcsolat
    print("\n[2/3] Supabase kapcsolat tesztelése...")
    try:
        from src.storage.supabase_client import get_supabase
        supabase = get_supabase()
        print("  ✓ Supabase kliens létrejött")
    except Exception as e:
        print(f"  ✗ HIBA a Supabase kapcsolatnál: {e}")
        return 1

    # 3. Táblák ellenőrzése
    print("\n[3/3] Adatbázis táblák ellenőrzése...")
    expected_tables = [
        "users", "clients", "ad_accounts", "campaigns",
        "campaign_kpis", "assignments", "alert_rules",
        "mutes", "alerts", "audit_log",
    ]
    try:
        from src.storage.supabase_client import get_supabase
        supabase = get_supabase()
        missing = []
        for table in expected_tables:
            try:
                # Egy minimális lekérdezés, ami ellenőrzi hogy a tábla létezik
                supabase.table(table).select("*").limit(1).execute()
                print(f"  ✓ {table}")
            except Exception:
                print(f"  ✗ {table} — HIÁNYZIK vagy nem elérhető")
                missing.append(table)

        if missing:
            print(f"\n  ⚠ Hiányzó táblák: {', '.join(missing)}")
            print("  → Futtasd le a migrációkat a supabase/migrations/ mappából.")
            return 1
    except Exception as e:
        print(f"  ✗ HIBA a táblák ellenőrzésekor: {e}")
        return 1

    print("\n" + "=" * 60)
    print("✓ Minden rendben! A rendszer készen áll a fejlesztésre.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
