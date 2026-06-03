-- =============================================================================
-- Migration 0003 — Séma v2 frissítések
-- PPC Monitor — 5. lépés: lifecycle, supporter role, contact_email, KPI bővítés
-- =============================================================================

-- ---------------------------------------------------------------------------
-- campaigns — lifecycle mezők
-- ---------------------------------------------------------------------------
ALTER TABLE campaigns
    ADD COLUMN lifecycle_state text DEFAULT 'new'
        CHECK (lifecycle_state IN ('new', 'learning', 'mature', 'paused', 'ended'));

ALTER TABLE campaigns
    ADD COLUMN lifecycle_until timestamptz;

ALTER TABLE campaigns
    ADD COLUMN data_valid_from date;

-- ---------------------------------------------------------------------------
-- campaign_kpis — új KPI mezők
-- ---------------------------------------------------------------------------
ALTER TABLE campaign_kpis
    ADD COLUMN target_ctr numeric;                         -- % célzott CTR

ALTER TABLE campaign_kpis
    ADD COLUMN max_cpc numeric;                            -- Ft max CPC

ALTER TABLE campaign_kpis
    ADD COLUMN no_conversion_critical_days integer DEFAULT 3;  -- X napja nincs konv → CRITICAL

ALTER TABLE campaign_kpis
    ADD COLUMN cpa_spike_critical_pct numeric DEFAULT 50;      -- +X% CPA → CRITICAL

ALTER TABLE campaign_kpis
    ADD COLUMN trend_warning_days integer DEFAULT 7;           -- X napja csökken → WARNING

-- ---------------------------------------------------------------------------
-- assignments — supporter role
-- ---------------------------------------------------------------------------
ALTER TABLE assignments
    ADD COLUMN role text DEFAULT 'primary'
        CHECK (role IN ('primary', 'supporter'));

-- ---------------------------------------------------------------------------
-- clients — ügyfél kontakt email (CRITICAL esetén automatikus email)
-- ---------------------------------------------------------------------------
ALTER TABLE clients
    ADD COLUMN contact_email text;

-- ---------------------------------------------------------------------------
-- alert_rules — severity enum bővítése: optimization szint hozzáadása
-- ---------------------------------------------------------------------------
ALTER TABLE alert_rules
    DROP CONSTRAINT IF EXISTS alert_rules_severity_check;

ALTER TABLE alert_rules
    ADD CONSTRAINT alert_rules_severity_check
        CHECK (severity IN ('critical', 'warning', 'optimization'));
