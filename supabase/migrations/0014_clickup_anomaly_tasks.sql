-- =============================================================================
-- Migration 0014 — ClickUp anomália-task integráció (struktúra + OM-mapping)
-- PPC Monitor
-- =============================================================================
-- MIÉRT KELL EZ A KÉT TÁBLA:
--
-- A CRITICAL riasztásokhoz ClickUp task készül, az adott kampányért felelős OM
-- SAJÁT listájában, RÁ szignálva. Ehhez három ClickUp azonosító kell (Space,
-- Folder, List), plusz OM-enként egy ClickUp user ID az assignee-hez.
--
-- Ezek NEM kerülhetnek env változóba vagy a kódba:
--   - a Space/Folder/List-eket maga a rendszer hozza létre (`/clickup setup`,
--     `/clickup setup-manager`), tehát az ID-k csak FUTÁSIDŐBEN léteznek;
--   - új PPC manager felvétele így egyetlen Discord parancs, nem deploy.
-- Ezért mindkettő adatbázis-tábla: ügyfél- vagy OM-változásnál egy sor
-- keletkezik, kódot és Railway env változót senki nem szerkeszt.
--
-- Idempotens (IF NOT EXISTS). Kézzel futtatandó a Supabase SQL editorban
-- (nincs supabase CLI). Kétszer lefuttatva sem hibázik. A kód addig is működik:
-- ha a táblák még nincsenek meg, a `storage.clickup_structure` olvasás/írás
-- warninggal degradál — a riasztás Discordon ilyenkor is kimegy, csak ClickUp
-- task nem készül hozzá (lásd `monitoring/router.py` graceful skip).
--
-- MEGJEGYZÉS a `serial` helyett: a projekt konvenciója a
-- `bigint GENERATED ALWAYS AS IDENTITY` (lásd 0013), nem a `serial` —
-- a sequence-ek így nem maradnak le adat-importnál (scripts/_sequences_after_import.sql).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1) clickup_structure — a `/clickup setup` által létrehozott Space
-- -----------------------------------------------------------------------------
-- Egyetlen sor (Workspace-enként egy Space). Azért külön tábla és nem env
-- változó, mert a Space ID-t a setup parancs kapja meg a ClickUp API-tól.
--
-- A `clickup_team_id` szándékosan itt is szerepel: ha a CLICKUP_TEAM_ID
-- (Workspace) valaha megváltozik, a régi Space ID egy MÁSIK workspace-hez
-- tartozna, és a task-létrehozás 404-gyel némán elhalna. A kód a beolvasásnál
-- összeveti a kettőt, és eltérésnél inkább kihagyja a taskot (warninggal),
-- mint hogy rossz helyre írjon.
CREATE TABLE IF NOT EXISTS clickup_structure (
    id               bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- A ClickUp Workspace (team) ID-ja, amelyben a Space létrejött.
    clickup_team_id  text        NOT NULL,

    -- A Space neve, ahogy a ClickUp-ban látszik ("PPC Anomália Riasztások").
    -- Név szerint keresünk rá a setupnál, hogy az újrafuttatás ne duplikáljon.
    space_name       text        NOT NULL,
    clickup_space_id text        NOT NULL,

    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE clickup_structure IS
    'A /clickup setup által létrehozott (vagy megtalált) ClickUp Space. Workspace-enként egy sor.';
COMMENT ON COLUMN clickup_structure.clickup_team_id IS
    'A CLICKUP_TEAM_ID (Workspace) amelyben a Space létrejött — a stale sor felismeréséhez.';
COMMENT ON COLUMN clickup_structure.space_name IS
    'A Space neve; a setup EZ ALAPJÁN keres, hogy többszöri futtatás se duplikáljon.';

-- Workspace + név párra EGY sor: a setup újrafuttatása upsertel, nem duplikál.
CREATE UNIQUE INDEX IF NOT EXISTS uq_clickup_structure_team_space
    ON clickup_structure (clickup_team_id, space_name);


-- -----------------------------------------------------------------------------
-- 2) clickup_manager_mapping — OM → ClickUp Folder/List/assignee
-- -----------------------------------------------------------------------------
-- A `/clickup setup-manager user:@X clickup_user_id:Y` tölti. A riasztás-router
-- innen dönti el, MELYIK listába kerül a task és KIRE szignáljuk.
--
-- Ha egy OM-nek nincs sora, az NEM hiba: a riasztás Discordon kimegy, csak
-- ClickUp task nem készül (warning a logban) — ugyanaz a graceful skip minta,
-- mint a hiányzó CLICKUP_API_TOKEN esetén.
CREATE TABLE IF NOT EXISTS clickup_manager_mapping (
    id                  bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- A MI users táblánk id-ja (nem Discord ID) — a router az assignmentekből
    -- feloldott user_id-val keres, azzal kell egyeznie.
    user_id             bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,

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
    'OM → ClickUp Folder/List/assignee leképezés. A /clickup setup-manager tölti, a CRITICAL riasztás-router olvassa.';
COMMENT ON COLUMN clickup_manager_mapping.user_id IS
    'A users tábla id-ja (NEM discord_user_id) — a router az assignmentekből ezt oldja fel.';
COMMENT ON COLUMN clickup_manager_mapping.clickup_assignee_id IS
    'ClickUp user ID az assignee-hez (/clickup list-members listázza). NULL = task assignee nélkül.';

-- OM-enként EGY mapping: a setup-manager újrafuttatása FRISSÍT, nem duplikál
-- (különben egy alertből két task születne, két listában).
CREATE UNIQUE INDEX IF NOT EXISTS uq_clickup_manager_mapping_user
    ON clickup_manager_mapping (user_id);
