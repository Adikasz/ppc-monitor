# PPC AI Kampánymonitor

AI-alapú PPC kampánymonitoring rendszer, ami a Meta Ads és Google Ads
kampányokat figyeli, anomáliákat észlel, és Discord-on értesíti a felelős
account managereket.

## Mit csinál a rendszer

- **Auto-discovery** — automatikusan felfedezi a kampányokat a Meta/Google fiókokból
- **Óránkénti monitoring** — lekéri a teljesítményadatokat és összeveti a beállított KPI-okkal
- **Anomália-detektálás** — CPA-ugrás, ROAS-esés, büdzsé-elfogyás, fiókszintű hibák
- **Célzott riasztás** — csak az adott ügyfélhez/kampányhoz rendelt személy kap értesítést
- **3 súlyossági szint** — 🔴 CRITICAL, 🟡 WARNING, 🟢 INSIGHT
- **Csendes idő** — 18:00–09:00 között és hétvégén nincs ping; reggel 9-kor / hétfőn összefoglaló
- **Némítás** — kampányonként X napra letiltható a riasztás (pl. tanulási fázis), majd visszakapcsolható
- **ClickUp integráció** — CRITICAL esetén automatikus task a felelősnek

## Architektúra

```
Railway (óránkénti cron)
  │
  ├─ Meta Ads API ──┐
  ├─ Google Ads API ┤→ Monitoring motor → Anomália-detektor
  │                 │                          │
  └─ Supabase ──────┘                          ↓
     (konfiguráció,                      Routing (kihez tartozik?)
      KPI-ok, állapot)                         │
                                               ↓
                                    Discord bot ─→ felelős AM csatornája
                                          │
                                          └─→ ClickUp task (CRITICAL)
```

## Tech stack

| Réteg | Eszköz |
|-------|--------|
| Futtatókörnyezet | Railway (cron + always-on bot) |
| Adatbázis | Supabase (PostgreSQL) |
| Bot / felület | Discord (discord.py) |
| AI | Claude Haiku (anomália-szövegezés, insight) |
| Hirdetési adat | Meta Ads API + Google Ads API |
| Tasksok | ClickUp API |

## Fejlesztői környezet beállítása

1. Klónozd a repót és lépj be:
   ```bash
   git clone <repo-url>
   cd mymins-ppc-monitor
   ```

2. Hozz létre virtuális környezetet és telepítsd a függőségeket:
   ```bash
   python -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Másold a `.env.example` fájlt `.env` néven, és töltsd ki a kulcsokat:
   ```bash
   cp .env.example .env
   ```

4. Futtasd a Supabase migrációkat (lásd `supabase/migrations/`).

5. Indítsd a botot:
   ```bash
   python -m src.bot.main
   ```

## Projektstruktúra

```
src/
  bot/            Discord bot és slash parancsok
    commands/     Egyes parancs-csoportok
  integrations/   Külső API kliensek (Meta, Google, ClickUp, Claude)
  monitoring/     Monitoring motor és anomália-detektálás
  routing/        Riasztás-célzás (kihez megy az értesítés)
  storage/        Supabase adatbázis-réteg
  utils/          Segédfüggvények (idő, csendes mód, logging)
supabase/
  migrations/     SQL séma-migrációk
scripts/          Egyszeri/karbantartó szkriptek
tests/            Tesztek
```

## Fejlesztési állapot

- [x] 1. lépés — Projektváz + Supabase séma
- [ ] 2. lépés — Discord bot alap
- [ ] 3. lépés — Supabase bekötés + alap parancsok
- [ ] 4. lépés — Auto-discovery (Meta + Google)
- [ ] 5. lépés — Monitoring motor + anomália-detektálás
- [ ] 6. lépés — Riasztások + routing + ClickUp
- [ ] 7. lépés — Csendes idő + összefoglalók + némítás
- [ ] 8. lépés — Railway deploy

## Licenc

Proprietary — PlanSmart. Minden jog fenntartva.
