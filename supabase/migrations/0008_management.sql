-- =============================================================================
-- Migration 0008 — Management command suite támogatás
-- PPC Monitor — 14. lépés: /client, /adaccount, /discover parancsok
-- =============================================================================
-- A management parancsok minden szükséges oszlopa MÁR létezik a sémában
-- (contact_email a 0003-ban került be), KIVÉVE az ügyfél-szintű insights
-- kapcsolót. Ezt itt vesszük fel. Idempotens: IF NOT EXISTS, így újrafuttatható.
--
-- Megjegyzés: a projekt migrációit kézzel alkalmazzuk a Supabase SQL editorban
-- (nincs supabase CLI a repóban). Ezt az egy fájlt egyszer kell lefuttatni.
-- =============================================================================

ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS insights_enabled boolean NOT NULL DEFAULT true;

COMMENT ON COLUMN clients.insights_enabled IS
    'Ügyfél-szintű insight/AI elemzés kapcsoló (/client insights). true = bekapcsolva. Meglévő sorok automatikusan true-t kapnak.';
