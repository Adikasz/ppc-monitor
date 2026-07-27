"""
Audit log (audit_log tábla) írás.

Minden konfigurációs változást (assign, unassign, set_kpi, mute, rule_set, stb.)
ide naplózunk. Mivel mindenki egyenrangú és bárki módosíthat, az audit log az
egyetlen visszakövetési lehetőség.

Csak írás történik innen — az olvasás (riportok) külön lekérdezéssel végezhető
közvetlenül a Supabase dashboardon vagy egy jövőbeli /audit parancsban.
"""
from __future__ import annotations

from typing import Any

from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

_TABLE = "audit_log"
log = get_logger(__name__)


def log_action(
    discord_user_id: str,
    action: str,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Audit bejegyzés írása.

    Szándékosan tűri a hibát (warning log), mert az audit nem blokkolhatja
    az üzleti műveletet — ha az audit ír sikertelen, az alapművelet már
    megtörtént, és fontos, hogy a felhasználó visszajelzést kapjon.

    Paraméterek:
        discord_user_id  — ki hajtotta végre (Discord snowflake ID str-ként)
        action           — mit csinált: "assign", "unassign", "set_kpi",
                           "mute", "unmute", "rule_set", "client_add", stb.
        entity_type      — mire vonatkozott: "client", "campaign", "kpi",
                           "rule", "mute", "assignment"
        entity_id        — az érintett sor belső DB ID-ja
        details          — szabad jsonb mező, a változás részletei
    """
    payload: dict[str, Any] = {
        "discord_user_id": discord_user_id,
        "action": action,
    }
    if entity_type is not None:
        payload["entity_type"] = entity_type
    if entity_id is not None:
        payload["entity_id"] = entity_id
    if details is not None:
        payload["details"] = details

    try:
        get_supabase().table(_TABLE).insert(payload).execute()
    except Exception:  # noqa: BLE001
        log.warning(
            "Audit log írás sikertelen (action=%s, entity=%s/%s) — folytatás",
            action,
            entity_type,
            entity_id,
            exc_info=True,
        )
