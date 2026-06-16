-- =============================================================================
-- Migration 0007 — alerts.email_sent_at (ügyfél-email deduplikáció)
-- PPC Monitor — 14. lépés: email router CRITICAL alertekhez
-- =============================================================================
-- A CRITICAL riasztásokról emailt küldünk az ügyfél contact_email címére.
-- Hogy ugyanarra az anomáliára ne menjen kétszer email (újra-routing, retry),
-- a kiküldés időpontját ide írjuk. NULL → még nem ment email; kitöltve → skip.
-- =============================================================================

ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS email_sent_at timestamptz;

COMMENT ON COLUMN alerts.email_sent_at IS
    'Mikor ment ki az ügyfél-email erről az alertről (NULL = még nem). Email-dedup.';
