"""
ClickUp workspace-lekérdezések az admin parancsokhoz (lista, folder, tagok).

Ezt a modult KIZÁRÓLAG az admin parancsok hívják (`/clickup setup-manager`,
`/clickup list-members`, `/clickup status`) — a riasztási úton nincs szerepe.
A három ClickUp modul felosztása:

    clickup.py         — Docs API v3, heti riport Doc
    clickup_router.py  — v2 task API, CRITICAL riasztás → task
    clickup_admin.py   — v2 lekérdezések (ez a fájl): lista/folder ellenőrzés, tagok

A STRUKTÚRÁT NEM MI HOZZUK LÉTRE:
    A Space-t, a PPC managerenkénti Foldert és a bennük lévő Listát az ügyfél
    készíti el KÉZZEL a ClickUp felületén, majd a kész Folder/List ID-kat adja
    meg a `/clickup setup-manager` parancsnak. Ez a modul ezért csak OLVAS:
    ellenőrzi, hogy a beírt azonosító létezik-e és elérhető-e a tokennel.

    Miért így jobb: a struktúra tulajdonosa a csapat marad (jogosultságok,
    nézetek, automatizációk mind a ClickUp UI-ban állíthatók), a bot pedig nem
    tud félkész vagy duplikált Space-eket létrehozni egy félresikerült setupnál.

MIÉRT DOB EZ A MODUL (a másik kettővel ellentétben):
    A riasztási úton a néma degradálás a helyes: egy ClickUp-hiba nem
    akaszthatja meg a Discord riasztást. Egy ADMIN PARANCS viszont pont
    fordítva működik — ha a megadott lista nem létezik, azt az adminnak
    PONTOSAN meg kell tudnia, mielőtt érvénytelen ID kerül az adatbázisba.
    Ezért itt `ClickUpAdminError` repül, emberi (magyar) üzenettel, amit a
    parancs egy az egyben ki tud írni.

A ClickUp REST API szinkron HTTP (requests); minden hívás asyncio.to_thread-ben
fut, hogy ne blokkolja a bot event loopját.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import requests

from src.config import get_config
from src.utils.logging import get_logger

log = get_logger(__name__)

_API_BASE = "https://api.clickup.com/api/v2"
_TIMEOUT_S = 20

# A riasztás-taskok Space-ének JAVASOLT neve. Csak dokumentáció/útmutató: a
# Space-t kézzel hozzák létre, a bot soha nem keres rá és nem tárolja az ID-ját
# — a taskokhoz elég a lista azonosítója.
SPACE_NAME = "PPC Anomália Riasztások"


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
# Azonosító-beolvasás (nyers ID vagy ClickUp link)
# ---------------------------------------------------------------------------

# A ClickUp "Copy link" a listára ilyen URL-t ad:
#     https://app.clickup.com/{workspace}/v/li/{list_id}
# (a régebbi felületen `/v/l/{list_id}`), Folderre pedig `/v/f/` illetve
# `/v/o/f/` szegmenssel. Csak ezeket a MEGNEVEZETT mintákat fogadjuk el.
_ID_PATTERNS: dict[str, tuple[str, ...]] = {
    "list": (r"/v/li/(\d+)", r"/v/l/(\d+)"),
    "folder": (r"/v/o/f/(\d+)", r"/v/f/(\d+)"),
}


def parse_id(raw: str | None, kind: str) -> str | None:
    """ClickUp azonosító kinyerése nyers ID-ből VAGY beillesztett linkből.

    Elfogad:
      - "901234567890"                                  → 901234567890
      - "https://app.clickup.com/9012/v/li/901234567890" → 901234567890
      - "<https://…>" (Discord link-escape)              → ugyanaz

    None, ha nem sikerült EGYÉRTELMŰEN azonosítani. SZÁNDÉKOSAN nem tippelünk
    "az utolsó számjegy-szegmens" alapon: egy Folder-nézet URL-je a Space
    ID-jával is végződhet, és egy elmentett rossz ID pont az a néma hiba, amit
    a validáció el akar kerülni. Ilyenkor a parancs a nyers ID-t kéri be.
    """
    text = (raw or "").strip().strip("<>").strip()
    if not text:
        return None
    if text.isdigit():
        return text
    for pattern in _ID_PATTERNS.get(kind, ()):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


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
            f"{what}: a token nem fér hozzá ehhez az elemhez (403). "
            f"Oszd meg a Space-t a token tulajdonosával a ClickUp-ban."
        )
    if resp.status_code == 404:
        raise ClickUpAdminError(f"{what}: nem található (404).")
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


# ---------------------------------------------------------------------------
# Lista- és folder-ellenőrzés
# ---------------------------------------------------------------------------

def _get_list_sync(list_id: str) -> dict[str, Any]:
    data = _request(
        "GET", f"{_API_BASE}/list/{list_id}",
        what=f"A(z) `{list_id}` lista lekérése",
    )
    folder = data.get("folder") or {}
    space = data.get("space") or {}
    return {
        "id": str(data.get("id") or list_id),
        "name": data.get("name") or "?",
        # Folderless listánál a ClickUp egy REJTETT folder-objektumot ad vissza
        # (`hidden: true`) — az ilyen lista nem egy valódi, kézzel létrehozott
        # Folderben van, ezért a folder ID-t ilyenkor nem tekintjük érvényesnek.
        "folder_id": str(folder.get("id")) if folder.get("id") and not folder.get("hidden") else None,
        "folder_name": folder.get("name") if not folder.get("hidden") else None,
        "folder_hidden": bool(folder.get("hidden")),
        "space_id": str(space.get("id")) if space.get("id") else None,
        "space_name": space.get("name"),
    }


async def get_list(list_id: str) -> dict[str, Any]:
    """Egy lista adatai: `{"id", "name", "folder_id", "folder_name", …}`.

    `ClickUpAdminError`-t dob, ha a lista nem létezik, vagy a token nem fér
    hozzá — a hívó parancs így NEM ment el érvénytelen azonosítót.
    """
    return await asyncio.to_thread(_get_list_sync, str(list_id))


def _get_folder_sync(folder_id: str) -> dict[str, Any]:
    data = _request(
        "GET", f"{_API_BASE}/folder/{folder_id}",
        what=f"A(z) `{folder_id}` Folder lekérése",
    )
    return {"id": str(data.get("id") or folder_id), "name": data.get("name") or "?"}


async def get_folder(folder_id: str) -> dict[str, Any]:
    """Egy Folder adatai: `{"id", "name"}`. `ClickUpAdminError` ha nem elérhető."""
    return await asyncio.to_thread(_get_folder_sync, str(folder_id))


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
