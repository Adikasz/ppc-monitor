-- =============================================================================
-- Migration 0009 — Client-szintű assign + KPI kaszkád + dinamikus küszöbök
-- PPC Monitor — 15. lépés
-- =============================================================================
-- Idempotens (IF NOT EXISTS). Kézzel futtatandó a Supabase SQL editorban
-- (nincs supabase CLI a repóban — lásd a 0008 megjegyzést).
-- =============================================================================

-- ---------------------------------------------------------------------------
-- RÉSZ 1 — assignments: öröklés-jelölő
-- A /client assign minden kampányra létrehoz egy kampány-szintű hozzárendelést;
-- ezeket inherited_from_client=true jelöli. A discovery az új kampányokra is
-- örökíti a kliens-szintű hozzárendeléseket (szintén true jelöléssel).
-- ---------------------------------------------------------------------------
ALTER TABLE assignments
    ADD COLUMN IF NOT EXISTS inherited_from_client boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN assignments.inherited_from_client IS
    'true = a kampány-szintű hozzárendelést a /client assign kaszkád vagy a discovery öröklés hozta létre (nem kézzel).';

-- ---------------------------------------------------------------------------
-- RÉSZ 2 — client_kpis: kliens-szintű KPI default (egy aktív sor / kliens)
-- A /client kpi ide ír, majd lecsorgatja a kliens összes kampányának
-- campaign_kpis sorába. A detektor innen örököl, ha nincs campaign-szintű érték.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS client_kpis (
    id                       bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    client_id                bigint NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    target_roas              numeric,
    target_roi               numeric,
    max_cpa                  numeric,
    max_cpl                  numeric,
    target_ctr               numeric,
    max_cpc                  numeric,
    monthly_budget           numeric,
    primary_conversion_event text,
    warning_pct              numeric DEFAULT 20,
    critical_pct             numeric DEFAULT 40,
    is_active                boolean DEFAULT true,
    created_at               timestamptz DEFAULT now(),
    UNIQUE (client_id)
);

COMMENT ON TABLE client_kpis IS
    'Kliens-szintű KPI default + warning/critical százalékok. /client kpi állítja; lecsorog a campaign_kpis-ba, a detektor innen örököl.';

-- ---------------------------------------------------------------------------
-- RÉSZ 2/3 — campaign_kpis: kliens-küszöbök tárolása + override-jelölő
-- A warning_pct/critical_pct a detektor irány-érzékeny küszöbeihez kell.
-- Az inherited_from_client jelöli, hogy a sort a kliens-kaszkád hozta-e létre:
--   false = kézi /campaign kpi override → a kliens-kaszkád NEM írja felül.
-- A meglévő (kézzel beállított) sorok automatikusan false-t kapnak — helyes,
-- mert azok override-ok.
-- ---------------------------------------------------------------------------
ALTER TABLE campaign_kpis ADD COLUMN IF NOT EXISTS warning_pct numeric;
ALTER TABLE campaign_kpis ADD COLUMN IF NOT EXISTS critical_pct numeric;
ALTER TABLE campaign_kpis ADD COLUMN IF NOT EXISTS inherited_from_client boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN campaign_kpis.inherited_from_client IS
    'true = a /client kpi kaszkád hozta létre; false = kézi /campaign kpi override (a kaszkád nem írja felül).';
