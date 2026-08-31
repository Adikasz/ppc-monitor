"""
ClickUp workspace-struktúra API (Space / Folder / List / tagok).

Ezt a modult KIZÁRÓLAG az admin parancsok hívják (`/clickup setup`,
`/clickup setup-manager`, `/clickup list-members`) — a riasztási úton nincs
szerepe. A három ClickUp modul felosztása:

    clickup.py         — Docs API v3, heti riport Doc
    clickup_router.py  — v2 task API, CRITICAL riasztás → task
    clickup_admin.py   — v2 struktúra API (ez a fájl): Space/Folder/List/tagok

MIÉRT DOB EZ A MODUL (a másik kettővel ellentétben):
    A riasztási úton a néma degradálás a helyes: egy ClickUp-hiba nem
    akaszthatja meg a Discord riasztást. Egy ADMIN PARANCS viszont pont
    fordítva működik — ha a Space létrehozása elhasal, azt az adminnak PONTOSAN
    meg kell tudnia, különben a "0 dolog történt" válasz semmit nem árul el.
    Ezért itt `ClickUpAdminError` repül, emberi (magyar) üzenettel, amit a
    parancs egy az egyben ki tud írni.

IDEMPOTENCIA:
    Minden `ensure_*` függvény ELŐBB NÉV SZERINT KERES, és csak ha nincs
    találat, hoz létre újat. A setup parancsok így többször is futtathatók
    anélkül, hogy a ClickUp megtelne "PPC Anomália Riasztások (2)" Space-ekkel.

    Következmény (tudatosan vállalt): ha valaki a ClickUp UI-ban ÁTNEVEZI a
    Space-t vagy a Foldert, a következő setup ÚJAT hoz létre. A DB-ben tárolt
    ID-k ilyenkor is a régire mutatnak — az átnevezés tehát biztonságos, csak a
    setup újrafuttatása előtt érdemes tudni róla.

A ClickUp REST API szinkron HTTP (requests); minden hívás asyncio.to_thread-ben
fut, hogy ne blokkolja a bot event loopját.
"""
from __future__ import annotations

import asyncio
from typing import Any

import requests

from src.config import get_config
from src.utils.logging import get_logger

log = get_logger(__name__)

_API_BASE = "https://api.clickup.com/api/v2"
_TIMEOUT_S = 20

# A riasztás-taskok Space-e. A név a keresés kulcsa is (lásd IDEMPOTENCIA).
SPACE_NAME = "PPC Anomália Riasztások"
# Az OM Folderén belüli lista neve — ide kerülnek a taskok.
LIST_NAME = "Anomália riasztások"


class ClickUpAdminError(RuntimeError):
    """ClickUp API hiba emberi (magyar) üzenettel — az admin parancs ezt írja ki."""


# ---------------------------------------------------------------------------
# Konfiguráció
# ---------------------------------------------------------------------------

def config_error() -> str | None:
    """Emberi hibaüzenet, ha a ClickUp alap-konfig hiányos; None ha rendben.

    Egy helyen ellenőrizve, hogy a parancs a válaszában PONTOSAN meg tudja
    mondani, mi hiányzik — a néma "nem sikerült" helyett.
    """
    cfg = get_config()
    if not (cfg.clickup_api_token or "").strip():
        return "hiányzik a `CLICKUP_API_TOKEN`"
    if not (cfg.clickup_team_id or "").strip():
        return "hiányzik a `CLICKUP_TEAM_ID` (a ClickUp Workspace ID-ja)"
    return None


def team_id() -> str:
    """A konfigurált Workspace (team) ID. A hívó előtte `config_error()`-t néz."""
    return (get_config().clickup_team_id or "").strip()


def _headers() -> dict[str, str]:
    return {
        "Authorization": (get_config().clickup_api_token or "").strip(),
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# HTTP réteg
# ---------------------------------------------------------------------------

def _request(
    method: str,
    url: str,
    *,
    what: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Egy ClickUp hívás. A válasz JSON-ja, vagy `ClickUpAdminError`.

    A hibaüzenetek szándékosan a ClickUp válaszára hivatkoznak: egy 401 más
    teendőt jelent (token), mint egy 403 (jogosultság) vagy egy 404 (rossz ID).
    """
    try:
        resp = requests.request(
            method, url, headers=_headers(), params=params, json=body, timeout=_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — hálózati hiba
        log.error("ClickUp %s hálózati hiba: %s", what, exc)
        raise ClickUpAdminError(f"{what}: nem sikerült elérni a ClickUp API-t ({exc})") from exc

    if resp.status_code == 401:
        raise ClickUpAdminError(
            f"{what}: a `CLICKUP_API_TOKEN` érvénytelen (401). "
            f"Ellenőrizd a Railway változót."
        )
    if resp.status_code == 403:
        raise ClickUpAdminError(
            f"{what}: a token nem jogosult erre a műveletre (403). "
            f"A ClickUp-ban a token tulajdonosának Space-létrehozási joga kell legyen."
        )
    if resp.status_code == 404:
        raise ClickUpAdminError(
            f"{what}: nem található (404) — rossz `CLICKUP_TEAM_ID`, vagy a "
            f"hivatkozott Space/Folder időközben törlődött."
        )
    if resp.status_code == 429:
        raise ClickUpAdminError(
            f"{what}: ClickUp rate limit (429) — várj egy percet, és futtasd újra."
        )
    if resp.status_code not in (200, 201):
        raise ClickUpAdminError(
            f"{what}: ClickUp hiba ({resp.status_code}) — {resp.text[:200]}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise ClickUpAdminError(f"{what}: a ClickUp válasza nem JSON — {resp.text[:200]}") from exc
    if not isinstance(data, dict):
        raise ClickUpAdminError(f"{what}: váratlan ClickUp válasz-formátum.")
    return data


def _by_name(rows: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """Az első elem, aminek a neve (kis-nagybetű és szóköz nélkül) egyezik."""
    target = name.strip().casefold()
    for row in rows:
        if str(row.get("name") or "").strip().casefold() == target:
            return row
    return None


# ---------------------------------------------------------------------------
# Space
# ---------------------------------------------------------------------------

# A Space létrehozásakor mindhárom mező kötelező (name, multiple_assignees,
# features). Nem kapcsolunk be semmit, ami a riasztás-taskokhoz nem kell —
# a due_dates viszont igen (a task-létrehozás határidőt is állít).
_SPACE_FEATURES: dict[str, Any] = {
    "due_dates": {
        "enabled": True,
        "start_date": False,
        "remap_due_dates": False,
        "remap_closed_due_date": False,
    },
    "time_tracking": {"enabled": False},
    "tags": {"enabled": True},
    "time_estimates": {"enabled": False},
    "checklists": {"enabled": True},
    "custom_fields": {"enabled": True},
    "remap_dependencies": {"enabled": False},
    "dependency_warning": {"enabled": False},
    "portfolios": {"enabled": False},
}


def _list_spaces_sync() -> list[dict[str, Any]]:
    data = _request(
        "GET", f"{_API_BASE}/team/{team_id()}/space",
        what="Space-ek lekérése", params={"archived": "false"},
    )
    return data.get("spaces") or []


def _ensure_space_sync(name: str) -> dict[str, Any]:
    """Meglévő Space név szerint, vagy létrehozás. `{"id", "name", "created"}`."""
    existing = _by_name(_list_spaces_sync(), name)
    if existing is not None:
        log.info("ClickUp Space már létezik: %s (#%s)", name, existing.get("id"))
        return {"id": str(existing.get("id")), "name": existing.get("name") or name,
                "created": False}

    created = _request(
        "POST", f"{_API_BASE}/team/{team_id()}/space",
        what="Space létrehozása",
        body={"name": name, "multiple_assignees": True, "features": _SPACE_FEATURES},
    )
    space_id = created.get("id")
    if not space_id:
        raise ClickUpAdminError("Space létrehozása: a ClickUp válasza `id` nélkül érkezett.")
    log.info("ClickUp Space létrehozva: %s (#%s)", name, space_id)
    return {"id": str(space_id), "name": created.get("name") or name, "created": True}


async def ensure_space(name: str = SPACE_NAME) -> dict[str, Any]:
    """A riasztás-Space megkeresése név szerint, vagy létrehozása.

    Visszatérés: `{"id": str, "name": str, "created": bool}`.
    `ClickUpAdminError`-t dob, ha a ClickUp hívás nem sikerül.
    """
    return await asyncio.to_thread(_ensure_space_sync, name)


# ---------------------------------------------------------------------------
# Folder + List
# ---------------------------------------------------------------------------

def _ensure_folder_sync(space_id: str, name: str) -> dict[str, Any]:
    data = _request(
        "GET", f"{_API_BASE}/space/{space_id}/folder",
        what="Folderek lekérése", params={"archived": "false"},
    )
    existing = _by_name(data.get("folders") or [], name)
    if existing is not None:
        log.info("ClickUp Folder már létezik: %s (#%s)", name, existing.get("id"))
        return {"id": str(existing.get("id")), "name": existing.get("name") or name,
                "created": False}

    created = _request(
        "POST", f"{_API_BASE}/space/{space_id}/folder",
        what="Folder létrehozása", body={"name": name},
    )
    folder_id = created.get("id")
    if not folder_id:
        raise ClickUpAdminError("Folder létrehozása: a ClickUp válasza `id` nélkül érkezett.")
    log.info("ClickUp Folder létrehozva: %s (#%s)", name, folder_id)
    return {"id": str(folder_id), "name": created.get("name") or name, "created": True}


def _ensure_list_sync(folder_id: str, name: str) -> dict[str, Any]:
    data = _request(
        "GET", f"{_API_BASE}/folder/{folder_id}/list",
        what="Listák lekérése", params={"archived": "false"},
    )
    existing = _by_name(data.get("lists") or [], name)
    if existing is not None:
        log.info("ClickUp List már létezik: %s (#%s)", name, existing.get("id"))
        return {"id": str(existing.get("id")), "name": existing.get("name") or name,
                "created": False}

    # SZÁNDÉKOSAN csak a nevet küldjük. A ClickUp v2 "Create List" végpontja
    # NEM tud egyedi TASK-státuszokat (pl. "Nyitva") definiálni — az ottani
    # `status` mező a lista SZÍNCÍMKÉJE, nem a benne használható státuszok.
    # A lista így a Space alapértelmezett státuszait örökli, a task pedig az
    # első (nyitott) státuszban jön létre — pontosan ahogy kértük.
    created = _request(
        "POST", f"{_API_BASE}/folder/{folder_id}/list",
        what="List létrehozása", body={"name": name},
    )
    list_id = created.get("id")
    if not list_id:
        raise ClickUpAdminError("List létrehozása: a ClickUp válasza `id` nélkül érkezett.")
    log.info("ClickUp List létrehozva: %s (#%s)", name, list_id)
    return {"id": str(list_id), "name": created.get("name") or name, "created": True}


async def ensure_folder(space_id: str, name: str) -> dict[str, Any]:
    """Folder megkeresése név szerint a Space-ben, vagy létrehozása."""
    return await asyncio.to_thread(_ensure_folder_sync, str(space_id), name)


async def ensure_list(folder_id: str, name: str = LIST_NAME) -> dict[str, Any]:
    """List megkeresése név szerint a Folderben, vagy létrehozása."""
    return await asyncio.to_thread(_ensure_list_sync, str(folder_id), name)


# ---------------------------------------------------------------------------
# Workspace-tagok (assignee ID-khez)
# ---------------------------------------------------------------------------

def _list_members_sync() -> list[dict[str, Any]]:
    data = _request("GET", f"{_API_BASE}/team", what="Workspace-tagok lekérése")
    wanted = str(team_id())
    for team in data.get("teams") or []:
        if str(team.get("id")) != wanted:
            continue
        out: list[dict[str, Any]] = []
        for member in team.get("members") or []:
            user = member.get("user") or {}
            if user.get("id") is None:
                continue
            out.append({
                "id": str(user.get("id")),
                "username": user.get("username") or "?",
                # Az `email` a hivatalos séma-példában nincs benne, a gyakorlatban
                # viszont gyakran visszajön — ha van, kiírjuk (segít az egyeztetésben).
                "email": user.get("email"),
            })
        return sorted(out, key=lambda m: (m["username"] or "").casefold())
    raise ClickUpAdminError(
        f"Workspace-tagok lekérése: a token nem lát `{wanted}` azonosítójú "
        f"Workspace-t. Ellenőrizd a `CLICKUP_TEAM_ID`-t."
    )


async def list_members() -> list[dict[str, Any]]:
    """A Workspace tagjai: `[{"id", "username", "email"}, …]`, név szerint rendezve.

    A `GET /api/v2/team` végpont minden Workspace-hez visszaadja a tagokat a
    ClickUp user ID-jukkal együtt — ebből a `/clickup setup-manager`-hez
    szükséges `clickup_user_id` egyszerűen kimásolható, nem kell a ClickUp UI-t
    bogarászni.
    """
    return await asyncio.to_thread(_list_members_sync)
