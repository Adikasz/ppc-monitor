"""
ClickUp Docs API v3 — heti riport Doc létrehozása.

Miért külön modul a `clickup_router.py` mellett: az a CRITICAL riasztásokból
TASKOT hoz létre a v2 API-n (`/api/v2/list/{id}/task`). A Docs egy másik API
(`/api/v3/workspaces/...`), más erőforrás-modellel — ugyanabba a fájlba téve
összekeveredne a két, egymástól független integráció.

Két hívás egy Doc létrehozása:
    1. POST /api/v3/workspaces/{workspace_id}/docs
       body: {"name", "parent": {"id", "type"}, "visibility", "create_page": false}
       Szándékosan `create_page: false`: a ClickUp által automatikusan
       létrehozott üres oldal mellé a sajátunk MÁSODIKKÉNT kerülne be, és a Doc
       egy üres lappal nyílna meg.
    2. POST /api/v3/workspaces/{workspace_id}/docs/{doc_id}/pages
       body: {"name", "content", "content_format": "text/md"}

Config:
    CLICKUP_API_TOKEN                 — ClickUp személyes API token (pk_...)
    CLICKUP_TEAM_ID                   — a Workspace (team) ID — a v3 út része
    CLICKUP_WEEKLY_REPORT_FOLDER_ID   — cél Folder  (parent.type = 5)
    CLICKUP_WEEKLY_REPORT_SPACE_ID    — cél Space   (parent.type = 4)

A Folder és a Space közül elég AZ EGYIKET beállítani. Ha mindkettő be van
állítva, a Folder nyer (az a szűkebb hely), és a választásról log készül.

Ha bármelyik kötelező elem hiányzik, a modul NEM dob: a
`weekly_report_config_error()` emberi hibaüzenetet ad, a
`create_weekly_report_doc()` pedig warninggal None-t — pontosan úgy, ahogy a
meglévő `clickup_router.create_clickup_task()` viselkedik hiányzó token esetén.

A ClickUp REST API szinkron HTTP (requests); a hívást asyncio.to_thread-ben
futtatjuk, hogy ne blokkolja a bot event loopját.
"""
from __future__ import annotations

import asyncio
from typing import Any

import requests

from src.config import get_config
from src.utils.logging import get_logger

log = get_logger(__name__)

_API_BASE = "https://api.clickup.com/api/v3"
_APP_BASE = "https://app.clickup.com"

# ClickUp parent.type enum (Docs API v3): 4=Space, 5=Folder, 6=List,
# 7=Everything, 12=Workspace. Csak a kettőt támogatjuk, aminek a heti
# riportnál értelme van.
_PARENT_TYPE_SPACE = 4
_PARENT_TYPE_FOLDER = 5

# A Doc a workspace tagjai számára látható legyen (ne a bot user privát Doc-ja).
_VISIBILITY = "PUBLIC"
_TIMEOUT_S = 20
_CONTENT_FORMAT = "text/md"


def _parent_from_config(cfg: Any) -> tuple[str, int] | None:
    """(parent_id, parent_type) a beállított Folder/Space alapján. None ha egyik sincs."""
    folder_id = (cfg.clickup_weekly_report_folder_id or "").strip()
    space_id = (cfg.clickup_weekly_report_space_id or "").strip()
    if folder_id:
        if space_id:
            log.info(
                "ClickUp heti riport: Folder ÉS Space is be van állítva — "
                "a Folder (#%s) nyer, a Space (#%s) figyelmen kívül marad.",
                folder_id, space_id,
            )
        return folder_id, _PARENT_TYPE_FOLDER
    if space_id:
        return space_id, _PARENT_TYPE_SPACE
    return None


def weekly_report_config_error() -> str | None:
    """Emberi hibaüzenet, ha a heti riport ClickUp-konfigja hiányos; None ha rendben.

    A riport-generátor EGYSZER, a kör elején hívja: így egy hiányzó token nem
    húsz azonos warningként jelenik meg, és a `/report weekly-now` válasza meg
    tudja mondani, PONTOSAN mi hiányzik (a néma "0 Doc készült" helyett).
    """
    cfg = get_config()
    if not (cfg.clickup_api_token or "").strip():
        return "hiányzik a `CLICKUP_API_TOKEN`"
    if not (cfg.clickup_team_id or "").strip():
        return "hiányzik a `CLICKUP_TEAM_ID` (a ClickUp Workspace ID-ja)"
    if _parent_from_config(cfg) is None:
        return (
            "hiányzik a `CLICKUP_WEEKLY_REPORT_FOLDER_ID` és a "
            "`CLICKUP_WEEKLY_REPORT_SPACE_ID` is — legalább az egyik kell"
        )
    return None


async def create_weekly_report_doc(
    title: str,
    markdown: str,
) -> dict[str, Any] | None:
    """Heti riport Doc létrehozása a konfigurált Folder/Space alatt.

    Paraméterek:
        title    — a Doc és az első oldal neve
        markdown — az oldal tartalma (Markdown)

    Visszatérés:
        {"doc_id": "...", "url": "..."} — siker
        None                            — hiányzó konfig VAGY (logolt) API hiba

    Sosem dob: egy ügyfél hibája miatt a hívó nem hagyhatja ki a többit.
    """
    cfg = get_config()
    problem = weekly_report_config_error()
    if problem:
        log.warning("ClickUp heti riport Doc kihagyva — %s.", problem)
        return None

    parent = _parent_from_config(cfg)
    if parent is None:  # a weekly_report_config_error() már kizárta
        return None
    parent_id, parent_type = parent

    return await asyncio.to_thread(
        _create_doc_sync,
        cfg.clickup_api_token.strip(),
        cfg.clickup_team_id.strip(),
        parent_id,
        parent_type,
        title,
        markdown,
    )


def _create_doc_sync(
    token: str,
    workspace_id: str,
    parent_id: str,
    parent_type: int,
    title: str,
    markdown: str,
) -> dict[str, Any] | None:
    """A két HTTP hívás (Doc váz + tartalom-oldal). Szinkron — to_thread-ből hívjuk."""
    headers = {"Authorization": token, "Content-Type": "application/json"}

    # 1) Doc váz
    doc_body = {
        "name": title,
        "parent": {"id": parent_id, "type": parent_type},
        "visibility": _VISIBILITY,
        "create_page": False,
    }
    doc = _post(
        f"{_API_BASE}/workspaces/{workspace_id}/docs",
        headers, doc_body, what="Doc",
    )
    if doc is None:
        return None

    doc_id = doc.get("id")
    if not doc_id:
        log.error("ClickUp Doc válasz `id` nélkül érkezett: %s", str(doc)[:200])
        return None

    # 2) Az oldal a tényleges tartalommal
    page_body = {
        "name": title,
        "content": markdown,
        "content_format": _CONTENT_FORMAT,
    }
    page = _post(
        f"{_API_BASE}/workspaces/{workspace_id}/docs/{doc_id}/pages",
        headers, page_body, what="Doc oldal",
    )
    if page is None:
        # A Doc LÉTEZIK, csak üres. Hibának számoljuk (a riport tartalma nem
        # jutott célba), de kiírjuk az ID-t, hogy kézzel megtalálható legyen.
        log.error(
            "ClickUp: a Doc létrejött (id=%s), de a tartalom feltöltése "
            "elhasalt — a Doc üresen maradt.", doc_id,
        )
        return None

    url = doc.get("url") or f"{_APP_BASE}/{workspace_id}/docs/{doc_id}"
    log.info("ClickUp heti riport Doc létrehozva: %s (%s)", doc_id, title)
    return {"doc_id": doc_id, "url": url}


def _post(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    what: str,
) -> dict[str, Any] | None:
    """Egy POST hívás — a válasz JSON-ja, vagy None (logolt hiba). Sosem dob."""
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — hálózati hiba
        log.error("ClickUp %s hálózati hiba: %s", what, exc)
        return None

    if resp.status_code == 401:
        log.warning("ClickUp %s: token invalid (401) — kihagyva", what)
        return None
    if resp.status_code == 404:
        log.warning(
            "ClickUp %s: 404 — a workspace vagy a cél Folder/Space ID nem "
            "létezik (vagy a token nem fér hozzá). Ellenőrizd a "
            "CLICKUP_TEAM_ID / CLICKUP_WEEKLY_REPORT_* értékeket.", what,
        )
        return None
    if resp.status_code == 429:
        log.warning("ClickUp %s: rate limit (429) — kihagyva", what)
        return None
    if resp.status_code not in (200, 201):
        log.warning(
            "ClickUp %s hiba (%s): %s", what, resp.status_code, resp.text[:200],
        )
        return None

    try:
        data = resp.json()
    except ValueError:
        log.error("ClickUp %s: a válasz nem JSON: %s", what, resp.text[:200])
        return None
    return data if isinstance(data, dict) else {"raw": data}
