"""
ClickUp task-létrehozó (anomália riasztásokhoz).

CRITICAL alert esetén az alert-router ezen keresztül hoz létre egy taskot AZ
ADOTT OM SAJÁT LISTÁJÁBAN, rá szignálva — a lista és az assignee a
`clickup_manager_mapping` táblából jön (`/clickup setup-manager` tölti,
0014 migration). Nincs env-be drótozott lista-ID: új PPC manager felvétele
egyetlen Discord parancs.

MELY SEVERITY-KRE KÉSZÜL TASK — `TASK_SEVERITIES`:
    Alapértelmezés: CSAK `critical`. Ez tudatos szűkítés — a WARNING napi
    szinten sokszorosa a CRITICAL-nak, és egy elárasztott ClickUp lista pont
    olyan használhatatlan, mint egy néma. Ha a WARNING is kelljen, EGYETLEN
    konstans bővítendő (`TASK_SEVERITIES`), a prioritás-leképezés
    (`_PRIORITY_BY_SEVERITY`) már készen áll rá (High = 2).

Hibatűrés (a riasztási út vasszabálya):
    Egyetlen függvény sem dob. Hiányzó token, hiányzó mapping, API hiba →
    warning + None, és a Discord riasztás MEGY KI ClickUp task nélkül. Egy
    ClickUp-probléma sosem nyelheti el magát a riasztást.
    (Az admin parancsok ezzel szemben `clickup_admin.py`-ban DOBNAK — ott a
    néma degradálás lenne a rossz válasz.)

A ClickUp REST API szinkron HTTP (requests); a hívások asyncio.to_thread-ben
futnak, hogy ne blokkolják a bot event loopját.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from src.config import get_config
from src.integrations import alert_content
from src.utils.logging import get_logger

log = get_logger(__name__)

_API_BASE = "https://api.clickup.com/api/v2"

# Mely riasztás-súlyosságokra készül ClickUp task. Lásd a modul-docstringet:
# a bővítés (pl. {"critical", "warning"}) szándékosan EGY sor.
TASK_SEVERITIES: frozenset[str] = frozenset({"critical"})

# ClickUp prioritás: 1=Urgent, 2=High, 3=Normal, 4=Low.
# Szigorúan severity-alapú leképezés — az eltérés MÉRTÉKE szándékosan NEM
# számít bele (az ügyfél is elhagyhatónak jelölte; külön finomítás lehet).
_PRIORITY_BY_SEVERITY: dict[str, int] = {
    "critical": 1,   # Urgent
    "warning": 2,    # High — csak ha a TASK_SEVERITIES bővül
}
_PRIORITY_FALLBACK = 3  # Normal

# Határidő: most + ennyi óra
_DUE_HOURS = 2
_TIMEOUT_S = 15


def is_task_severity(severity: str | None) -> bool:
    """Készül-e ClickUp task erre a severity-re (lásd `TASK_SEVERITIES`)."""
    return (severity or "").lower() in TASK_SEVERITIES


# ---------------------------------------------------------------------------
# Task létrehozása
# ---------------------------------------------------------------------------

async def create_clickup_task(
    alert: dict[str, Any],
    campaign: dict[str, Any],
    client: dict[str, Any] | None = None,
    *,
    platform: str | None = None,
    mapping: dict[str, Any],
) -> dict[str, Any] | None:
    """ClickUp task létrehozása egy riasztáshoz, az OM saját listájában.

    Paraméterek:
        alert    — az alert sor (severity, message, observed_value, detected_at, …)
        campaign — a kampány sor (név)
        client   — az ügyfél sor (a task címében a kliensnév); opcionális
        platform — "meta" / "google" (a router a fiókból oldja fel); opcionális
        mapping  — a `clickup_manager_mapping` sor: `clickup_list_id` +
                   opcionális `clickup_assignee_id`. A hívó feladata feloldani;
                   ha nincs mapping, ezt a függvényt NEM hívja meg.

    Visszatérés:
        {"task_id", "url", "description"} — siker
        None                              — hiányzó token/lista VAGY (logolt) API hiba

    A `description` azért van a válaszban, hogy a Discord-link utólagos
    hozzáfűzéséhez (`append_discord_link`) ne kelljen külön GET-tel visszaolvasni
    a taskot — a szöveget mi állítottuk elő, ismerjük.

    Sosem dob.
    """
    token = (get_config().clickup_api_token or "").strip()
    list_id = str((mapping or {}).get("clickup_list_id") or "").strip()

    if not token:
        log.warning("ClickUp task kihagyva — hiányzik a CLICKUP_API_TOKEN.")
        return None
    if not list_id:
        log.warning(
            "ClickUp task kihagyva — a mappingben nincs `clickup_list_id` "
            "(alert #%s). Futott már a `/clickup setup-manager`?",
            alert.get("id"),
        )
        return None

    assignee_raw = (mapping or {}).get("clickup_assignee_id")
    return await asyncio.to_thread(
        _create_task_sync, token, list_id, alert, campaign, client, platform, assignee_raw,
    )


def _assignee_ids(assignee_raw: Any) -> list[int]:
    """A mapping assignee ID-ja ClickUp-formában (`[int]`), vagy üres lista.

    A ClickUp `assignees` numerikus user ID-kat vár. A DB textként tárolja (mint
    a többi külső ID-t), és az admin gépeli be — egy elgépelt, nem numerikus
    érték miatt viszont nem maradhat el a TASK: ilyenkor inkább assignee nélkül
    hozzuk létre, warninggal.
    """
    if assignee_raw in (None, ""):
        return []
    try:
        return [int(str(assignee_raw).strip())]
    except (TypeError, ValueError):
        log.warning(
            "ClickUp assignee ID nem szám: %r — a task assignee nélkül jön létre. "
            "Javítás: `/clickup setup-manager` (a helyes ID-t a "
            "`/clickup list-members` mutatja).",
            assignee_raw,
        )
        return []


def _create_task_sync(
    token: str,
    list_id: str,
    alert: dict[str, Any],
    campaign: dict[str, Any],
    client: dict[str, Any] | None,
    platform: str | None,
    assignee_raw: Any,
) -> dict[str, Any] | None:
    severity = (alert.get("severity") or "critical").lower()
    campaign_name = campaign.get("campaign_type") or campaign.get("name") or "?"
    client_name = (client or {}).get("name") or "?"
    # A campaigns táblában nincs platform — a hívó (router) adja a fiókból; a
    # campaign dict-en is megengedjük (enriched), végső fallback "?".
    platform = platform or campaign.get("platform") or "?"

    # A cím és a leírás a KÖZÖS formázóból jön: pontosan azt a szöveget
    # keretezi, amit a Discord üzenet is mutat (lásd integrations/alert_content.py).
    title = alert_content.clickup_task_title(
        client_name=client_name,
        platform=platform,
        message=alert.get("message"),
        observed_value=alert.get("observed_value"),
    )
    description = alert_content.clickup_task_description(
        campaign_name=campaign_name,
        platform=platform,
        message=alert.get("message"),
        observed_value=alert.get("observed_value"),
        threshold_value=alert.get("threshold_value"),
        detected_at_text=alert_content.detected_at_label(alert, get_config().timezone),
        alert_id=alert.get("id"),
        campaign_id=alert.get("campaign_id"),
    )

    due_date_ms = int(
        (datetime.now(timezone.utc) + timedelta(hours=_DUE_HOURS)).timestamp() * 1000
    )

    body: dict[str, Any] = {
        "name": title,
        "description": description,
        "priority": _PRIORITY_BY_SEVERITY.get(severity, _PRIORITY_FALLBACK),
        "due_date": due_date_ms,
    }
    # SZÁNDÉKOSAN nincs `status` a bodyban: a task a lista ALAPÉRTELMEZETT
    # (első, nyitott) státuszában jön létre. Egy kitalált státusznév ("Nyitva")
    # 400-zal szállna el minden olyan listán, ahol nincs így elnevezve.
    assignees = _assignee_ids(assignee_raw)
    if assignees:
        body["assignees"] = assignees

    data = _post_json(f"{_API_BASE}/list/{list_id}/task", token, body, what="task")
    if data is None:
        return None

    task_id = data.get("id")
    if not task_id:
        log.error("ClickUp task válasz `id` nélkül érkezett: %s", str(data)[:200])
        return None

    url = data.get("url") or f"https://app.clickup.com/t/{task_id}"
    log.info(
        "ClickUp task létrehozva: %s (lista=%s, assignee=%s) — %s",
        task_id, list_id, assignees or "nincs", title,
    )
    return {"task_id": str(task_id), "url": url, "description": description}


# ---------------------------------------------------------------------------
# Discord-link visszaírása a taskra
# ---------------------------------------------------------------------------

async def append_discord_link(
    task_id: str,
    description: str,
    discord_message_url: str,
) -> bool:
    """A kiküldött Discord üzenet linkjének hozzáfűzése a task leírásához.

    A router hívja MIUTÁN a Discord üzenet kiment — így lesz a linkelés
    kétirányú (a Discord üzenetben ott a task, a taskon ott az üzenet).

    A ClickUp v2-ben a task módosítása **PUT** `/task/{id}` (nincs PATCH
    végpont); a küldött body ettől még részleges — csak a `description` mezőt
    írjuk felül, a többi mező érintetlen marad.

    A teljes (korábbi + link) szöveget küldjük, mert a ClickUp a `description`-t
    lecseréli, nem hozzáfűzi. Az eredeti szöveget nem kell visszaolvasnunk: mi
    állítottuk elő, a `create_clickup_task` vissza is adta.

    Visszatérés: True siker esetén, False (logolt warning) egyébként. Sosem dob:
    egy hiányzó Discord-link kellemetlen, de nem kritikus — a task attól még ott
    van, a riasztás pedig már kiment.
    """
    token = (get_config().clickup_api_token or "").strip()
    if not token or not task_id or not discord_message_url:
        return False

    new_description = f"{description}\n\n{alert_content.discord_link_line(discord_message_url)}"
    data = await asyncio.to_thread(
        _put_json,
        f"{_API_BASE}/task/{task_id}",
        token,
        {"description": new_description},
        what="task Discord-link",
    )
    if data is None:
        log.warning(
            "ClickUp task #%s Discord-linkje nem íródott vissza — a task link "
            "nélkül marad (a riasztás már kiment).", task_id,
        )
        return False

    log.info("ClickUp task #%s kiegészítve a Discord üzenet linkjével", task_id)
    return True


# ---------------------------------------------------------------------------
# HTTP réteg — sosem dob, hibánál None (a hívó degradál)
# ---------------------------------------------------------------------------

def _post_json(url: str, token: str, body: dict[str, Any], *, what: str) -> dict[str, Any] | None:
    return _send_json("POST", url, token, body, what)


def _put_json(url: str, token: str, body: dict[str, Any], *, what: str) -> dict[str, Any] | None:
    return _send_json("PUT", url, token, body, what)


def _send_json(
    method: str,
    url: str,
    token: str,
    body: dict[str, Any],
    what: str,
) -> dict[str, Any] | None:
    try:
        resp = requests.request(
            method, url,
            headers={"Authorization": token, "Content-Type": "application/json"},
            json=body, timeout=_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — hálózati hiba
        log.error("ClickUp %s hálózati hiba: %s", what, exc)
        return None

    if resp.status_code == 401:
        log.warning("ClickUp token invalid (401) — %s kihagyva", what)
        return None
    if resp.status_code == 404:
        log.warning(
            "ClickUp %s: 404 — a cél lista/task nem létezik (vagy a token nem "
            "fér hozzá). Ellenőrizd a `clickup_manager_mapping` sorát: "
            "`/clickup status`.", what,
        )
        return None
    if resp.status_code == 429:
        log.warning("ClickUp rate limit (429) — %s kihagyva", what)
        return None
    if resp.status_code not in (200, 201):
        log.warning("ClickUp %s hiba (%s): %s", what, resp.status_code, resp.text[:200])
        return None

    try:
        data = resp.json()
    except ValueError:
        log.error("ClickUp %s: a válasz nem JSON: %s", what, resp.text[:200])
        return None
    return data if isinstance(data, dict) else {"raw": data}
