# Supabase migrációk futtatása

Ez a mappa tartalmazza az adatbázis-séma migrációkat. Sorrendben kell
lefuttatni őket (a fájlnevek elején lévő szám a sorrend).

## A legegyszerűbb mód — Supabase SQL Editor

1. Menj a Supabase projekted felületére
2. Bal oldali menü → **SQL Editor**
3. **New query**
4. Másold be a `0001_initial_schema.sql` teljes tartalmát, és **Run**
5. Ismételd a `0002_default_alert_rules.sql` fájllal

Ennyi. A táblák és az alapértelmezett szabályok létrejönnek.

## Ellenőrzés

A migrációk után futtasd a setup-ellenőrzőt a projekt gyökeréből:

```bash
python -m scripts.check_setup
```

Ha minden zöld pipa, készen állsz a 2. lépésre (Discord bot).

## Migrációk sorrendje

| Fájl | Mit csinál |
|------|-----------|
| `0001_initial_schema.sql` | Az összes tábla létrehozása (users, clients, campaigns, stb.) |
| `0002_default_alert_rules.sql` | Alapértelmezett anomália-küszöbök beszúrása |

## Új migráció hozzáadása

Ha később módosul a séma, ne írd át a meglévő fájlokat — hozz létre újat
növekvő sorszámmal (pl. `0003_add_something.sql`). Így a változások
nyomon követhetők és reprodukálhatók.
