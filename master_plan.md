# PPC AI Kampánymonitor — Master Plan v2.0

> **Projekt:** AI-alapú PPC kampánymonitoring rendszer Meta Ads és Google Ads integrációval
> **Cél:** Automatikus anomália-detektálás + Discord értesítés + ClickUp task-kezelés
> **Tech stack:** Python · Discord · Supabase · Railway · Claude API · ClickUp
> **Repo:** `Adikasz/ppc-monitor` (privát GitHub)
> **Verzió:** 2.0 — frissítve a MyMins × PlanSmart meeting után

---

## 1. Rendszer áttekintő

### Mit csinál
- **Auto-discovery** — automatikusan felfedezi a kampányokat a Meta/Google fiókokból
- **Óránkénti monitoring** — lekéri a teljesítményadatokat, összehasonlítja a beállított KPI-okkal
- **Anomália-detektálás** — CPA-ugrás, ROAS-esés, büdzsé-elfogyás, fiókszintű hibák
- **Célzott riasztás** — csak a hozzárendelt OM kap értesítést (+ opcionális helyettesek)
- **3 súlyossági szint** — 🔴 CRITICAL · 🟡 WARNING · 🟢 OPTIMIZATION/INFO
- **Csendes idő** — 18:00–09:00 + hétvégén nincs ping; reggel 9 / hétfőn összefoglaló
- **Deduplikáció** — metrikánként 1 ping per anomália, nem ismételget
- **ClickUp integráció** — CRITICAL → automatikus task a felelős OM-nek
- **Ügyfél email** — CRITICAL esetén automatikusan emailt küld az ügyfélnek (nem az OM-nek)
- **Kampány lifecycle** — szakasz-tudatos monitoring (New / Learning / Mature / Paused / Ended)

---

## 2. Jogosultság modell (véglegesített)

### EGY szint — mindenki egyenrangú

| Szempont | Döntés |
|----------|--------|
| Jogosultságok | **1 szint** — mindenki ugyanazt tudja |
| Rálátás | Minden OM látja az összes OM alerts-csatornáját |
| Értesítés | Csak az kap pinget, aki a kampányhoz/ügyfélhez van rendelve |
| Módosítás | Bárki módosíthat bármit (kollégának besegítés lehetséges) |
| Multi-owner | Kampányhoz több OM is hozzárendelhető (helyettesítés) |

### Jogosultságok részletesen
- `/clients` — ügyfelek kezelése (mindenki)
- `/assign` / `/unassign` — OM hozzárendelése (mindenki)
- `/campaign` — kampányok kezelése (mindenki)
- `/rule` — küszöbök beállítása (mindenki)
- `/mute` / `/unmute` — némítás (mindenki)
- `/my-clients` — saját ügyfelek listája

### Helyettesítés (multi-owner)
```
/campaign add-supporter campaign:<id> supporter:<@user>
/campaign remove-supporter campaign:<id> supporter:<@user>
```
- Elsődleges OM + tetszőleges számú helyettes
- Mindenki kap értesítést aki rá van rendelve
- Manuálisan kezelt (nem automatikus naptár-alapú)

---

## 3. Anomália szintek (véglegesített)

### 🔴 CRITICAL
Discord ping (@mention) + ClickUp task + Ügyfél email

**Esetek:**
- Leálltak a hirdetések (kampány aktív de 0 impression)
- Fiók letiltva (account_disabled)
- Hirdetés elutasítva (ad_disapproved) — 3+ db
- X napja nincs konverzió — sales kampánynál (konfig)
- X napja nincs lead — lead kampánynál (konfig)
- CPA túl magas — %-os küszöb (kampányonként konfig)
- Büdzsé 100%-ban elfogyott (budget_depleted)

### 🟡 WARNING
Discord üzenet (ping nélkül, de OM látja)

**Esetek:**
- Metrikák folyamatos csökkenése X napja
- CPA növekvő trend (előző időszakhoz képest)
- Büdzsé 90%-on (budget_near_limit)
- CTR csökken X napja folyamatosan
- CPC emelkedik X napja folyamatosan
- ROAS esik X napja folyamatosan
- Learning phase befagyva (7+ nap)

**Trend logika:**
- Összehasonlítás: előző N nap vs. megelőző N nap
- N értéke: kampányonként konfigurálható

### 🟢 OPTIMIZATION / INFO
Discord üzenet (csöndes, nincs ping)

**Esetek:**
- Stabil ROAS → lehetne emelni a büdzsét
- Alacsony impression share → lehetne emelni az ajánlatot
- X kreatívra nem költ → érdemes leállítani
- Általános optimalizálási javaslat

> **Megjegyzés:** OPTIMIZATION szint = extra modul (Diagnostic AI), külön árajánlattal.

---

## 4. Kampány lifecycle (kampányállapot)

### Szakaszok

| Állapot | Mit jelent | Monitoring |
|---------|-----------|------------|
| `new` | Most indult, nincs historikus adat | ❌ Nem jelez |
| `learning` | Meta/Google learning phase | ⚠️ Csak CRITICAL |
| `mature` | Normál üzem, van historikus adat | ✅ Teljes monitoring |
| `paused` | Szüneteltetett | ❌ Nem jelez |
| `ended` | Lezárva | ❌ Archivált |

### Beállítás
```
/campaign set-state campaign:<id> state:<new|learning|mature|paused|ended>
/campaign set-state campaign:<id> state:learning until:2026-06-20
```
- Manuálisan állítható (OM vagy admin)
- `until` opcionális — lejárat után automatikusan `mature`-ra vált
- `new` állapot: nincs anomália-riasztás, adatgyűjtés folyik

---

## 5. KPI struktúra (kampányonként — mind kötelező)

| Mező | Típus | Megjegyzés |
|------|-------|-----------|
| `target_roas` | numeric (%) | Célzott ROAS |
| `max_cpa` | numeric (Ft) | Max megengedett CPA |
| `max_cpl` | numeric (Ft) | Max megengedett CPL (lead kampánynál) |
| `monthly_budget` | numeric (Ft) | Havi büdzsé |
| `target_ctr` | numeric (%) | Célzott CTR |
| `max_cpc` | numeric (Ft) | Max CPC |
| `primary_conversion_event` | text | "Purchase" / "Lead" / stb. |
| `campaign_lifecycle_state` | enum | new / learning / mature / paused / ended |
| `data_valid_from` | date | Mikor lett jó a mérés (korábbi adatok ignorálva) |
| `no_conversion_critical_days` | integer | X napja nincs konv → CRITICAL |
| `cpa_spike_critical_pct` | numeric (%) | +X% CPA → CRITICAL |
| `trend_warning_days` | integer | X napja csökken → WARNING |

> `data_valid_from` — ha a mérés korábban rossz volt, a rendszer csak ettől a dátumtól néz.

---

## 6. Riasztás deduplikáció (véglegesített)

| Típus | Deduplikáció |
|-------|-------------|
| Metrika-alapú (CPA, ROAS, CTR stb.) | **1 ping per anomália** — nem ismételget |
| Fiók letiltva / hirdetés elutasítva | **Azonnal ping** — minden alkalommal |
| Ügyfél email | **Csak CRITICAL esetén, 1x per esemény** |
| ClickUp task | **Csak CRITICAL esetén, 1x per esemény** |

---

## 7. Összefoglaló (napi agenda)

- **Mikor:** Reggel 9:00 (Europe/Budapest)
- **Hétvége:** Péntek 9:00 – Hétfő 9:00 közötti összes event hétfőn jelenik meg
- **Tartalom:** Összes event az előző összefoglalótól mostanáig (anomáliák + változtatások)
- **Hova megy:** Az érintett OM Discord csatornájára
- **Helyettesek:** Ha valaki helyettes, ő is megkapja az összefoglalót

---

## 8. Ügyfélüzenet sablonok (B.4)

- **Nyelv:** Magyar (egyelőre)
- **Jóváhagyás:** Az érintett OM hagyja jóvá (1 szint)
- **AI személyre szabás:** Ügyfél neve, OM neve, kampány neve automatikusan behelyettesítve
- **Approval flow:** AI generálja → OM jóváhagyja → elküldve
- **Sablontípusok (definiálandó meetingen):**
  - Fiók letiltva
  - Hirdetés elutasítva
  - Magas CPA
  - Nincs konverzió
  - Büdzsé elfogyott
  - Egyéb (szabad szöveges)

---

## 9. Discord csatorna struktúra

| Csatorna | Ki látja | Ki kap pinget |
|---------|---------|--------------|
| `#alerts-[om-name]` | Mindenki (minden OM) | Csak az érintett OM + helyettesek |
| `#admin-config` | Mindenki | — |
| `#daily-summary` | Mindenki | Senki (csak olvasás) |

> Nem-OM pozíciók (pl. management) ne legyenek a csatornákon.

---

## 10. Extra modulok (scope creep — külön árajánlat)

### A csomag — Kampány lifecycle kezelés
- **Státusz:** ✅ Benne van az alapban (manuális beállítás)
- **Extra fejlesztési díj:** 0 Ft (már tervezve)

### B1 csomag — Diagnostic AI (metrikák alapján)
- **Mit tud:** "Valószínűleg rossz audience, mert X metrika alapján..."
- **Technológia:** Claude API (Haiku)
- **Fejlesztési díj:** ~60 000–80 000 Ft (egyszeri)
- **Havi extra:** ~1 000–2 000 Ft

### B2 csomag — Diagnostic AI + Kreatív pull
- **Mit tud:** Metrikák + kreatív szöveg/kép alapján diagnózis
- **Technológia:** Claude API + Meta Ad Library API
- **Fejlesztési díj:** ~90 000–120 000 Ft (egyszeri)
- **Havi extra:** ~2 000–4 000 Ft

> Ezek az élesítés **után** aktiválhatók, nem blokkolják az alap rendszert.

---

## 11. Adatbázis séma (frissített)

### Módosítások a v1.0-hoz képest

#### `campaigns` tábla — új mezők
```sql
lifecycle_state         text DEFAULT 'new'
                        CHECK ('new' | 'learning' | 'mature' | 'paused' | 'ended')
lifecycle_until         timestamptz   -- auto-váltás lejárata
data_valid_from         date          -- korábbi rossz mérés ignorálása
```

#### `campaign_kpis` tábla — új mezők
```sql
target_ctr                    numeric   -- % (kötelező)
max_cpc                       numeric   -- Ft (kötelező)
no_conversion_critical_days   integer   -- X napja nincs konv → CRITICAL
cpa_spike_critical_pct        numeric   -- +X% → CRITICAL
trend_warning_days            integer   -- X napja csökken → WARNING
```

#### `assignments` tábla — supporter logika
```sql
role   text DEFAULT 'primary' CHECK ('primary' | 'supporter')
```
> `primary` = elsődleges felelős, `supporter` = helyettes

#### `clients` tábla — ügyfél email
```sql
contact_email   text   -- ügyfél email (CRITICAL esetén automatikus email)
```

#### `alert_rules` — severity enum bővítése
```sql
severity   text CHECK ('critical' | 'warning' | 'optimization')
```

---

## 12. Mappastruktúra (frissített)

```
ppc-monitor/
├── src/
│   ├── bot/
│   │   └── commands/
│   │       ├── clients.py         ✅ Kész
│   │       ├── assignments.py     ✅ Kész
│   │       ├── campaigns.py       🔄 5. lépés
│   │       ├── mutes.py           ⏳ 11. lépés
│   │       └── rules.py           ⏳ 13. lépés
│   ├── storage/
│   │   ├── clients.py             ✅ Kész
│   │   ├── users.py               ✅ Kész
│   │   ├── assignments.py         ✅ Kész
│   │   ├── audit.py               ✅ Kész
│   │   ├── campaigns.py           🔄 5. lépés
│   │   ├── kpis.py                🔄 5. lépés
│   │   ├── alerts.py              ⏳ 8. lépés
│   │   ├── mutes.py               ⏳ 11. lépés
│   │   └── rules.py               ⏳ 13. lépés
│   ├── integrations/
│   │   ├── meta_ads.py            🔄 6. lépés
│   │   ├── google_ads.py          🔄 7. lépés
│   │   ├── claude_ai.py           🔄 9. lépés
│   │   ├── clickup.py             🔄 12. lépés
│   │   └── email_sender.py        🔄 9. lépés (ügyfél email)
│   ├── monitoring/
│   │   ├── discovery.py           🔄 6. lépés
│   │   ├── fetcher.py             🔄 6. lépés
│   │   ├── detector.py            🔄 8. lépés
│   │   ├── rules_engine.py        🔄 8. lépés
│   │   └── scheduler.py           🔄 10. lépés
│   ├── routing/
│   │   ├── router.py              🔄 9. lépés
│   │   ├── dispatcher.py          🔄 9. lépés
│   │   └── summarizer.py          🔄 10. lépés
│   └── utils/
│       ├── logging.py             ✅ Kész
│       ├── timezone.py            ⏳
│       └── quiet_hours.py         ⏳ 10. lépés
└── supabase/migrations/
    ├── 0001_initial_schema.sql    ✅ Kész
    ├── 0002_default_alert_rules.sql ✅ Kész
    └── 0003_v2_schema_updates.sql 🔄 Következő (lifecycle, email, role)
```

---

## 13. Implementációs terv (frissített)

| # | Lépés | Tartalom | Státusz |
|---|-------|----------|---------|
| 1 | Projektváz + Supabase séma | Alapstruktúra | ✅ Kész |
| 2 | Discord bot alapja | Bot bejelentkezés | ✅ Kész |
| 3 | Ügyfelek (`/clients`) | CRUD + Discord parancsok | ✅ Kész |
| 4 | Hozzárendelések + Users | assign/unassign/my-clients | ✅ Kész |
| **5** | **Séma v2 + Kampányok + KPI** | lifecycle, supporter role, contact_email + /campaign parancsok | **🔄 KÖVETKEZŐ** |
| 6 | Meta Ads integráció | API + auto-discovery | ⏳ |
| 7 | Google Ads integráció | GAQL + discovery | ⏳ |
| 8 | Anomália-detektor | rules_engine + detector | ⏳ |
| 9 | Routing + dispatcher | Discord + ügyfél email + ClickUp | ⏳ |
| 10 | Csendes idő + összefoglaló | quiet hours + summarizer | ⏳ |
| 11 | Némítás | mute/unmute parancsok | ⏳ |
| 12 | ClickUp integráció | CRITICAL → task | ⏳ |
| 13 | Küszöb-kezelő parancsok | /rule set/list | ⏳ |
| 14 | Tesztek + dokumentáció | unit tests + OM cheatsheet | ⏳ |
| 15 | Railway deploy | élesítés | ⏳ |

---

## 14. 5. lépés részletezése (KÖVETKEZŐ)

### 5a — Supabase séma v2 migráció
Fájl: `supabase/migrations/0003_v2_schema_updates.sql`

```sql
-- campaigns tábla bővítése
ALTER TABLE campaigns ADD COLUMN lifecycle_state text DEFAULT 'new'
    CHECK (lifecycle_state IN ('new','learning','mature','paused','ended'));
ALTER TABLE campaigns ADD COLUMN lifecycle_until timestamptz;
ALTER TABLE campaigns ADD COLUMN data_valid_from date;

-- campaign_kpis bővítése
ALTER TABLE campaign_kpis ADD COLUMN target_ctr numeric;
ALTER TABLE campaign_kpis ADD COLUMN max_cpc numeric;
ALTER TABLE campaign_kpis ADD COLUMN no_conversion_critical_days integer DEFAULT 3;
ALTER TABLE campaign_kpis ADD COLUMN cpa_spike_critical_pct numeric DEFAULT 50;
ALTER TABLE campaign_kpis ADD COLUMN trend_warning_days integer DEFAULT 7;

-- assignments: supporter role
ALTER TABLE assignments ADD COLUMN role text DEFAULT 'primary'
    CHECK (role IN ('primary','supporter'));

-- clients: ügyfél email
ALTER TABLE clients ADD COLUMN contact_email text;

-- alert_rules: optimization szint
ALTER TABLE alert_rules DROP CONSTRAINT IF EXISTS alert_rules_severity_check;
ALTER TABLE alert_rules ADD CONSTRAINT alert_rules_severity_check
    CHECK (severity IN ('critical','warning','optimization'));
```

### 5b — `src/storage/campaigns.py`
- `list_campaigns(client_id, active_only=True)`
- `get_campaign(campaign_id)`
- `create_campaign(...)`
- `set_lifecycle_state(campaign_id, state, until=None)`
- `add_supporter(campaign_id, user_id)`
- `remove_supporter(campaign_id, user_id)`

### 5c — `src/storage/kpis.py`
- `get_active_kpis(campaign_id)`
- `set_kpis(campaign_id, **fields)` — verziózott INSERT

### 5d — `src/bot/commands/campaigns.py` (Cog)
```
/campaign list client:<név>
/campaign info campaign_id:<id>
/campaign add client:<név> platform:<meta|google> account_id:<...> name:<...>
/campaign tag campaign_id:<id> type:<google_brand_search|meta_conversion|...>
/campaign kpi campaign_id:<id> target_roas:<N> max_cpa:<N> ...
/campaign set-state campaign_id:<id> state:<new|learning|mature|paused|ended> [until:<dátum>]
/campaign add-supporter campaign_id:<id> supporter:<@user>
/campaign remove-supporter campaign_id:<id> supporter:<@user>
```

### 5e — `main.py` frissítés
CampaignsCog regisztrálása az `_EXTENSIONS`-be.

---

## 15. Üzleti döntések összefoglalója

| Téma | Döntés |
|------|--------|
| Jogosultság | 1 szint — mindenki egyenrangú |
| Riasztás célzás | Hozzárendelt OM + helyettesek |
| Csatornák | Nyílt (minden OM lát mindent), de ping csak az érintettnek |
| Nem-OM | Ne kapjanak értesítést, ne legyenek a csatornákon |
| Email | Csak ügyfélnek, CRITICAL esetén (nem OM-nek) |
| ClickUp | Csak CRITICAL esetén task |
| Deduplikáció | 1 ping per anomália |
| Csendes idő | 18:00–09:00 + hétvége |
| Összefoglaló | Reggel 9:00, előző összefoglalótól mostanáig |
| Lifecycle | Manuális beállítás, `until` auto-váltással |
| Diagnostic AI | Extra modul, külön árajánlat (~60–120K Ft) |
| Sablonok | Magyar, OM jóváhagyja, AI személyre szabja |
| GA4 | Ellenőrző forrás (nem primary), mérési eltérés warning |

---

## 16. Havi költség (becsült)

| Téma | Havi |
|------|------|
| Railway | ~2 000 Ft |
| Supabase (free tier) | 0 Ft |
| Claude Haiku (basic riasztás szövegezés) | ~500–1 500 Ft |
| **Alap összesen** | **~3 000–5 000 Ft** |
| + Diagnostic AI (B1) | +1 000–2 000 Ft |
| + Diagnostic AI + Kreatív (B2) | +2 000–4 000 Ft |
