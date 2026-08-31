"""
ClickUp struktúra + OM-mapping adathozzáférés (0014 migration).

Két táblát kezel, mert egy fogalmi egységet alkotnak — "hova és kinek megy a
ClickUp task":

    clickup_structure        — a `/clickup setup` által létrehozott Space
                               (Workspace-enként egy sor)
    clickup_manager_mapping  — OM → Folder / List / assignee leképezés
                               (`/clickup setup-manager` tölti)

MIÉRT NEM DOB EGYIK FÜGGVÉNY SEM:
    A riasztás-router OLVASSA ezeket minden CRITICAL alertnél. Ha a 0014
    migration még nem futott le, vagy a Supabase pillanatnyilag nem elérhető,
    az NEM akaszthatja meg a riasztást — a Discord üzenetnek akkor is ki kell
    mennie. Ezért minden olvasás/írás warninggal degradál (None / False), és
    a hívó dönt: ClickUp task nélkül, Discord-only routinggal folytat.

    Az ÍRÁS oldalon (admin parancsok) ez azt jelenti, hogy a parancs a False
    visszatérésből tud emberi hibaüzenetet adni — nem néma stack trace-t.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

_STRUCTURE_TABLE = "clickup_structure"
_MAPPING_TABLE = "clickup_manager_mapping"

# Minden degradált ág ezt fűzi a warninghoz — a Railway logból azonnal
# kiderüljön, hogy nem kód-hiba, hanem elmaradt migráció a gyanús.
_MIGRATION_HINT = "Lefutott a 0014_clickup_anomaly_tasks migration?"

log = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# clickup_structure — a Space
# ---------------------------------------------------------------------------

def get_space(clickup_team_id: str, space_name: str) -> dict[str, Any] | None:
    """A workspace+név párhoz tartozó elmentett Space sor, vagy None.

    A `clickup_team_id` szűrés nem formaság: ha a CLICKUP_TEAM_ID megváltozik
    (más ClickUp workspace), a régi Space ID egy IDEGEN workspace-re mutatna, és
    a task-létrehozás 404-gyel némán elhalna. Így inkább "nincs setup" választ
    adunk, amit a parancs és a router is kezelni tud.
    """
    try:
        res = (
            get_supabase()
            .table(_STRUCTURE_TABLE)
            .select("*")
            .eq("clickup_team_id", str(clickup_team_id))
            .eq("space_name", space_name)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 — hiányzó tábla / DB hiba
        log.warning("ClickUp Space olvasás sikertelen: %s — %s", exc, _MIGRATION_HINT)
        return None
    return res.data[0] if res.data else None


def save_space(
    clickup_team_id: str,
    space_name: str,
    clickup_space_id: str,
) -> dict[str, Any] | None:
    """A Space elmentése (upsert a `clickup_team_id, space_name` kulcsra).

    Idempotens: a `/clickup setup` többszöri futtatása ugyanazt a sort frissíti.
    Visszatérés: a mentett sor, vagy None (logolt hiba).
    """
    payload = {
        "clickup_team_id": str(clickup_team_id),
        "space_name": space_name,
        "clickup_space_id": str(clickup_space_id),
        "updated_at": _now_iso(),
    }
    try:
        res = (
            get_supabase()
            .table(_STRUCTURE_TABLE)
            .upsert(payload, on_conflict="clickup_team_id,space_name")
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("ClickUp Space mentése sikertelen: %s — %s", exc, _MIGRATION_HINT)
        return None
    return res.data[0] if res.data else payload


# ---------------------------------------------------------------------------
# clickup_manager_mapping — OM → Folder / List / assignee
# ---------------------------------------------------------------------------

def get_mapping_for_user(user_id: int) -> dict[str, Any] | None:
    """Egy OM ClickUp mappingja, vagy None ha nincs (még nem futott setup-manager).

    A CRITICAL riasztás-router hívja MINDEN kritikus alertnél — ezért nem dob:
    egy DB-hiba miatt nem maradhat el a Discord riasztás.
    """
    if user_id is None:
        return None
    try:
        res = (
            get_supabase()
            .table(_MAPPING_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "ClickUp mapping olvasás sikertelen (user #%s): %s — %s",
            user_id, exc, _MIGRATION_HINT,
        )
        return None
    return res.data[0] if res.data else None


def upsert_mapping(
    user_id: int,
    *,
    clickup_folder_id: str,
    clickup_list_id: str,
    clickup_assignee_id: str | None = None,
) -> dict[str, Any] | None:
    """OM mapping létrehozása/frissítése (upsert a `user_id` egyedi kulcsra).

    Idempotens: a `/clickup setup-manager` újrafuttatása FRISSÍTI a sort (pl.
    javított assignee ID), nem hoz létre másodikat — különben egy alertből két
    task születne, két listában.

    Visszatérés: a mentett sor, vagy None (logolt hiba).
    """
    payload: dict[str, Any] = {
        "user_id": user_id,
        "clickup_folder_id": str(clickup_folder_id),
        "clickup_list_id": str(clickup_list_id),
        "clickup_assignee_id": str(clickup_assignee_id) if clickup_assignee_id else None,
        "updated_at": _now_iso(),
    }
    try:
        res = (
            get_supabase()
            .table(_MAPPING_TABLE)
            .upsert(payload, on_conflict="user_id")
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "ClickUp mapping mentése sikertelen (user #%s): %s — %s",
            user_id, exc, _MIGRATION_HINT,
        )
        return None
    return res.data[0] if res.data else payload


def list_mappings() -> list[dict[str, Any]]:
    """Az ÖSSZES OM-mapping, a users adataival kiegészítve (a `/clickup status`-hoz).

    Hiba esetén üres lista (logolt warning) — a státusz-parancs így "nincs
    mapping"-ot mutat, nem hasal el.
    """
    try:
        res = (
            get_supabase()
            .table(_MAPPING_TABLE)
            .select("*, users(id, display_name, discord_user_id)")
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("ClickUp mapping-lista olvasás sikertelen: %s — %s", exc, _MIGRATION_HINT)
        return []
    return res.data or []


def delete_mapping(user_id: int) -> bool:
    """Egy OM mappingjának törlése. True ha volt mit törölni, False egyébként.

    (Offboardingnál / hibás assignee ID visszavonásánál. A `users` sor törlése
    a 0014 ON DELETE CASCADE miatt magától takarít.)
    """
    try:
        existing = (
            get_supabase()
            .table(_MAPPING_TABLE)
            .select("id")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not existing.data:
            return False
        get_supabase().table(_MAPPING_TABLE).delete().eq("user_id", user_id).execute()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "ClickUp mapping törlése sikertelen (user #%s): %s — %s",
            user_id, exc, _MIGRATION_HINT,
        )
        return False
    return True
