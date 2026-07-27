-- =============================================================================
-- Migration 0004 — campaign_insights (óránkénti metrika-history)
-- PPC Monitor — 9. lépés: valódi Meta/Google metrics tárolása
-- =============================================================================
-- A scheduler óránként lekéri minden monitorozott kampány aktuális metrikáit
-- (Meta/Google API), és ebbe a táblába írja. A detektor a friss insightból
-- dolgozik; a history (≈1 hét) trendekhez és utólagos elemzéshez kell.
-- =============================================================================

create table if not exists campaign_insights (
    id               bigint      generated always as identity primary key,
    campaign_id      bigint      not null references campaigns(id) on delete cascade,

    -- Nyers metrikák (a platform API-ból, normalizálva)
    impressions      integer,
    clicks           integer,
    spend            numeric,                       -- költés (Meta: spend, Google: cost)
    conversions      numeric,                       -- numeric! a Google konverzió tört is lehet
    conversion_value numeric,

    -- Származtatott metrikák
    ctr              numeric,                       -- clicks / impressions (arány, pl. 0.05)
    cpc              numeric,                       -- spend / clicks
    cpa              numeric,                       -- spend / conversions
    roas             numeric,                       -- conversion_value / spend

    fetched_at       timestamptz not null default now(),   -- mikor kértük le az API-ból
    created_at       timestamptz not null default now()
);

comment on table campaign_insights is 'Óránkénti kampány-metrika snapshotok (Meta/Google). A detektor és a trendek alapja.';

-- Gyors lekérdezés a legutóbbi insightra kampányonként
create index if not exists idx_campaign_insights_campaign
    on campaign_insights (campaign_id, fetched_at desc);

-- Óránkénti egyediség: egy kampányra óránként egyetlen sor.
-- FIGYELEM: date_trunc('hour', timestamptz) STABLE (session-timezone függő),
-- ezért unique indexbe közvetlenül NEM tehető. A `... AT TIME ZONE 'UTC'`
-- timestamptz → timestamp (tz nélkül) konverzió fix és IMMUTABLE, így a
-- date_trunc rajta már indexelhető.
create unique index if not exists uq_campaign_insights_hour
    on campaign_insights (campaign_id, (date_trunc('hour', fetched_at at time zone 'UTC')));
