-- =============================================================================
-- Migration 0010 — Fiók-szintű (ad account) assign + KPI kaszkád
-- PPC Monitor — 21. lépés
-- =============================================================================
-- A hozzárendelés és a KPI egysége a CLIENT-ről az AD ACCOUNT-ra kerül: a Meta-
-- és a Google-fiók külön OM-hez és külön KPI-hoz tartozhat. A client ernyő marad.
--
-- Idempotens (IF NOT EXISTS). Kézzel futtatandó a Supabase SQL editorban
-- (nincs supabase CLI — lásd a 0008 megjegyzést). Kétszer lefuttatva sem hibázik.
--
-- A régi client_kpis tábla és az assignments.inherited_from_client /
-- campaign_kpis.inherited_from_client mezők MEGMARADNAK (adatmegőrzés) — csak
-- már nem a kaszkád elsődleges forrásai.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Fiók-szintű hozzárendelés (a kliens-szintű helyett)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS account_assignments (
    id            bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    ad_account_id bigint NOT NULL REFERENCES ad_accounts(id) ON DELETE CASCADE,
    user_id       bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role          text DEFAULT 'primary' CHECK (role IN ('primary', 'supporter')),
    created_at    timestamptz DEFAULT now(),
    UNIQUE (ad_account_id, user_id)
);

COMMENT ON TABLE account_assignments IS
    'Fiók-szintű OM-hozzárendelés (21. lépés). Lecsorog a fiók kampányainak assignments soraiba; új kampány a discovery-ben örökli.';

-- ---------------------------------------------------------------------------
-- Fiók-szintű KPI (a client_kpis fiók-szintű megfelelője)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ad_account_kpis (
    id                       bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    ad_account_id            bigint NOT NULL REFERENCES ad_accounts(id) ON DELETE CASCADE,
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
    UNIQUE (ad_account_id)
);

COMMENT ON TABLE ad_account_kpis IS
    'Fiók-szintű KPI default + warning/critical százalékok (21. lépés). /account kpi állítja; lecsorog a campaign_kpis-ba, a detektor innen örököl (campaign → ad_account → client → default).';

-- ---------------------------------------------------------------------------
-- Öröklés-jelölők a fiók-szintű kaszkádhoz
-- ---------------------------------------------------------------------------
ALTER TABLE assignments
    ADD COLUMN IF NOT EXISTS inherited_from_account boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN assignments.inherited_from_account IS
    'true = a kampány-szintű hozzárendelést a fiók-szintű (/account assign) kaszkád vagy a discovery öröklés hozta létre.';

ALTER TABLE campaign_kpis
    ADD COLUMN IF NOT EXISTS inherited_from_account boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN campaign_kpis.inherited_from_account IS
    'true = a sort a fiók-szintű (/account kpi) kaszkád hozta létre. Kézi override = inherited_from_account=false ÉS inherited_from_client=false (a kaszkád nem írja felül).';
