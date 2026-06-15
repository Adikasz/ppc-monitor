-- =============================================================================
-- Migration 0006 — users.alerts_channel_id (per-OM személyes alert csatorna)
-- PPC Monitor — 11. lépés: per-OM alert csatornák + multi-assignee + admin fallback
-- =============================================================================
-- Eddig MINDEN alert egyetlen közös csatornára ment (DISCORD_ALERTS_CHANNEL_ID).
-- Mostantól minden OM a SAJÁT csatornájába kapja a hozzárendelt kampányai
-- riasztásait. Ezt az itt felvett oszlop tárolja (Discord channel ID, text).
-- Ha egy OM-nek nincs beállítva csatornája, a riasztás az admin csatornára megy
-- (fallback), egy figyelmeztetéssel együtt.
-- =============================================================================

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS alerts_channel_id text;

COMMENT ON COLUMN users.alerts_channel_id IS
    'Discord channel ID for personal alerts (assigned campaigns only)';
