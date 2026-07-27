-- =============================================================================
-- Migration 0011 — Új KPI metrikák: CTR / CPM / Frequency / Impressions küszöbök
-- PPC Monitor
-- =============================================================================
-- Négy új, opcionális (nullable, DEFAULT NULL = kikapcsolva) KPI-küszöb a
-- campaign_kpis és az ad_account_kpis táblákban. A detektor ezekből új anomália-
-- típusokat számol: ctr_drop, cpm_spike, frequency_spike, impressions_drop.
--
-- A meglévő oszlopok (numeric) konvencióját követjük; a min_impressions egész.
-- Idempotens (IF NOT EXISTS). Kézzel futtatandó a Supabase SQL editorban
-- (nincs supabase CLI). Kétszer lefuttatva sem hibázik. A kód addig is működik:
-- a hiányzó oszlopok None-ként öröklődnek → az új szabályok egyszerűen nem fognak
-- tüzelni, a régiek változatlanok.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Kampány-szintű KPI küszöbök
-- ---------------------------------------------------------------------------
ALTER TABLE campaign_kpis
    ADD COLUMN IF NOT EXISTS min_ctr         numeric DEFAULT NULL,  -- minimum CTR % (pl. 1.5)
    ADD COLUMN IF NOT EXISTS max_cpm         numeric DEFAULT NULL,  -- maximum CPM Ft (pl. 500)
    ADD COLUMN IF NOT EXISTS max_frequency   numeric DEFAULT NULL,  -- maximum frequency (pl. 3.5)
    ADD COLUMN IF NOT EXISTS min_impressions integer DEFAULT NULL;  -- minimum impressions (drop-figyelés)

-- ---------------------------------------------------------------------------
-- Fiók-szintű KPI küszöbök (lecsorognak a campaign_kpis-ba)
-- ---------------------------------------------------------------------------
ALTER TABLE ad_account_kpis
    ADD COLUMN IF NOT EXISTS min_ctr         numeric DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS max_cpm         numeric DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS max_frequency   numeric DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS min_impressions integer DEFAULT NULL;
