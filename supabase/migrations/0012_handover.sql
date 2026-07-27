-- =============================================================================
-- Migration 0012 — Handover metaadat az account_assignments soron
-- PPC Monitor
-- =============================================================================
-- Az ideiglenes (szabadság) handover-nél feljegyezzük, KI adta át a fiókot és
-- MEDDIG szól az átadás. Ebből tudja a `/account handover-return` pontosan, mely
-- sorokat kell visszavenni (a fogadó user melyik fiókjait kapta handover-rel).
--
--   handover_from_user_id — az eredeti OM (aki átadta); NULL = nem handover-sor
--   handover_until        — az ideiglenes átadás lejárata (audit/infó; nincs
--                           auto-visszaállító job, a return parancs végzi manuálisan)
--
-- Idempotens (IF NOT EXISTS). Kézzel futtatandó a Supabase SQL editorban
-- (nincs supabase CLI). Kétszer lefuttatva sem hibázik. A kód addig is működik:
-- a hiányzó oszlopok esetén a handover metaadat nélkül fut (degradál), a
-- handover-return pedig a "mindkét user rajta van" heurisztikára esik vissza.
-- =============================================================================

ALTER TABLE account_assignments
    ADD COLUMN IF NOT EXISTS handover_from_user_id bigint
        REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS handover_until timestamptz DEFAULT NULL;

COMMENT ON COLUMN account_assignments.handover_from_user_id IS
    'Ideiglenes handover: az eredeti OM (aki átadta) user_id-ja. NULL = nem handover-sor.';
COMMENT ON COLUMN account_assignments.handover_until IS
    'Ideiglenes handover lejárati időpontja (infó; a /account handover-return végzi a visszavételt).';

-- Gyors szűrés a visszaadásnál (fogadó user + eredeti átadó).
CREATE INDEX IF NOT EXISTS account_assignments_handover_idx
    ON account_assignments (user_id, handover_from_user_id);
