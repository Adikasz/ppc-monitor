# PPC AI Kampánymonitor — Master Plan

> **Projekt:** AI-alapú PPC kampánymonitoring rendszer Meta Ads és Google Ads integrációval
> **Cél:** Automatikus anomália-detektálás + Discord értesítés + ClickUp task-kezelés
> **Tech stack:** Python · Discord · Supabase · Railway · Claude API · ClickUp
> **Repo:** `Adikasz/ppc-monitor` (privát GitHub)

---

## 1. Rendszer áttekintő

### Mit csinál
- **Auto-discovery** — automatikusan felfedezi a kampányokat a Meta/Google fiókokból
- **Óránkénti monitoring** — lekéri a teljesítményadatokat, összehasonlítja a beállított KPI-okkal
- **Anomália-detektálás** — CPA-ugrás, ROAS-esés, büdzsé-elfogyás, fiókszintű hibák
- **Célzott riasztás** — csak az ügyfélhez/kampányhoz rendelt OM kap értesítést
- **3 súlyossági szint** — 🔴 CRITICAL · 🟡 WARNING · 🟢 INSIGHT
- **Csendes idő** — 18:00–09:00 + hétvégén nincs ping; reggel 9 / hétfőn összefoglaló
- **Némítás** — kampányonként X napra letiltható, majd visszakapcsolható
- **ClickUp integráció** — CRITICAL → automatikus task a felelős OM-nek

### Architektúra

```
Railway (óránkénti cron)
  │
  ├─ Meta Ads API ────┐
  ├─ Google Ads API ──┤→ Monitoring motor → Anomália-detektor
  │                   │                            │
  └─ Supabase ────────┘                            ↓
     (konfiguráció,                          Routing (kihez tartozik?)
      KPI-ok, állapot)                            │
                                                  ↓
                                    Discord bot ─→ felelős OM csatornája
                                          │
                                          └─→ ClickUp task (CRITICAL)
```

### Tech stack

| Réteg | Eszköz | Megjegyzés |
|-------|--------|------------|
| Futtatókörnyezet | Railway | óránkénti cron + always-on bot |
| Adatbázis | Supabase (PostgreSQL) | service_role kulccsal |
| Bot / felület | Discord (discord.py 2.4) | slash commands |
| AI | Claude Haiku 4.5 | anomália-szövegezés, insight |
| Hirdetési adat | Meta Ads API + Google Ads API | csak az ügyfél API kulcsai |
| Task-kezelés | ClickUp API | CRITICAL alertekhez |
| Fejlesztés | Python 3.14 + venv | VS Code + Claude Code |

### Üzleti döntések (Zoli pontosítása alapján)

| Téma | Döntés |
|------|--------|
| Jogosultság | **1 szint** — mindenki egyenrangú, bárki módosíthat |
| Riasztás célzás | **célzott** — csak a hozzárendelt OM kap értesítést |
| Új ügyfél onboarding | **auto-discovery** — rendszer felfedezi a kampányokat |
| Kampányszám / ügyfél | 1–6 tipikusan, 10 nap max onboarding |
| Némítás | kampányonként X napra letiltható, paranccsal visszakapcsolható |
| Csendes idő | 18:00–09:00 + hétvége; reggel 9 / hétfőn összefoglaló |
| CRITICAL/WARNING küszöb | **konfigurálható változó** — még egyeztetés alatt |
| Notification csatorna | Discord + ClickUp (CRITICAL) — **NEM email** |

---

## 2. Adatbázis séma (Supabase)

### Táblák (10 db)

#### `users` — Account managerek
```sql
id                bigint PK
discord_user_id   text UNIQUE NOT NULL
display_name      text NOT NULL
email             text
is_active         boolean DEFAULT true
created_at        timestamptz
updated_at        timestamptz
```
> Mindenki egyenrangú; a hozzárendelés dönti el ki mit kap.

#### `clients` — Ügyfelek
```sql
id                  bigint PK
name                text UNIQUE NOT NULL
is_active           boolean DEFAULT true
discord_channel_id  text             -- ide mennek a riasztások
notes               text
created_at          timestamptz
updated_at          timestamptz
```

#### `ad_accounts` — Hirdetési fiókok (Meta + Google)
```sql
id                    bigint PK
client_id             bigint FK → clients
platform              text CHECK ('meta' | 'google')
external_account_id   text NOT NULL    -- act_123... | 1234567890
account_name          text
is_active             boolean DEFAULT true
UNIQUE (platform, external_account_id)
```

#### `campaigns` — Kampányok (auto-discovery tölti)
```sql
id                     bigint PK
ad_account_id          bigint FK → ad_accounts
external_campaign_id   text NOT NULL
name                   text NOT NULL
campaign_type          text   -- google_brand_search, meta_conversion, stb.
is_monitored           boolean DEFAULT true
platform_status        text   -- ACTIVE, PAUSED, stb.
discovered_at          timestamptz
last_seen_at           timestamptz
UNIQUE (ad_account_id, external_campaign_id)
```

#### `campaign_kpis` — KPI célok (verziózott)
```sql
id                          bigint PK
campaign_id                 bigint FK → campaigns
target_roas                 numeric   -- %-ban
max_cpa                     numeric   -- Ft
max_cpl                     numeric   -- Ft
monthly_budget              numeric   -- Ft
target_ctr                  numeric   -- %-ban
max_cpc                     numeric   -- Ft
primary_conversion_event    text      -- "Purchase", "Lead"
is_active                   boolean   -- legutóbbi sor
set_by_discord_user_id      text
created_at                  timestamptz
```

#### `assignments` — OM hozzárendelések
```sql
id                              bigint PK
user_id                         bigint FK → users
client_id                       bigint FK → clients  (NULL ha kampány-szintű)
campaign_id                     bigint FK → campaigns (NULL ha ügyfél-szintű)
created_by_discord_user_id      text
created_at                      timestamptz
CHECK (client_id IS NOT NULL OR campaign_id IS NOT NULL)
```
> Ez dönti el ki kap riasztást.

#### `alert_rules` — Anomália-küszöbök (3-szintű öröklődés)
```sql
id                       bigint PK
scope                    text CHECK ('global' | 'campaign_type' | 'campaign')
campaign_type            text                  -- ha scope='campaign_type'
campaign_id              bigint FK             -- ha scope='campaign'
metric                   text NOT NULL         -- cpa_spike, roas_drop, stb.
warning_threshold        numeric
critical_threshold       numeric
direction                text CHECK ('above' | 'below')
is_active                boolean DEFAULT true
set_by_discord_user_id   text
```
> Öröklődés: `global < campaign_type < campaign` — legspecifikusabb nyer.

#### `mutes` — Némítások (X napra)
```sql
id                            bigint PK
campaign_id                   bigint FK → campaigns
muted_until                   timestamptz NOT NULL
reason                        text
is_active                     boolean DEFAULT true
created_by_discord_user_id    text
```

#### `alerts` — Riasztások naplója
```sql
id                            bigint PK
campaign_id                   bigint FK
client_id                     bigint FK
severity                      text CHECK ('critical' | 'warning' | 'insight')
metric                        text NOT NULL
observed_value                numeric
threshold_value               numeric
message                       text              -- Claude által generált
status                        text DEFAULT 'pending'
                              CHECK ('pending' | 'sent' | 'suppressed' | 'summarized')
routed_to_discord_user_id     text
discord_message_id            text
clickup_task_id               text
detected_at                   timestamptz
sent_at                       timestamptz
dedup_key                     text              -- deduplikációhoz
```

#### `audit_log` — Konfig változás napló
```sql
id                bigint PK
discord_user_id   text NOT NULL    -- ki csinálta
action            text NOT NULL    -- assign, set_kpi, mute, stb.
entity_type       text             -- client, campaign, kpi, rule, mute
entity_id         bigint
details           jsonb            -- a változás részletei
created_at        timestamptz
```

### Alapértelmezett küszöbök (seed)

| Metrika | WARNING | CRITICAL | Irány |
|---------|---------|----------|-------|
| `cpa_spike` | +25% | +50% | above |
| `roas_drop` | -20% | -40% | below |
| `budget_depleted` | 90% | 100% | above |
| `ctr_low` | -30% | -50% | below |
| `cpc_high` | +30% | +60% | above |
| `account_disabled` | — | 1 | above |
| `payment_error` | — | 1 | above |
| `ad_disapproved` | 1 | 3 | above |
| `learning_frozen` | 5 nap | 7 nap | above |

---

## 3. Mappastruktúra

```
ppc-monitor/
├── .env                              # környezeti változók (NEM committelt)
├── .env.example                      # template
├── .gitignore
├── README.md
├── requirements.txt                  # Python függőségek
├── railway.json                      # Railway deploy config
│
├── src/
│   ├── __init__.py
│   ├── config.py                     # Központi konfig (env betöltés)
│   │
│   ├── bot/                          # Discord bot
│   │   ├── __init__.py
│   │   ├── main.py                   # belépési pont
│   │   └── commands/                 # Slash commands (Cog-ok)
│   │       ├── __init__.py
│   │       ├── loader.py             # Cog automatikus betöltés
│   │       ├── ping.py               # /ping (teszt)
│   │       ├── clients.py            # /clients list, add, info
│   │       ├── assignments.py        # /assign, /unassign, /my-clients
│   │       ├── campaigns.py          # /campaign list, kpi, tag
│   │       ├── mutes.py              # /mute, /unmute
│   │       └── rules.py              # /rule list, set
│   │
│   ├── storage/                      # Supabase adatbázis-réteg
│   │   ├── __init__.py
│   │   ├── supabase_client.py        # singleton kliens
│   │   ├── clients.py                # clients tábla CRUD
│   │   ├── users.py                  # users tábla CRUD
│   │   ├── campaigns.py              # campaigns tábla CRUD
│   │   ├── kpis.py                   # campaign_kpis tábla CRUD
│   │   ├── assignments.py            # assignments tábla CRUD
│   │   ├── mutes.py                  # mutes tábla CRUD
│   │   ├── alerts.py                 # alerts tábla CRUD
│   │   ├── rules.py                  # alert_rules tábla CRUD
│   │   └── audit.py                  # audit_log írás
│   │
│   ├── integrations/                 # Külső API kliensek
│   │   ├── __init__.py
│   │   ├── meta_ads.py               # Meta Ads API
│   │   ├── google_ads.py             # Google Ads API
│   │   ├── claude_ai.py              # Anthropic Claude API
│   │   └── clickup.py                # ClickUp API
│   │
│   ├── monitoring/                   # Monitoring motor
│   │   ├── __init__.py
│   │   ├── discovery.py              # auto-discovery (új kampányok)
│   │   ├── fetcher.py                # adatlekérés Meta + Google
│   │   ├── detector.py               # anomália-detektor
│   │   ├── rules_engine.py           # küszöb-öröklődés (global → campaign)
│   │   └── scheduler.py              # APScheduler óránkénti futás
│   │
│   ├── routing/                      # Riasztás-célzás
│   │   ├── __init__.py
│   │   ├── router.py                 # kihez megy az értesítés
│   │   ├── dispatcher.py             # Discord + ClickUp küldés
│   │   └── summarizer.py             # csendes idő utáni összefoglaló
│   │
│   └── utils/
│       ├── __init__.py
│       ├── logging.py                # egységes logger
│       ├── timezone.py               # Europe/Budapest időkezelés
│       └── quiet_hours.py            # csendes idő logika
│
├── supabase/
│   └── migrations/
│       ├── 0001_initial_schema.sql       # 10 tábla létrehozása
│       ├── 0002_default_alert_rules.sql  # küszöb seed
│       └── README.md                     # migráció útmutató
│
├── scripts/
│   ├── check_setup.py                # környezet ellenőrző
│   ├── seed_test_data.py             # tesztadat betöltés
│   └── manual_discovery.py           # auto-discovery kézi indítás
│
└── tests/
    ├── __init__.py
    ├── test_storage.py
    ├── test_detector.py
    └── test_routing.py
```

---

## 4. Implementációs terv (lépésről lépésre)

### ✅ 1. lépés — Projektváz + Supabase séma
- ✅ Projekt mappastruktúra
- ✅ Supabase projekt létrehozása (Frankfurt régió)
- ✅ 10 tábla létrehozása SQL migrációkkal (`0001_initial_schema.sql`)
- ✅ Alapértelmezett küszöbök beszúrása (`0002_default_alert_rules.sql`)
- ✅ `check_setup.py` — környezet ellenőrző
- ✅ Python venv + függőségek (`requirements.txt`)
- ✅ GitHub privát repo + `Adikasz/ppc-monitor`

**Eredmény:** `python -m scripts.check_setup` → minden zöld

### ✅ 2. lépés — Discord bot alapja
- ✅ `src/bot/main.py` — bot belépési pont (`PpcBot` osztály, `setup_hook`, `on_ready`)
- ✅ Discord Developer Portal → bot + token
- ✅ Bot meghívása szerverre
- ✅ `setup_hook` + Cog loader (`_EXTENSIONS` lista)
- ✅ `on_ready` event log

**Eredmény:** `python -m src.bot.main` → bot online

### ✅ 3. lépés — Ügyfél-kezelés (clients)
- ✅ `src/storage/clients.py` — CRUD funkciók (`list_clients`, `get_client`, `get_client_by_name`, `create_client`)
- ✅ `src/bot/commands/clients.py` — `/clients list`, `add`, `info` (`ClientsCog`)
- ✅ Admin csatorna védelem (`DISCORD_ADMIN_CHANNEL_ID`)
- ✅ Slash command guild-szintű sync (gyors deploy)

**Eredmény:** Discord-on `/clients add Stopvill` működik

### ✅ 4. lépés — Hozzárendelések (assignments) + users
- ✅ `src/storage/users.py` — `get_or_create_user`, `get_user_by_discord_id`, `list_users` (auto-reg Discord ID alapján)
- ✅ `src/storage/assignments.py` — `create_assignment`, `delete_assignment`, `get_clients_for_user`, `get_assignments_for_client` (idempotens CRUD)
- ✅ `src/storage/audit.py` — `log_action` (hiba-tűrő audit írás)
- ✅ `src/bot/commands/assignments.py`:
  - `/assign client:<név> manager:<@user>` — admin csatorna + auto-regisztráció + audit
  - `/unassign client:<név> manager:<@user>` — admin csatorna + audit
  - `/my-clients` — saját ügyfeleim (bárhonnan, ephemeral)
- ✅ `src/bot/main.py` — `assignments` Cog regisztrálva az `_EXTENSIONS` listában

**Eredmény:** OM hozzárendelhető ügyfélhez, OM lekérdezi a saját ügyfeleit

### 5. lépés — Hirdetési fiókok (ad_accounts) + auto-discovery előkészítés
- `src/storage/campaigns.py` — kampányok CRUD
- `src/bot/commands/campaigns.py`:
  - `/campaign list client:<név>`
  - `/campaign tag campaign:<id> type:<google_brand_search|meta_conversion|...>`
  - `/campaign kpi campaign:<id> target_roas:X max_cpa:Y ...`
- Ad account összerendelés ügyfélhez (kézi konfiguráláskor)

**Eredmény:** Kampányokat lehet manuálisan felvenni és KPI-okat hozzárendelni

### 6. lépés — Meta Ads integráció + auto-discovery
- `src/integrations/meta_ads.py`:
  - Kampányok listázása fiókból
  - Insights (impressziók, klikkek, költés, conversiók) lekérése
- `src/monitoring/discovery.py`:
  - Új kampányok beszúrása `campaigns` táblába
  - Leállt kampányok `is_monitored=false`-ra állítása
  - `last_seen_at` frissítése
- `scripts/manual_discovery.py` — kézi indító

**Eredmény:** `python -m scripts.manual_discovery` → új kampányok automatikusan megjelennek a DB-ben

### 7. lépés — Google Ads integráció
- `src/integrations/google_ads.py`:
  - GAQL query a kampányokra + metrikákra
  - MCC (manager) account kezelés
- Discovery kibővítése Google-re is
- Egységes formátum: `meta_ads.py` és `google_ads.py` ugyanazt a dict struktúrát adja vissza

**Eredmény:** Mindkét platform kampányai egységesen lekérhetőek

### 8. lépés — Anomália-detektor + szabály-motor
- `src/storage/rules.py` — küszöb lekérdezés öröklődéssel
- `src/monitoring/rules_engine.py`:
  - Adott (campaign, metric) → effektív küszöb (global < type < campaign)
- `src/monitoring/detector.py`:
  - Metrikák összehasonlítása küszöbökkel
  - Anomáliák kategorizálása CRITICAL / WARNING / INSIGHT-re
  - Némítások figyelembevétele
- `src/storage/alerts.py` — pending alertek beszúrása + deduplikáció

**Eredmény:** Anomáliák észlelve, `alerts` táblába kerülnek `status='pending'` állapotban

### 9. lépés — Routing + Discord dispatcher
- `src/routing/router.py`:
  - Adott (client/campaign) → felelős OM(ek) lekérdezése
- `src/routing/dispatcher.py`:
  - Discord üzenet az OM csatornájába
  - Claude Haiku hívás a riasztás szöveges magyarázatához
  - `alerts.status` → `'sent'`, `discord_message_id` mentés

**Eredmény:** Anomáliák megjelennek a felelős OM Discord csatornáján

### 10. lépés — Csendes idő + összefoglalók
- `src/utils/quiet_hours.py` — `is_quiet_now(timezone)` függvény
- `src/routing/summarizer.py`:
  - `status='suppressed'` alertek összegyűjtése
  - Reggel 9 / hétfő 9 → összefoglaló üzenet
  - Claude használat a természetes nyelvű összefoglalóhoz
- `src/monitoring/scheduler.py` — APScheduler cron job-ok

**Eredmény:** 18:00 után nincs ping; reggel 9-kor egy közös összefoglaló

### 11. lépés — Némítás (mute) parancsok
- `src/storage/mutes.py` — CRUD
- `src/bot/commands/mutes.py`:
  - `/mute campaign:<id> days:<N> reason:<szöveg>`
  - `/unmute campaign:<id>`
  - `/mutes` — aktív némítások listája
- Auto-lejárat: `muted_until < now()` → némítás megszűnik

**Eredmény:** Tanulási fázisban lévő kampányok némíthatók

### 12. lépés — ClickUp integráció
- `src/integrations/clickup.py`:
  - Task létrehozás (CRITICAL alerts)
  - Felelős OM hozzárendelése
- Dispatcher kibővítése: CRITICAL → ClickUp task + Discord ping
- `alerts.clickup_task_id` mentés

**Eredmény:** CRITICAL alertekre automatikusan ClickUp task készül

### 13. lépés — Küszöb-kezelő parancsok
- `src/bot/commands/rules.py`:
  - `/rule list scope:<global|campaign_type|campaign>`
  - `/rule set scope:<...> metric:<...> warning:<N> critical:<M>`
- Audit log minden módosításra

**Eredmény:** A küszöbök Discord-ról konfigurálhatók

### 14. lépés — Tesztek + dokumentáció
- `tests/test_storage.py` — CRUD funkciók
- `tests/test_detector.py` — anomália-logika
- `tests/test_routing.py` — célzás
- README frissítés a használati útmutatóval
- Onboarding dokumentum az OM-eknek (parancs cheat sheet)

**Eredmény:** A rendszer tesztelt, dokumentált

### 15. lépés — Railway deploy
- Railway projekt létrehozása
- GitHub repo csatlakoztatása (auto-deploy)
- Environment variables beállítása (a `.env` tartalma)
- 2 service:
  - **bot** — always-on Discord bot
  - **monitor** — óránkénti cron job (scheduler)
- Logok ellenőrzése
- Stopvill ügyfél éles betöltése

**Eredmény:** A rendszer 24/7 fut a felhőben, Stopvill kampányait figyeli

---

## 5. Fejlesztési állapot

| # | Lépés | Status |
|---|-------|--------|
| 1 | Projektváz + Supabase séma | ✅ Kész |
| 2 | Discord bot alapja | ✅ Kész |
| 3 | Ügyfél-kezelés (`/clients`) | ✅ Kész |
| 4 | Hozzárendelések | ✅ Kész |
| 5 | Kampányok + KPI | 🔄 Következő |
| 6 | Meta Ads integráció + discovery | ⏳ |
| 7 | Google Ads integráció | ⏳ |
| 8 | Anomália-detektor | ⏳ |
| 9 | Routing + Discord dispatcher | ⏳ |
| 10 | Csendes idő + összefoglaló | ⏳ |
| 11 | Némítás | ⏳ |
| 12 | ClickUp integráció | ⏳ |
| 13 | Küszöb-kezelő parancsok | ⏳ |
| 14 | Tesztek + dokumentáció | ⏳ |
| 15 | Railway deploy | ⏳ |

---

## 6. Időbecslés

| Fázis | Tartalom | Becsült idő |
|-------|----------|-------------|
| **Fázis 1** — Alaprendszer | 1–9. lépés (bot + monitoring + alert) | Mid-June 2026 |
| **Fázis 2** — Finomítás | 10–14. lépés (csendes idő, némítás, ClickUp, tesztek) | Vége-Június / Július 2026 |
| **Fázis 3** — Élesítés | 15. lépés (Railway deploy + Stopvill éles) | Július 2026 |
| **Fázis 4** — Bővítés | Account-level events, szegmens-elemző, GA4 | Később |

## 7. Költségek (havi)

| Tétel | Költség |
|-------|---------|
| Railway hosting | ~2 000 Ft |
| Supabase (free tier elég) | 0 Ft |
| Claude Haiku 4.5 API | ~500–1 500 Ft |
| **Összesen** | **~3 000–5 000 Ft/hó** |

## 8. Fontos technikai megjegyzések

- **Python 3.14 használata** — `google-ads==25.1.0` nem kompatibilis, frissítés szükséges 28.2+ verzióra
- **`audioop-lts` szükséges** — Python 3.13+ óta nincs stdlib-ben, discord.py-nak kell
- **Supabase új API formátum** — 2.17.0 verzió használata (nem 2.9.x)
- **Windows konzol UTF-8** — `sys.stdout.reconfigure(encoding="utf-8")` a magyar karakterek miatt
- **`.env` SOHA nem commitelhető** — `.gitignore` ezt biztosítja
- **Cég referenciák semlegesítve** — projekt neve `ppc-monitor`, nincs cégnév a kódban

---

## 9. Repo info

- **GitHub:** `https://github.com/Adikasz/ppc-monitor` (privát)
- **Branch:** `main`
- **Commits:**
  - `20004c0` — 1. lépés: projektváz + Supabase séma
  - `8189db6` — 2. lépés: Discord bot alapja
  - `70f7fd8` — 3. lépés: Supabase ügyfél-kezelés + Discord parancsok

## 10. Készítő

**Fejlesztő:** Dávid (PlanSmart)
**Megrendelő:** MyMins (Zoli)
**Teszt ügyfél:** Stopvill
**Dokumentum verzió:** 1.0 — 2026.05.28.