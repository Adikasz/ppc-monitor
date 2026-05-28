-- ============================================================
-- PPC AI Kampánymonitor — Adatbázis séma
-- 0001_initial_schema.sql
-- ============================================================
-- Ez a séma a véglegesített követelmények alapján készült:
--  - Egy jogosultsági szint (mindenki egyenrangú), de célzott riasztás
--  - Auto-discovery (a kampányok a Meta/Google fiókból jönnek)
--  - Konfigurálható CRITICAL/WARNING küszöbök (változóként)
--  - Némítás funkció (kampányonként, X napra)
--  - Csendes idő + összefoglaló
-- ============================================================


-- ============================================================
-- 1. FELHASZNÁLÓK (account managerek)
-- ============================================================
-- Mindenki egyenrangú — nincs admin/nem-admin megkülönböztetés.
-- A Discord user ID alapján azonosítjuk őket.
create table if not exists users (
    id              bigint generated always as identity primary key,
    discord_user_id text        not null unique,        -- Discord numerikus ID
    display_name    text        not null,               -- pl. "Máté"
    email           text,                                -- opcionális, összefoglalókhoz
    is_active       boolean     not null default true,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

comment on table users is 'Account managerek. Mindenki egyenrangú; a hozzárendelés dönti el ki mit kap.';


-- ============================================================
-- 2. ÜGYFELEK
-- ============================================================
create table if not exists clients (
    id              bigint generated always as identity primary key,
    name            text        not null unique,         -- pl. "Stopvill"
    is_active       boolean     not null default true,
    -- Az ügyfél Discord csatornája, ahova a riasztások mennek.
    -- Lehet közös is, de jellemzően a felelős AM személyes csatornája.
    discord_channel_id text,
    notes           text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

comment on table clients is 'Ügyfelek (pl. Stopvill). A kampányok ehhez tartoznak.';


-- ============================================================
-- 3. HIRDETÉSI FIÓKOK (ad accounts)
-- ============================================================
-- Egy ügyfélnek lehet több fiókja (Meta + Google, vagy több is platformonként).
create table if not exists ad_accounts (
    id              bigint generated always as identity primary key,
    client_id       bigint      not null references clients(id) on delete cascade,
    platform        text        not null check (platform in ('meta', 'google')),
    -- A platform saját fiók-azonosítója:
    --   Meta:   "act_123456789"
    --   Google: "1234567890" (customer ID kötőjelek nélkül)
    external_account_id text    not null,
    account_name    text,                                -- emberi név, ha van
    is_active       boolean     not null default true,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (platform, external_account_id)
);

comment on table ad_accounts is 'Meta/Google hirdetési fiókok, ügyfélhez kötve. Az auto-discovery innen indul.';


-- ============================================================
-- 4. KAMPÁNYOK
-- ============================================================
-- Auto-discovery tölti fel: a rendszer felfedezi a fiókban lévő kampányokat.
-- A típus-címkézést és a KPI-okat az account manager állítja be.
create table if not exists campaigns (
    id              bigint generated always as identity primary key,
    ad_account_id   bigint      not null references ad_accounts(id) on delete cascade,
    -- A platform saját kampány-azonosítója (auto-discovery tölti)
    external_campaign_id text   not null,
    name            text        not null,                -- platformról jövő kampánynév

    -- Kampánytípus — a CTR/CPC értékeléshez. Az AM címkézi.
    -- Lehetséges értékek (bővíthető):
    --   google_brand_search, google_general_search, google_shopping,
    --   google_pmax, google_display,
    --   meta_traffic, meta_conversion, meta_remarketing
    campaign_type   text,

    -- A monitoring státusza
    is_monitored    boolean     not null default true,   -- figyeljük-e egyáltalán

    -- Auto-discovery metaadat
    platform_status text,                                 -- a platform szerinti státusz (ACTIVE, PAUSED, stb.)
    discovered_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now(),   -- mikor láttuk utoljára a fiókban

    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (ad_account_id, external_campaign_id)
);

comment on table campaigns is 'Kampányok, auto-discovery-vel felfedezve. Típus és KPI az AM-től jön.';
comment on column campaigns.is_monitored is 'Ha a kampány leáll a platformon, ez false-ra vált (kiesik a figyelésből).';


-- ============================================================
-- 5. KAMPÁNY KPI-ok (célok)
-- ============================================================
-- Kampányonként egy aktív KPI-sor. Bármikor módosítható (verziózzuk a historyt).
create table if not exists campaign_kpis (
    id              bigint generated always as identity primary key,
    campaign_id     bigint      not null references campaigns(id) on delete cascade,

    -- Célok (mind nullozható, de a követelmény szerint igyekszünk mindet kitölteni)
    target_roas     numeric,                              -- cél ROAS, pl. 500 (=500%)
    max_cpa         numeric,                              -- max CPA forintban
    max_cpl         numeric,                              -- max CPL forintban (lead-gen)
    monthly_budget  numeric,                              -- havi büdzsé forintban
    target_ctr      numeric,                              -- cél CTR százalékban, pl. 3.5
    max_cpc         numeric,                              -- max CPC forintban
    primary_conversion_event text,                        -- pl. "Purchase", "Lead"

    is_active       boolean     not null default true,    -- ez az aktuális sor
    created_at      timestamptz not null default now(),
    -- ki állította be (audit)
    set_by_discord_user_id text
);

comment on table campaign_kpis is 'Kampány célok. Verziózott: új sor = új verzió, a régi is_active=false lesz.';


-- ============================================================
-- 6. HOZZÁRENDELÉSEK (ki felel melyik ügyfélért/kampányért)
-- ============================================================
-- Ez dönti el, KI KAP riasztást. A követelmény szerint:
-- mindenki bármit hozzárendelhet, de a riasztás CSAK a hozzárendelt
-- személy(ek)hez megy.
--
-- Rugalmas: hozzárendelhetünk ügyfél-szinten VAGY kampány-szinten.
--   - client_id kitöltve, campaign_id null  → az egész ügyfélért felel
--   - campaign_id kitöltve                   → csak az adott kampányért felel
create table if not exists assignments (
    id              bigint generated always as identity primary key,
    user_id         bigint      not null references users(id) on delete cascade,
    client_id       bigint      references clients(id) on delete cascade,
    campaign_id     bigint      references campaigns(id) on delete cascade,
    created_at      timestamptz not null default now(),
    created_by_discord_user_id text,                      -- ki hozta létre (audit)

    -- Legalább az egyik szintnek meg kell lennie
    check (client_id is not null or campaign_id is not null)
);

comment on table assignments is 'Ki felel miért. A riasztás-célzás ezen alapul. Ügyfél- vagy kampány-szintű.';


-- ============================================================
-- 7. ANOMÁLIA-SZABÁLYOK / KÜSZÖBÖK (konfigurálható változók)
-- ============================================================
-- A követelmény: a CRITICAL/WARNING küszöb legyen konfigurálható változó.
-- Ezt háromszintű öröklődéssel oldjuk meg:
--   1. globális alapérték (scope='global')
--   2. kampánytípus-szintű felülírás (scope='campaign_type')
--   3. kampány-szintű felülírás (scope='campaign')
-- A legspecifikusabb nyer.
create table if not exists alert_rules (
    id              bigint generated always as identity primary key,

    scope           text        not null check (scope in ('global', 'campaign_type', 'campaign')),
    -- scope='campaign_type' esetén a típus neve; scope='campaign' esetén null és campaign_id-t használunk
    campaign_type   text,
    campaign_id     bigint      references campaigns(id) on delete cascade,

    -- Milyen metrikára vonatkozik
    --   cpa_spike, roas_drop, budget_depleted, ctr_low, cpc_high,
    --   account_disabled, payment_error, ad_disapproved, learning_frozen
    metric          text        not null,

    -- A küszöb és hogy melyik súlyossági szintet váltja ki
    warning_threshold  numeric,                           -- ezt átlépve WARNING
    critical_threshold numeric,                           -- ezt átlépve CRITICAL
    -- a küszöb iránya: 'above' (efölött baj) vagy 'below' (ezalatt baj)
    direction       text        check (direction in ('above', 'below')),

    is_active       boolean     not null default true,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    set_by_discord_user_id text
);

comment on table alert_rules is 'Konfigurálható anomália-küszöbök. Öröklődés: global < campaign_type < campaign.';


-- ============================================================
-- 8. NÉMÍTÁSOK (mute)
-- ============================================================
-- A követelmény: kampányonként X napra letiltható a riasztás
-- (pl. új kampány tanulási fázisa), majd visszakapcsolható.
create table if not exists mutes (
    id              bigint      generated always as identity primary key,
    campaign_id     bigint      not null references campaigns(id) on delete cascade,
    -- meddig tart a némítás
    muted_until     timestamptz not null,
    reason          text,                                 -- pl. "tanulási fázis"
    is_active       boolean     not null default true,
    created_at      timestamptz not null default now(),
    created_by_discord_user_id text
);

comment on table mutes is 'Kampány-némítás X napra. muted_until lejár → automatikusan újraindul a figyelés.';


-- ============================================================
-- 9. RIASZTÁSOK (napló + deduplikáció + összefoglaló alap)
-- ============================================================
-- Minden észlelt anomáliát ide naplózunk. Ez kell:
--   - deduplikációhoz (ne menjen ki ugyanaz 10x)
--   - a csendes idő utáni összefoglalóhoz
--   - audithoz / utólagos elemzéshez
create table if not exists alerts (
    id              bigint      generated always as identity primary key,
    campaign_id     bigint      references campaigns(id) on delete set null,
    client_id       bigint      references clients(id) on delete set null,

    severity        text        not null check (severity in ('critical', 'warning', 'insight')),
    metric          text        not null,                 -- mi váltotta ki
    -- a konkrét értékek a riasztáskor
    observed_value  numeric,
    threshold_value numeric,

    -- a Claude által generált emberi szöveg
    message         text,

    -- állapotkövetés
    status          text        not null default 'pending'
                    check (status in ('pending', 'sent', 'suppressed', 'summarized')),
    -- 'pending'    = észlelve, még nem küldtük
    -- 'sent'       = azonnal kiment (munkaidőben, nem némított)
    -- 'suppressed' = csendes időben keletkezett, összefoglalóba megy
    -- 'summarized' = bekerült egy összefoglalóba

    -- kihez ment (routing eredménye)
    routed_to_discord_user_id text,
    discord_message_id text,                              -- ha kiment, a Discord üzenet ID
    clickup_task_id text,                                 -- ha készült ClickUp task

    detected_at     timestamptz not null default now(),
    sent_at         timestamptz,

    -- deduplikációs kulcs: ugyanarra a kampány+metrika+nap kombóra ne menjen több
    dedup_key       text
);

comment on table alerts is 'Minden észlelt anomália naplója. Deduplikáció, összefoglaló, audit alapja.';
create index if not exists idx_alerts_dedup on alerts (dedup_key, detected_at);
create index if not exists idx_alerts_status on alerts (status);
create index if not exists idx_alerts_campaign on alerts (campaign_id);


-- ============================================================
-- 10. AUDIT LOG (ki mit módosított)
-- ============================================================
-- Mivel mindenki egyenrangú és bárki módosíthat, fontos a nyomon követhetőség.
create table if not exists audit_log (
    id              bigint      generated always as identity primary key,
    discord_user_id text        not null,                 -- ki csinálta
    action          text        not null,                 -- pl. "assign", "set_kpi", "mute", "unmute"
    entity_type     text,                                  -- "client", "campaign", "kpi", "rule", "mute"
    entity_id       bigint,
    details         jsonb,                                 -- a változás részletei
    created_at      timestamptz not null default now()
);

comment on table audit_log is 'Minden konfigurációs változás naplója. Bárki módosíthat, ezért auditálunk.';
create index if not exists idx_audit_user on audit_log (discord_user_id, created_at);


-- ============================================================
-- AUTOMATIKUS updated_at FRISSÍTÉS
-- ============================================================
create or replace function set_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

-- updated_at trigger minden olyan táblára, ahol van ilyen oszlop
do $$
declare
    t text;
begin
    foreach t in array array['users', 'clients', 'ad_accounts', 'campaigns', 'alert_rules']
    loop
        execute format('
            drop trigger if exists trg_%I_updated_at on %I;
            create trigger trg_%I_updated_at
                before update on %I
                for each row execute function set_updated_at();
        ', t, t, t, t);
    end loop;
end;
$$;
