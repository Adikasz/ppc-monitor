"""
OM → ClickUp leképezés adathozzáférés (`clickup_manager_mapping`, 0014 migration).

Egyetlen táblát kezel: melyik PPC manager riasztásaiból melyik ClickUp listába
készül task, és kire szignáljuk.

    user_id             — a MI users táblánk id-ja
    clickup_folder_id   — a manager Foldere (kézzel hozták létre a ClickUp-ban)
    clickup_list_id     — a Folderben lévő lista, ide kerülnek a taskok
    clickup_assignee_id — a ClickUp user ID az assignee-hez

A sorokat a `/clickup setup-manager` írja, a KÉZZEL létrehozott ClickUp
struktúrából kimásolt azonosítókkal. A parancs a mentés előtt ellenőrzi az
azonosítókat a ClickUp API-n — ide tehát csak létező, elérhető ID kerül.
(Space ID-t szándékosan NEM tárolunk: a taskhoz a lista azonosítója elég, és
egy olvasatlan oszlop csak félrevezetne.)

MIÉRT NEM DOB EGYIK FÜGGVÉNY SEM:
    A riasztás-router OLVASSA ezt minden CRITICAL alertnél. Ha a 0014 migration
    még nem futott le, vagy a Supabase pillanatnyilag nem elérhető, az NEM
    akaszthatja meg a riasztást — a Discord üzenetnek akkor is ki kell mennie.
    Ezért minden olvasás/írás warninggal degradál (None / False / üres lista),
    és a hívó dönt: ClickUp task nélkül, Discord-only routinggal folytat.

    Az ÍRÁS oldalon (admin parancsok) ez azt jelenti, hogy a parancs a None
    visszatérésből tud emberi hibaüzenetet adni — nem néma stack trace-t.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

_TABLE = "clickup_manager_mapping"

# Minden degradált ág ezt fűzi a warninghoz — a Railway logból azonnal
# kiderüljön, hogy nem kód-hiba, hanem elmaradt migráció a gyanús.
_MIGRATION_HINT = "Lefutott a 0014_clickup_anomaly_tasks migration?"

log = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            .table(_TABLE)
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
    javított assignee ID vagy áthelyezett lista), nem hoz létre másodikat —
    különben egy alertből két task születne, két listában.

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
            .table(_TABLE)
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
            .table(_TABLE)
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
            .table(_TABLE)
            .select("id")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not existing.data:
            return False
        get_supabase().table(_TABLE).delete().eq("user_id", user_id).execute()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "ClickUp mapping törlése sikertelen (user #%s): %s — %s",
            user_id, exc, _MIGRATION_HINT,
        )
        return False
    return True
