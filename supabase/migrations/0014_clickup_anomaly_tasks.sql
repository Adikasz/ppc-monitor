-- =============================================================================
-- Migration 0014 — ClickUp anomália-task integráció (OM → lista leképezés)
-- PPC Monitor
-- =============================================================================
-- MIÉRT KELL EZ A TÁBLA:
--
-- A CRITICAL riasztásokhoz ClickUp task készül, az adott kampányért felelős OM
-- SAJÁT listájában, RÁ szignálva. Ehhez OM-enként két ClickUp azonosító kell
-- (a Folder és a benne lévő List), plusz egy ClickUp user ID az assignee-hez.
--
-- A ClickUp STRUKTÚRÁT (Space → Folderek → Listák) az ügyfél hozza létre KÉZZEL
-- a ClickUp felületén; a kész azonosítókat a `/clickup setup-manager` parancsnak
-- adja meg, ami a mentés ELŐTT ellenőrzi őket a ClickUp API-n.
--
-- Ezek az ID-k NEM kerülhetnek env változóba vagy a kódba: minden új PPC
-- manager egy újabb Folder+List párt jelentene, tehát egy újabb deployt. Így
-- viszont az OM felvétele egyetlen Discord parancs.
--
-- SZÁNDÉKOSAN NINCS Space-tábla: a task létrehozásához a lista azonosítója
-- elég, a Space ID-t soha nem használnánk — egy olvasatlan oszlop pedig csak
-- azt a látszatot keltené, hogy számít valamiben.
--
-- Idempotens (IF NOT EXISTS). Kézzel futtatandó a Supabase SQL editorban
-- (nincs supabase CLI). Kétszer lefuttatva sem hibázik. A kód addig is működik:
-- ha a tábla még nincs meg, a `storage.clickup_mapping` olvasás/írás warninggal
-- degradál — a riasztás Discordon ilyenkor is kimegy, csak ClickUp task nem
-- készül hozzá (lásd `monitoring/router.py` graceful skip).
--
-- MEGJEGYZÉS a `serial` helyett: a projekt konvenciója a
-- `bigint GENERATED ALWAYS AS IDENTITY` (lásd 0013), nem a `serial` —
-- a sequence-ek így nem maradnak le adat-importnál (scripts/_sequences_after_import.sql).
-- =============================================================================

CREATE TABLE IF NOT EXISTS clickup_manager_mapping (
    id                  bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- A MI users táblánk id-ja (nem Discord ID) — a router az assignmentekből
    -- feloldott user_id-val keres, azzal kell egyeznie.
    user_id             bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- A ClickUp-ban KÉZZEL létrehozott Folder és a benne lévő List azonosítója.
    -- A folder_id-t nem a task-létrehozás használja (ahhoz a lista elég), hanem
    -- a `/clickup status` ellenőrzés és az emberi visszakövethetőség: ebből
    -- derül ki, MELYIK manager-mappájából való a lista.
    clickup_folder_id   text        NOT NULL,
    clickup_list_id     text        NOT NULL,

    -- A ClickUp-os assignee azonosító (numerikus, de textként tároljuk, mint a
    -- többi külső ID-t a projektben). NULLABLE: a mapping assignee nélkül is
    -- értelmes — a task létrejön a jó listában, csak nincs ráakasztva senki.
    clickup_assignee_id text,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE clickup_manager_mapping IS
    'OM → ClickUp Folder/List/assignee leképezés. A /clickup setup-manager tölti (kézzel létrehozott ClickUp struktúrából), a CRITICAL riasztás-router olvassa.';
COMMENT ON COLUMN clickup_manager_mapping.user_id IS
    'A users tábla id-ja (NEM discord_user_id) — a router az assignmentekből ezt oldja fel.';
COMMENT ON COLUMN clickup_manager_mapping.clickup_folder_id IS
    'A manager ClickUp Foldere. Nem a task-létrehozáshoz kell, hanem az ellenőrzéshez/visszakövetéshez.';
COMMENT ON COLUMN clickup_manager_mapping.clickup_list_id IS
    'A cél lista — ide kerülnek a CRITICAL riasztás-taskok.';
COMMENT ON COLUMN clickup_manager_mapping.clickup_assignee_id IS
    'ClickUp user ID az assignee-hez (/clickup list-members listázza). NULL = task assignee nélkül.';

-- OM-enként EGY mapping: a setup-manager újrafuttatása FRISSÍT, nem duplikál
-- (különben egy alertből két task születne, két listában).
CREATE UNIQUE INDEX IF NOT EXISTS uq_clickup_manager_mapping_user
    ON clickup_manager_mapping (user_id);
