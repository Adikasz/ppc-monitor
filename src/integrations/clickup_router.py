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
    *,
    assignee_ids: list[int] | None = None,
) -> dict[str, Any] | None:
    """ClickUp task létrehozása egy CRITICAL alerthez.

    Visszatérés: {"task_id": "..."} siker esetén, különben None.

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
        _create_task_sync, token, list_id, alert, campaign, assignee_ids
    )


def _create_task_sync(
    token: str,
    list_id: str,
    alert: dict[str, Any],
    campaign: dict[str, Any],
    assignee_ids: list[int] | None,
) -> dict[str, Any] | None:
    severity = (alert.get("severity") or "").upper()
    campaign_name = campaign.get("name") or "?"
    metric = alert.get("metric") or "anomaly"

    title = f"[{severity}] {campaign_name} - {metric}"
    description = (
        f"Campaign: {campaign.get('campaign_type') or campaign_name}\n"
        f"Metric: {metric}\n"
        f"Observed: {alert.get('observed_value')}\n"
        f"Threshold: {alert.get('threshold_value')}\n\n"
        f"{alert.get('message', '')}\n\n"
        f"Alert ID: {alert.get('id')}\n"
        f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
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
        log.warning("ClickUp 401 — érvénytelen API token, task kihagyva")
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
