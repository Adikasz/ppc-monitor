"""
ClickUp task-létrehozó (CRITICAL riasztásokhoz).

CRITICAL alert esetén az alert-router ezen keresztül hoz létre egy taskot a
ClickUp "Anomalies" listában, hogy az anomália követhető legyen.

Config:
    CLICKUP_API_TOKEN            — ClickUp személyes API token (pk_...)
    CLICKUP_ANOMALIES_LIST_ID    — a cél lista ("Anomalies") ID-ja

Ha bármelyik hiányzik, vagy a token érvénytelen (401), a task-létrehozás
warning-gal kihagyásra kerül — SOHA nem állítja le a schedulert/routert.

A ClickUp REST API szinkron HTTP (requests); a hívást asyncio.to_thread-ben
futtatjuk, hogy ne blokkolja az event loopot.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from src.config import get_config
from src.utils.logging import get_logger

log = get_logger(__name__)

_API_BASE = "https://api.clickup.com/api/v2"
# ClickUp prioritás: 1=Urgent, 2=High, 3=Normal, 4=Low
_PRIORITY_URGENT = 1
# Határidő: most + ennyi óra
_DUE_HOURS = 2


async def create_clickup_task(
    alert: dict[str, Any],
    campaign: dict[str, Any],
    client: dict[str, Any] | None = None,
    *,
    platform: str | None = None,
    assignee_ids: list[int] | None = None,
) -> dict[str, Any] | None:
    """ClickUp task létrehozása egy CRITICAL alerthez.

    Visszatérés: {"task_id": "..."} siker esetén, különben None. A hívó truthy-
    ként kezeli (van task / nincs), és a task_id-t el is tárolja az
    alerts.clickup_task_id mezőbe — ezért adunk vissza dict-et bool helyett.

    `client` → a task címében a kliensnév; `platform` → a leírásban. Mindkettő
    opcionális: ha hiányzik, "?" kerül a helyére.

    Hiányzó token vagy lista-ID → graceful skip (warning), sosem dob.

    Megjegyzés: az assignee ClickUp user_id-t igényel; Discord→ClickUp user
    leképezés még nincs, ezért alapból assignee nélkül jön létre a task
    (assignee_ids-szel felülírható, ha lesz mapping).
    """
    config = get_config()
    token = config.clickup_api_token
    list_id = config.clickup_anomalies_list_id

    if not token or not list_id:
        log.warning(
            "ClickUp task kihagyva — hiányzik a CLICKUP_API_TOKEN vagy a "
            "CLICKUP_ANOMALIES_LIST_ID."
        )
        return None

    return await asyncio.to_thread(
        _create_task_sync, token, list_id, alert, campaign, client, platform, assignee_ids
    )


def _create_task_sync(
    token: str,
    list_id: str,
    alert: dict[str, Any],
    campaign: dict[str, Any],
    client: dict[str, Any] | None,
    platform: str | None,
    assignee_ids: list[int] | None,
) -> dict[str, Any] | None:
    severity = (alert.get("severity") or "critical").upper()
    campaign_name = campaign.get("name") or "?"
    client_name = (client or {}).get("name") or "?"
    # A campaigns táblában nincs platform — a hívó (router) adja a fiókból; a
    # campaign dict-en is megengedjük (enriched), végső fallback "?".
    platform = platform or campaign.get("platform") or "?"
    detected_at = alert.get("detected_at") or datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    title = f"[{severity}] {client_name} — {campaign_name}"
    description = (
        f"Kampány: {campaign_name}\n"
        f"Platform: {platform}\n"
        f"Probléma: {alert.get('message', '')}\n"
        f"Észlelés: {detected_at}\n"
        f"Alert ID: {alert.get('id')}\n\n"
        f"/campaign info campaign_id:{alert.get('campaign_id')}"
    )
    due_date_ms = int(
        (datetime.now(timezone.utc) + timedelta(hours=_DUE_HOURS)).timestamp() * 1000
    )

    body: dict[str, Any] = {
        "name": title,
        "description": description,
        "priority": _PRIORITY_URGENT,
        "due_date": due_date_ms,
    }
    if assignee_ids:
        body["assignees"] = assignee_ids

    try:
        resp = requests.post(
            f"{_API_BASE}/list/{list_id}/task",
            headers={"Authorization": token, "Content-Type": "application/json"},
            json=body,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001 — hálózati hiba
        log.error("ClickUp hálózati hiba: %s", exc)
        return None

    if resp.status_code == 401:
        log.warning("ClickUp token invalid (401) — task kihagyva")
        return None
    if resp.status_code == 429:
        log.warning("ClickUp rate limit (429) — task kihagyva")
        return None
    if resp.status_code not in (200, 201):
        log.warning("ClickUp task hiba (%s): %s", resp.status_code, resp.text[:200])
        return None

    try:
        task_id = resp.json().get("id")
    except ValueError:
        task_id = None

    log.info("ClickUp task létrehozva: %s (%s)", task_id, title)
    return {"task_id": task_id}
