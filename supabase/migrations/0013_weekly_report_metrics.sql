-- =============================================================================
-- Migration 0013 — weekly_report_metrics (heti riport aggregátum-cache)
-- PPC Monitor — Heti Riport-összefoglaló & Akciójavaslat modul
-- =============================================================================
-- MIÉRT KELL EZ A TÁBLA:
--
-- A heti riport az ELŐZŐ hét számait az AZT MEGELŐZŐ hét számaihoz hasonlítja
-- ("változás %"). A nyers adat a campaign_insights táblában van — DE azt az
-- óránkénti monitoring ciklus minden futáskor megtisztítja a 7 napnál régebbi
-- soroktól (scheduler.hourly_monitoring → prune_old_insights(7)).
--
-- Hétfő 08:00-kor tehát:
--     előző hét   (−7 … 0 nap)   → MEGVAN a campaign_insights-ban
--     azt megelőző (−14 … −7 nap) → MÁR TÖRÖLVE
--
-- Ezért a job minden futáskor elmenti IDE a most kiszámolt heti aggregátumot,
-- és a KÖVETKEZŐ heti futás innen olvassa az összehasonlító alapot. A tábla
-- tehát nem másodlagos igazságforrás: a heti számokat továbbra is a
-- campaign_insights-ból aggregáljuk, ez csak megőrzi őket a nyers sorok
-- törlése után.
--
-- Következmény (tudatosan vállalt): a legelső futás(ok)nál nincs előző heti
-- sor, ilyenkor a riport "nincs adat"-ot ír a változás oszlopba. A második
-- héttől kezdve teljes.
--
-- Idempotens (IF NOT EXISTS). Kézzel futtatandó a Supabase SQL editorban
-- (nincs supabase CLI). Kétszer lefuttatva sem hibázik. A kód addig is működik:
-- ha a tábla még nincs meg, a cache olvasás/írás warninggal degradál, a riport
-- változás-oszlop nélkül elkészül.
-- =============================================================================

CREATE TABLE IF NOT EXISTS weekly_report_metrics (
    id               bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id        bigint      NOT NULL REFERENCES clients(id) ON DELETE CASCADE,

    -- A hét HÉTFŐJE a konfigurált időzónában (Europe/Budapest), dátumként.
    -- A hét ablaka: [week_start 00:00, week_start + 7 nap 00:00) — fél-nyitott,
    -- ugyanaz a konvenció, mint a summary.daily_range / workweek_range esetén.
    week_start       date        NOT NULL,

    -- Nyers összegek (a hét összes napja × az ügyfél összes kampánya)
    spend            numeric,
    impressions      bigint,
    clicks           bigint,
    conversions      numeric,
    conversion_value numeric,

    -- Származtatott metrikák (a nyers összegekből, nem sorok átlagaként)
    ctr              numeric,    -- clicks / impressions — ARÁNY (pl. 0.0512), nem %
    cpa              numeric,    -- spend / conversions
    roas             numeric,    -- conversion_value / spend; NULL ha nincs bevétel-adat

    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE weekly_report_metrics IS
    'Heti aggregátum-cache a heti riporthoz. A campaign_insights 7 napos retenciója miatt az előző heti összehasonlító alap csak innen elérhető.';
COMMENT ON COLUMN weekly_report_metrics.week_start IS
    'A hét hétfője (Europe/Budapest). Az ablak fél-nyitott: [week_start, week_start + 7 nap).';
COMMENT ON COLUMN weekly_report_metrics.ctr IS
    'clicks / impressions — ARÁNY (0.0512 = 5,12%), a campaign_insights.ctr konvenciójával egyezően.';

-- Ügyfelenként hetente EGY sor. Erre épül az upsert (on_conflict) is: a
-- job kétszeri lefuttatása (cron + /report weekly-now) nem duplikál.
CREATE UNIQUE INDEX IF NOT EXISTS uq_weekly_report_metrics_client_week
    ON weekly_report_metrics (client_id, week_start);

-- Az előző heti sor kikeresése (client_id + week_start) az egyedi indexen megy;
-- ez a plusz index az ügyfél teljes idősorát kéri le gyorsan (későbbi trendekhez).
CREATE INDEX IF NOT EXISTS weekly_report_metrics_client_idx
    ON weekly_report_metrics (client_id, week_start DESC);
