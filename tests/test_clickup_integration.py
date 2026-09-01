"""
ClickUp anomália-task integráció — mapping, routing, hiba-izoláció.

Hat elvárás áll a tesztek mögött:

1. ÉRVÉNYTELEN AZONOSÍTÓ NEM KERÜLHET AZ ADATBÁZISBA. A ClickUp struktúrát
   (Space → Folderek → Listák) az ügyfél hozza létre KÉZZEL, és a kimásolt
   ID-kat adja meg a `/clickup setup-manager`-nek. A parancs ezért MENTÉS
   ELŐTT ellenőrzi a listát a ClickUp API-n, és azt is, hogy tényleg a
   megadott Folderben van-e: egy elgépelt lista-ID némán nyelné el az összes
   későbbi riasztás-taskot.

2. A TASK SZÖVEGE UGYANONNAN JÖN, MINT A DISCORD ÜZENETÉ. Egyetlen közös
   formázó (`integrations/alert_content.py`) adja a kliens/platform címkét, az
   anomália leírását, az értéket és az időbélyeget. Ha ez szétcsúszik, a task és
   az üzenet ugyanarról a riasztásról más számot mutatna — élesben, némán.

3. HIÁNYZÓ MAPPING = GRACEFUL SKIP, NEM NÉMA KIMARADÁS. Ha az OM-nek még nincs
   ClickUp mappingja, a riasztás Discordon KIMEGY, és a logban WARNING marad
   arról, hogy miért nem készült task — ugyanaz a minta, mint a hiányzó
   CLICKUP_API_TOKEN esetén.

4. HIBA-IZOLÁCIÓ MINDKÉT IRÁNYBAN. ClickUp-hiba nem akaszthatja meg a Discord
   riasztást; Discord-hiba nem viheti magával a taskot.

5. A KÉTIRÁNYÚ LINKELÉS SORRENDJE KÖTÖTT: task létrejön → Discord üzenet megy
   (benne a task linkje) → task frissül (benne az üzenet ugrólinkje). Ez a
   sorrend a funkció lényege, ezért külön, hívás-naplóval ellenőrizzük.

6. A TÁBLA HIÁNYA NEM DÖNTI ROMBA A RIASZTÁST. Ha a 0014 migration még nem
   futott le, a storage réteg warninggal degradál (None / False), nem dob.
"""
from __future__ import annotations

import contextlib
import logging
from types import SimpleNamespace
from unittest import mock

import pytest

from src.bot.commands import clickup as clickup_cmd
from src.integrations import alert_content
from src.integrations import clickup_admin
from src.integrations.clickup_admin import ClickUpAdminError
from src.integrations import clickup_router
from src.integrations import discord_router
from src.monitoring import router
from src.storage import clickup_mapping as clickup_storage


# ===========================================================================
# Segédek
# ===========================================================================

class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    """Memóriában futó Supabase query-lánc; opcionálisan hibát dob (nincs tábla)."""

    def __init__(self, store: dict, table: str, raise_msg: str | None = None):
        self._store = store
        self._table = table
        self._raise = raise_msg
        self._eq: list[tuple] = []
        self._mode = "select"
        self._payload: dict | None = None

    # -- lánc-elemek --------------------------------------------------------
    def select(self, *_a, **_k):
        self._mode = "select"
        return self

    def upsert(self, payload, on_conflict=None):  # noqa: ANN001
        self._mode = "upsert"
        self._payload = payload
        self._conflict = (on_conflict or "").split(",")
        return self

    def delete(self):
        self._mode = "delete"
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def limit(self, _n):
        return self

    # -- végrehajtás --------------------------------------------------------
    def execute(self):
        if self._raise:
            raise Exception(self._raise)
        rows = self._store.setdefault(self._table, [])

        if self._mode == "upsert":
            key = [c.strip() for c in self._conflict if c.strip()]
            for row in rows:
                if all(str(row.get(k)) == str(self._payload.get(k)) for k in key):
                    row.update(self._payload)
                    return _Resp([dict(row)])
            new_row = {"id": len(rows) + 1, **self._payload}
            rows.append(new_row)
            return _Resp([dict(new_row)])

        matched = [r for r in rows if all(str(r.get(c)) == str(v) for c, v in self._eq)]
        if self._mode == "delete":
            for row in matched:
                rows.remove(row)
        # MÁSOLATOT adunk vissza, ahogy a valódi PostgREST is: így egy későbbi
        # frissítés nem írja át visszamenőleg a korábban lekért sort (ez a
        # különbség egy naiv fake-nél hamis "átment" eredményt adna).
        return _Resp([dict(r) for r in matched])


class _SB:
    """Minimális Supabase kliens a storage-tesztekhez."""

    def __init__(self, store: dict | None = None, raise_msg: str | None = None):
        self.store = store if store is not None else {}
        self._raise = raise_msg

    def table(self, name):  # noqa: ANN001
        return _Query(self.store, name, self._raise)


@contextlib.contextmanager
def _capture(caplog, *logger_names, level=logging.WARNING):
    """A projekt loggereire köti a caplog handlerét (a projekt konvenciója).

    A `utils.logging.get_logger` `propagate=False`-t állít (hogy ne duplázódjon
    a log), ezért a caplog magától NEM lát semmit — a handlert kézzel kell
    rákötni, ahogy a `test_morning_notifications` is teszi.
    """
    caplog.set_level(level)
    loggers = [logging.getLogger(name) for name in logger_names]
    for lg in loggers:
        lg.addHandler(caplog.handler)
    try:
        yield
    finally:
        for lg in loggers:
            lg.removeHandler(caplog.handler)


_LOG_ROUTER = "src.monitoring.router"
_LOG_TASK = "src.integrations.clickup_router"
_LOG_STORAGE = "src.storage.clickup_mapping"


class _Interaction:
    def __init__(self, channel_id=1, user_id=42):
        self.channel_id = channel_id
        self.user = mock.Mock(id=user_id)
        self.response = mock.AsyncMock()
        self.followup = mock.AsyncMock()
        self.channel = mock.AsyncMock()

    def sent(self) -> list[str]:
        return [c.args[0] for c in self.followup.send.call_args_list if c.args]


def _alert(severity="critical", **extra) -> dict:
    return {
        "id": 7,
        "campaign_id": 3,
        "severity": severity,
        "metric": "roas_drop",
        "message": "ROAS 4.20 a cél 6.00 alatt (-40% kritikus küszöb: 3.60)",
        "observed_value": 4.2,
        "threshold_value": 3.6,
        "detected_at": "2026-08-31T12:32:00+00:00",
        **extra,
    }


_MAPPING = {
    "user_id": 1,
    "clickup_folder_id": "FD1",
    "clickup_list_id": "LS1",
    "clickup_assignee_id": "555",
}


def _patch_router(
    stack,
    *,
    recipients,
    mapping=None,
    task_result=None,
    send_result=None,
    append_result=True,
):
    """A router külső függéseit mockolja; visszaadja a hívás-naplót + mockokat.

    A napló (`calls`) a KÉTIRÁNYÚ LINKELÉS sorrendjét rögzíti: ebbe kerül a
    ClickUp task létrehozás, a Discord küldés és a task frissítése, abban a
    sorrendben, ahogy a router meghívta őket.
    """
    calls: list[str] = []

    stack.enter_context(mock.patch.object(
        router.campaigns_storage, "get_campaign",
        return_value={"id": 3, "name": "ROAS kampány", "ad_account_id": 5},
    ))
    stack.enter_context(mock.patch.object(router.mutes_storage, "is_muted", return_value=False))
    stack.enter_context(mock.patch.object(
        router.ad_accounts_storage, "get_ad_account",
        return_value={"id": 5, "platform": "meta", "client_id": 10},
    ))
    stack.enter_context(mock.patch.object(
        router.clients_storage, "get_client", return_value={"id": 10, "name": "magicherb"},
    ))
    stack.enter_context(mock.patch.object(router, "_resolve_recipients", return_value=recipients))
    stack.enter_context(mock.patch.object(
        router, "get_config", return_value=SimpleNamespace(
            discord_admin_channel_id="admin999", discord_guild_id="guild1",
        ),
    ))
    stack.enter_context(mock.patch.object(router.quiet_hours, "is_quiet_now", return_value=False))
    stack.enter_context(mock.patch.object(router.alerts_storage, "mark_alert_routed"))
    stack.enter_context(mock.patch.object(router.alerts_storage, "mark_alert_suppressed"))
    stack.enter_context(mock.patch.object(
        router.clickup_storage, "get_mapping_for_user", return_value=mapping,
    ))

    async def _create(*_a, **_k):
        calls.append("clickup_create")
        if isinstance(task_result, Exception):
            raise task_result
        return task_result

    async def _send(*_a, **_k):
        calls.append("discord_send")
        if isinstance(send_result, Exception):
            raise send_result
        return send_result

    async def _append(*_a, **_k):
        calls.append("clickup_append")
        return append_result

    create = mock.AsyncMock(side_effect=_create)
    send = mock.AsyncMock(side_effect=_send)
    append = mock.AsyncMock(side_effect=_append)
    stack.enter_context(mock.patch.object(router.clickup_router, "create_clickup_task", new=create))
    stack.enter_context(mock.patch.object(router.discord_router, "send_discord_alert", new=send))
    stack.enter_context(mock.patch.object(router.clickup_router, "append_discord_link", new=append))

    return SimpleNamespace(calls=calls, create=create, send=send, append=append)


def _recipient(discord_id="david", channel="111", role="primary", user_id=1) -> dict:
    return {
        "user_id": user_id,
        "discord_user_id": discord_id,
        "display_name": f"User{discord_id}",
        "role": role,
        "alerts_channel_id": channel,
    }


_TASK = {"task_id": "TSK1", "url": "https://app.clickup.com/t/TSK1",
         "description": "Kampány: ROAS kampány"}
_SENT = {"channel_id": 111, "message_id": 222, "guild_id": 333}


# ===========================================================================
# 1) Storage — clickup_manager_mapping CRUD
# ===========================================================================

def test_mapping_crud_roundtrip_and_idempotency():
    """Mapping létrehozás → olvasás → frissítés → törlés, user_id kulcsra."""
    sb = _SB()
    with mock.patch.object(clickup_storage, "get_supabase", return_value=sb):
        clickup_storage.upsert_mapping(
            1, clickup_folder_id="FD1", clickup_list_id="LS1", clickup_assignee_id="555",
        )
        first = clickup_storage.get_mapping_for_user(1)

        # Újrafuttatás javított assignee ID-val → FRISSÍT, nem duplikál.
        clickup_storage.upsert_mapping(
            1, clickup_folder_id="FD1", clickup_list_id="LS1", clickup_assignee_id="777",
        )
        second = clickup_storage.get_mapping_for_user(1)
        rows = list(sb.store["clickup_manager_mapping"])

        deleted = clickup_storage.delete_mapping(1)
        after = clickup_storage.get_mapping_for_user(1)

    assert first["clickup_list_id"] == "LS1"
    assert first["clickup_assignee_id"] == "555"
    assert second["clickup_assignee_id"] == "777"
    assert len(rows) == 1, "OM-enként egy mapping — különben két task születne"
    assert deleted is True
    assert after is None


def test_mapping_for_unknown_user_is_none_not_error():
    """Ismeretlen (még nem mappelt) OM → None, nem kivétel."""
    with mock.patch.object(clickup_storage, "get_supabase", return_value=_SB()):
        assert clickup_storage.get_mapping_for_user(999) is None


def test_delete_mapping_returns_false_when_there_was_nothing():
    with mock.patch.object(clickup_storage, "get_supabase", return_value=_SB()):
        assert clickup_storage.delete_mapping(999) is False


def test_storage_degrades_when_the_migration_has_not_run(caplog):
    """Hiányzó 0014 tábla → warning + None/False/üres lista, SOSEM kivétel.

    Ez a riasztási út védelme: egy elmaradt migráció miatt nem maradhat el a
    Discord riasztás.
    """
    boom = _SB(raise_msg='relation "clickup_manager_mapping" does not exist')
    with mock.patch.object(clickup_storage, "get_supabase", return_value=boom), \
         _capture(caplog, _LOG_STORAGE):
        assert clickup_storage.get_mapping_for_user(1) is None
        assert clickup_storage.upsert_mapping(
            1, clickup_folder_id="F", clickup_list_id="L") is None
        assert clickup_storage.list_mappings() == []
        assert clickup_storage.delete_mapping(1) is False

    assert "0014" in caplog.text, "a warning mondja meg, hogy migráció hiányozhat"


# ===========================================================================
# 2) clickup_admin — azonosító-beolvasás és ellenőrzés (mockolt ClickUp API)
# ===========================================================================

def _api(responses: dict):
    """Fake `requests.request`, ami (METHOD, URL-részlet) → (status, json) alapján válaszol."""
    calls: list[tuple[str, str]] = []

    def _request(method, url, **_kw):
        calls.append((method, url))
        for (m, fragment), (status, payload) in responses.items():
            if m == method and fragment in url:
                return SimpleNamespace(
                    status_code=status, text=str(payload), json=lambda p=payload: p,
                )
        raise AssertionError(f"váratlan ClickUp hívás: {method} {url}")

    return _request, calls


_CFG = SimpleNamespace(clickup_api_token="pk_test", clickup_team_id="TEAM1")


def test_parse_id_accepts_raw_ids_and_pasted_links():
    """Nyers ID és beillesztett ClickUp link is elfogadott (Copy link)."""
    assert clickup_admin.parse_id("901234567890", "list") == "901234567890"
    assert clickup_admin.parse_id("  901234567890  ", "list") == "901234567890"
    assert clickup_admin.parse_id(
        "https://app.clickup.com/9012345/v/li/901234567890", "list") == "901234567890"
    # Discord a beillesztett linket <>-be teheti a link-előnézet elnyomásához.
    assert clickup_admin.parse_id(
        "<https://app.clickup.com/9012345/v/l/901234567890>", "list") == "901234567890"
    assert clickup_admin.parse_id(
        "https://app.clickup.com/9012345/v/o/f/90123456", "folder") == "90123456"
    assert clickup_admin.parse_id(
        "https://app.clickup.com/9012345/v/f/90123456", "folder") == "90123456"


def test_parse_id_refuses_to_guess():
    """Ismeretlen alakú linkből NEM tippelünk azonosítót.

    Egy Folder-nézet URL-je a Space ID-jával is végződhet — az "utolsó
    számjegy-szegmens" heurisztika pont azt a néma, rossz ID-t mentené el,
    amit a validáció el akar kerülni.
    """
    assert clickup_admin.parse_id("https://app.clickup.com/9012345/v/li/", "list") is None
    assert clickup_admin.parse_id("https://app.clickup.com/9012345/valami/42", "list") is None
    assert clickup_admin.parse_id("", "list") is None
    assert clickup_admin.parse_id(None, "list") is None
    # A lista-mintát nem fogadjuk el folderként (és fordítva sem).
    assert clickup_admin.parse_id(
        "https://app.clickup.com/9012345/v/li/901234567890", "folder") is None


@pytest.mark.asyncio
async def test_get_list_returns_the_owning_folder():
    """A lista lekérdezése megmondja, MELYIK Folderben van — ez a kereszt-ellenőrzés alapja."""
    request, calls = _api({
        ("GET", "/list/LS1"): (200, {
            "id": "LS1", "name": "Anomália riasztások",
            "folder": {"id": "FD1", "name": "Dávid", "hidden": False},
            "space": {"id": "SP9", "name": "PPC Anomália Riasztások"},
        }),
    })
    with mock.patch.object(clickup_admin, "get_config", return_value=_CFG),          mock.patch.object(clickup_admin.requests, "request", new=request):
        lista = await clickup_admin.get_list("LS1")

    assert lista["name"] == "Anomália riasztások"
    assert lista["folder_id"] == "FD1"
    assert lista["folder_name"] == "Dávid"
    assert lista["folder_hidden"] is False
    assert all(m == "GET" for m, _ in calls), "az ellenőrzés csak olvas, nem hoz létre semmit"


@pytest.mark.asyncio
async def test_get_list_flags_a_folderless_list():
    """Folderless listánál a ClickUp REJTETT foldert ad vissza — ez nem valódi Folder."""
    request, _ = _api({
        ("GET", "/list/LS2"): (200, {
            "id": "LS2", "name": "Magányos lista",
            "folder": {"id": "HIDDEN1", "name": "hidden", "hidden": True},
        }),
    })
    with mock.patch.object(clickup_admin, "get_config", return_value=_CFG),          mock.patch.object(clickup_admin.requests, "request", new=request):
        lista = await clickup_admin.get_list("LS2")

    assert lista["folder_hidden"] is True
    assert lista["folder_id"] is None


@pytest.mark.asyncio
async def test_get_list_raises_a_human_message_when_missing():
    request, _ = _api({("GET", "/list/NINCS"): (404, {})})
    with mock.patch.object(clickup_admin, "get_config", return_value=_CFG),          mock.patch.object(clickup_admin.requests, "request", new=request):
        with pytest.raises(clickup_admin.ClickUpAdminError) as exc:
            await clickup_admin.get_list("NINCS")

    assert "404" in str(exc.value)
    assert "NINCS" in str(exc.value)


@pytest.mark.asyncio
async def test_get_folder_reads_the_name():
    request, _ = _api({("GET", "/folder/FD1"): (200, {"id": "FD1", "name": "Dávid"})})
    with mock.patch.object(clickup_admin, "get_config", return_value=_CFG),          mock.patch.object(clickup_admin.requests, "request", new=request):
        assert await clickup_admin.get_folder("FD1") == {"id": "FD1", "name": "Dávid"}


@pytest.mark.asyncio
async def test_the_admin_module_creates_nothing():
    """A struktúrát az ügyfél hozza létre kézzel — a bot NEM tud Space-t/Foldert csinálni."""
    for eltavolitott in ("ensure_space", "ensure_folder", "ensure_list"):
        assert not hasattr(clickup_admin, eltavolitott), (
            f"{eltavolitott} visszakerült — a struktúra-létrehozás kézi maradjon"
        )


@pytest.mark.asyncio
async def test_list_members_returns_ids_for_the_configured_workspace():
    """A `/clickup list-members` a ClickUp user ID-kat a GET /team-ből olvassa."""
    request, _ = _api({
        ("GET", "/api/v2/team"): (200, {"teams": [
            {"id": "TEAM0", "members": [{"user": {"id": 1, "username": "idegen"}}]},
            {"id": "TEAM1", "members": [
                {"user": {"id": 555, "username": "david", "email": "d@x.hu"}},
                {"user": {"id": 556, "username": "Adam"}},
                {"user": {}},  # hiányos sor — kihagyandó, nem hibázhat
            ]},
        ]}),
    })
    with mock.patch.object(clickup_admin, "get_config", return_value=_CFG), \
         mock.patch.object(clickup_admin.requests, "request", new=request):
        members = await clickup_admin.list_members()

    assert [m["id"] for m in members] == ["556", "555"]  # név szerint rendezve
    assert members[1]["email"] == "d@x.hu"


@pytest.mark.asyncio
async def test_admin_api_errors_are_raised_with_a_human_message():
    """Admin úton a hiba HANGOS: emberi üzenettel dob, nem néma None-nal tér vissza."""
    request, _ = _api({("GET", "/list/LS1"): (401, {})})
    with mock.patch.object(clickup_admin, "get_config", return_value=_CFG), \
         mock.patch.object(clickup_admin.requests, "request", new=request):
        with pytest.raises(clickup_admin.ClickUpAdminError) as exc:
            await clickup_admin.get_list("LS1")

    assert "CLICKUP_API_TOKEN" in str(exc.value)


# ===========================================================================
# 3) A parancsok — /clickup setup-manager, list-members, status
# ===========================================================================

def _member(user_id=99, name="Dávid"):
    """Discord member-utánzat a parancshíváshoz."""
    m = mock.Mock(spec=[], id=user_id)
    m.display_name = name
    m.name = name.lower()
    return m


_LIST_OK = {
    "id": "901234567890", "name": "Anomália riasztások",
    "folder_id": "90123456", "folder_name": "Dávid", "folder_hidden": False,
    "space_id": "SP9", "space_name": "PPC Anomália Riasztások",
}


def _patch_setup_manager(stack, *, list_result=_LIST_OK, mentve=None):
    """A setup-manager külső függéseit mockolja; a mentést a `mentve` dictbe gyűjti."""
    stack.enter_context(mock.patch.object(clickup_cmd, "_is_admin_channel", return_value=True))
    stack.enter_context(mock.patch.object(
        clickup_cmd.clickup_admin, "config_error", return_value=None))
    stack.enter_context(mock.patch.object(
        clickup_cmd.users_storage, "get_or_create_user",
        return_value=({"id": 1, "display_name": "Dávid"}, False)))
    stack.enter_context(mock.patch.object(clickup_cmd.audit, "log_action"))

    get_list = mock.AsyncMock(
        side_effect=list_result if isinstance(list_result, Exception) else None,
        return_value=None if isinstance(list_result, Exception) else list_result,
    )
    stack.enter_context(mock.patch.object(clickup_cmd.clickup_admin, "get_list", new=get_list))

    # `mentve if mentve is not None else {}` — NEM `mentve or {}`: a hívó üres
    # dictje falsy, és az `or` egy ÚJ dictbe írna, amit senki nem lát.
    sink = mentve if mentve is not None else {}
    upsert = mock.Mock(
        side_effect=lambda uid, **kw: sink.update({"user_id": uid, **kw}) or {"id": 1},
    )
    stack.enter_context(mock.patch.object(clickup_cmd.clickup_storage, "upsert_mapping", new=upsert))
    return SimpleNamespace(get_list=get_list, upsert=upsert)


@pytest.mark.asyncio
async def test_setup_manager_saves_the_mapping_after_validating_the_list():
    """Érvényes ID-k → a lista ellenőrzése UTÁN mentés."""
    interaction = _Interaction()
    mentve: dict = {}
    with contextlib.ExitStack() as stack:
        m = _patch_setup_manager(stack, mentve=mentve)
        await clickup_cmd.ClickUpCog.setup_manager.callback(
            clickup_cmd.ClickUpCog(mock.Mock()), interaction,
            user=_member(), clickup_user_id="555", folder_id="90123456", list_id="901234567890",
        )

    m.get_list.assert_awaited_once_with("901234567890")
    assert mentve == {
        "user_id": 1, "clickup_folder_id": "90123456",
        "clickup_list_id": "901234567890", "clickup_assignee_id": "555",
    }
    valasz = "\n".join(interaction.sent())
    assert "Anomália riasztások" in valasz and "555" in valasz


@pytest.mark.asyncio
async def test_setup_manager_accepts_pasted_clickup_links():
    """A ClickUp "Copy link" URL-je is elfogadott — nem kell ID-t vadászni."""
    interaction = _Interaction()
    mentve: dict = {}
    with contextlib.ExitStack() as stack:
        _patch_setup_manager(
            stack, mentve=mentve,
            list_result={**_LIST_OK, "id": "901234567890", "folder_id": "90123456"},
        )
        await clickup_cmd.ClickUpCog.setup_manager.callback(
            clickup_cmd.ClickUpCog(mock.Mock()), interaction,
            user=_member(), clickup_user_id="555",
            folder_id="https://app.clickup.com/9012345/v/o/f/90123456",
            list_id="https://app.clickup.com/9012345/v/li/901234567890",
        )

    assert mentve["clickup_folder_id"] == "90123456"
    assert mentve["clickup_list_id"] == "901234567890"


@pytest.mark.asyncio
async def test_setup_manager_does_not_save_an_unreachable_list():
    """Nem létező / nem elérhető lista → egyértelmű hiba, SEMMI nem mentődik.

    Egy 404-es lista némán nyelné el az összes későbbi riasztás-taskot.
    """
    interaction = _Interaction()
    with contextlib.ExitStack() as stack:
        m = _patch_setup_manager(
            stack,
            list_result=ClickUpAdminError(
                "A(z) `909999999999` lista lekérése: nem található (404)."),
        )
        await clickup_cmd.ClickUpCog.setup_manager.callback(
            clickup_cmd.ClickUpCog(mock.Mock()), interaction,
            user=_member(), clickup_user_id="555",
            folder_id="90123456", list_id="909999999999",
        )

    m.upsert.assert_not_called()
    valasz = "\n".join(interaction.sent())
    assert "404" in valasz
    assert "NEM lett elmentve" in valasz


@pytest.mark.asyncio
async def test_setup_manager_does_not_save_a_list_from_another_folder():
    """A lista létezik, de MÁS Folderben van → nem mentünk (kimásolási hiba)."""
    interaction = _Interaction()
    with contextlib.ExitStack() as stack:
        m = _patch_setup_manager(
            stack,
            list_result={**_LIST_OK, "folder_id": "90999999", "folder_name": "Ádám"},
        )
        await clickup_cmd.ClickUpCog.setup_manager.callback(
            clickup_cmd.ClickUpCog(mock.Mock()), interaction,
            user=_member(), clickup_user_id="555", folder_id="90123456", list_id="901234567890",
        )

    m.upsert.assert_not_called()
    valasz = "\n".join(interaction.sent())
    assert "Ádám" in valasz and "90999999" in valasz


@pytest.mark.asyncio
async def test_setup_manager_does_not_save_a_folderless_list():
    interaction = _Interaction()
    with contextlib.ExitStack() as stack:
        m = _patch_setup_manager(
            stack,
            list_result={**_LIST_OK, "folder_id": None, "folder_name": None,
                         "folder_hidden": True},
        )
        await clickup_cmd.ClickUpCog.setup_manager.callback(
            clickup_cmd.ClickUpCog(mock.Mock()), interaction,
            user=_member(), clickup_user_id="555", folder_id="90123456", list_id="901234567890",
        )

    m.upsert.assert_not_called()
    assert "folderless" in "\n".join(interaction.sent())


@pytest.mark.asyncio
async def test_setup_manager_rejects_a_non_numeric_clickup_user_id():
    """Elgépelt assignee ID → azonnali, érthető hiba (nem néma, assignee nélküli taskok)."""
    interaction = _Interaction()
    with contextlib.ExitStack() as stack:
        m = _patch_setup_manager(stack)
        await clickup_cmd.ClickUpCog.setup_manager.callback(
            clickup_cmd.ClickUpCog(mock.Mock()), interaction,
            user=_member(), clickup_user_id="@david", folder_id="90123456", list_id="901234567890",
        )

    m.get_list.assert_not_awaited()
    m.upsert.assert_not_called()
    assert "list-members" in "\n".join(interaction.sent())


@pytest.mark.asyncio
async def test_setup_manager_rejects_an_unparseable_id():
    """Felismerhetetlen link → a parancs a nyers ID-t kéri, nem tippel."""
    for folder_id, list_id, kell in (
        ("valami-hulyeseg", "901234567890", "Folder ID"),
        ("90123456", "valami-hulyeseg", "List ID"),
    ):
        interaction = _Interaction()
        with contextlib.ExitStack() as stack:
            m = _patch_setup_manager(stack)
            await clickup_cmd.ClickUpCog.setup_manager.callback(
                clickup_cmd.ClickUpCog(mock.Mock()), interaction,
                user=_member(), clickup_user_id="555",
                folder_id=folder_id, list_id=list_id,
            )

        m.upsert.assert_not_called()
        assert kell in "\n".join(interaction.sent())


@pytest.mark.asyncio
async def test_setup_manager_reports_a_failed_database_write():
    """A ClickUp ID-k jók, de a DB írás elhasal → a válasz megmondja, mi a teendő."""
    interaction = _Interaction()
    with contextlib.ExitStack() as stack:
        _patch_setup_manager(stack)
        stack.enter_context(mock.patch.object(
            clickup_cmd.clickup_storage, "upsert_mapping", return_value=None))
        await clickup_cmd.ClickUpCog.setup_manager.callback(
            clickup_cmd.ClickUpCog(mock.Mock()), interaction,
            user=_member(), clickup_user_id="555", folder_id="90123456", list_id="901234567890",
        )

    assert "0014" in "\n".join(interaction.sent())


def test_there_is_no_setup_command_anymore():
    """A struktúrát kézzel hozzák létre — nem hagytunk bent no-op `setup` parancsot."""
    parancsok = {c.name for c in clickup_cmd.ClickUpCog.__cog_app_commands__}
    assert parancsok == {"setup-manager", "list-members", "status"}


@pytest.mark.asyncio
async def test_commands_are_admin_channel_only():
    cog = clickup_cmd.ClickUpCog(mock.Mock())
    for hivas in (
        lambda i: clickup_cmd.ClickUpCog.list_members.callback(cog, i),
        lambda i: clickup_cmd.ClickUpCog.status.callback(cog, i),
        lambda i: clickup_cmd.ClickUpCog.setup_manager.callback(
            cog, i, user=_member(), clickup_user_id="555", folder_id="90123456", list_id="901234567890"),
    ):
        interaction = _Interaction()
        with mock.patch.object(clickup_cmd, "_is_admin_channel", return_value=False):
            await hivas(interaction)
        assert "admin csatornában" in "\n".join(interaction.sent())


# --- /clickup status -------------------------------------------------------

def _mapping_row(user_id=1, name="Dávid", list_id="901234567890", folder_id="90123456", assignee="555"):
    return {
        "user_id": user_id, "clickup_folder_id": folder_id, "clickup_list_id": list_id,
        "clickup_assignee_id": assignee,
        "users": {"id": user_id, "display_name": name, "discord_user_id": "d1"},
    }


def _patch_status(stack, *, mappings, get_list=None, config_problem=None):
    stack.enter_context(mock.patch.object(clickup_cmd, "_is_admin_channel", return_value=True))
    stack.enter_context(mock.patch.object(
        clickup_cmd.clickup_admin, "config_error", return_value=config_problem))
    stack.enter_context(mock.patch.object(
        clickup_cmd.clickup_storage, "list_mappings", return_value=mappings))
    stack.enter_context(mock.patch.object(
        clickup_cmd.clickup_admin, "get_list",
        new=get_list if get_list is not None else mock.AsyncMock(return_value=_LIST_OK),
    ))


@pytest.mark.asyncio
async def test_status_shows_the_manual_setup_steps_when_there_is_no_mapping():
    """Üres állapotban a status MEGMONDJA, mit kell kézzel létrehozni a ClickUp-ban."""
    interaction = _Interaction()
    with contextlib.ExitStack() as stack:
        _patch_status(stack, mappings=[])
        await clickup_cmd.ClickUpCog.status.callback(
            clickup_cmd.ClickUpCog(mock.Mock()), interaction)

    valasz = "\n".join(interaction.sent())
    assert clickup_admin.SPACE_NAME in valasz
    assert "setup-manager" in valasz


@pytest.mark.asyncio
async def test_status_marks_a_live_mapping_as_ok():
    interaction = _Interaction()
    with contextlib.ExitStack() as stack:
        _patch_status(stack, mappings=[_mapping_row()])
        await clickup_cmd.ClickUpCog.status.callback(
            clickup_cmd.ClickUpCog(mock.Mock()), interaction)

    assert "✅ **Dávid**" in "\n".join(interaction.sent())


@pytest.mark.asyncio
async def test_status_flags_a_deleted_list():
    """A ClickUp-ban időközben törölt lista ❌ jelet kap — nem néma "minden rendben"."""
    interaction = _Interaction()
    with contextlib.ExitStack() as stack:
        _patch_status(
            stack, mappings=[_mapping_row()],
            get_list=mock.AsyncMock(side_effect=ClickUpAdminError("nem található (404)")),
        )
        await clickup_cmd.ClickUpCog.status.callback(
            clickup_cmd.ClickUpCog(mock.Mock()), interaction)

    assert "❌ **Dávid**" in "\n".join(interaction.sent())


@pytest.mark.asyncio
async def test_status_flags_a_list_that_moved_to_another_folder():
    interaction = _Interaction()
    with contextlib.ExitStack() as stack:
        _patch_status(
            stack, mappings=[_mapping_row()],
            get_list=mock.AsyncMock(return_value={**_LIST_OK, "folder_id": "90999999"}),
        )
        await clickup_cmd.ClickUpCog.status.callback(
            clickup_cmd.ClickUpCog(mock.Mock()), interaction)

    assert "⚠️ **Dávid**" in "\n".join(interaction.sent())


@pytest.mark.asyncio
async def test_status_treats_a_renamed_list_as_healthy():
    """Az átNEVEZÉS nem hiba: a taskokat ID alapján hozzuk létre."""
    interaction = _Interaction()
    with contextlib.ExitStack() as stack:
        _patch_status(
            stack, mappings=[_mapping_row()],
            get_list=mock.AsyncMock(return_value={**_LIST_OK, "name": "Új név"}),
        )
        await clickup_cmd.ClickUpCog.status.callback(
            clickup_cmd.ClickUpCog(mock.Mock()), interaction)

    assert "✅ **Dávid**" in "\n".join(interaction.sent())


@pytest.mark.asyncio
async def test_status_does_not_call_clickup_when_the_config_is_missing():
    """Token nélkül nincs értelme ellenőrizni — a sorok ❔ jelet kapnak."""
    interaction = _Interaction()
    get_list = mock.AsyncMock(return_value=_LIST_OK)
    with contextlib.ExitStack() as stack:
        _patch_status(stack, mappings=[_mapping_row()], get_list=get_list,
                      config_problem="hiányzik a `CLICKUP_API_TOKEN`")
        await clickup_cmd.ClickUpCog.status.callback(
            clickup_cmd.ClickUpCog(mock.Mock()), interaction)

    get_list.assert_not_awaited()
    valasz = "\n".join(interaction.sent())
    assert "CLICKUP_API_TOKEN" in valasz
    assert "❔ **Dávid**" in valasz


@pytest.mark.asyncio
async def test_status_caps_the_validation_and_says_so():
    """A ClickUp-ellenőrzés felső korlátos — és a válasz BEVALLJA, meddig futott."""
    sok = [_mapping_row(user_id=i, name=f"OM{i}", list_id=f"90123456789{i}")
           for i in range(clickup_cmd._MAX_VALIDATED + 3)]
    get_list = mock.AsyncMock(return_value=_LIST_OK)
    interaction = _Interaction()
    with contextlib.ExitStack() as stack:
        _patch_status(stack, mappings=sok, get_list=get_list)
        await clickup_cmd.ClickUpCog.status.callback(
            clickup_cmd.ClickUpCog(mock.Mock()), interaction)

    assert get_list.await_count == clickup_cmd._MAX_VALIDATED
    valasz = "\n".join(interaction.sent())
    assert str(clickup_cmd._MAX_VALIDATED) in valasz
    assert "❔" in valasz


# ===========================================================================
# 4) A task szövege — UGYANAZ a formázó, mint a Discord üzeneté
# ===========================================================================

def test_task_title_and_discord_header_use_the_same_client_and_platform():
    """A cím és a Discord fejléc ugyanabból a közös formázóból építkezik."""
    fejlec = alert_content.campaign_label("magicherb", "meta", "ROAS kampány")
    cim = alert_content.clickup_task_title(
        client_name="magicherb", platform="meta",
        message="ROAS 4.20 a cél 6.00 alatt", observed_value=4.2,
    )

    assert fejlec.startswith("magicherb [META]")
    assert cim.startswith("magicherb - meta - ")


def test_task_title_carries_the_alert_message_verbatim():
    """A cím a riasztás SAJÁT üzenetét viszi — semmit nem fogalmaz újra."""
    uzenet = "ROAS 4.20 a cél 6.00 alatt (-40% kritikus küszöb: 3.60)"
    cim = alert_content.clickup_task_title(
        client_name="magicherb", platform="meta", message=uzenet, observed_value=4.2,
    )
    assert uzenet in cim


def test_task_title_appends_the_value_only_when_the_message_omits_it():
    """`({aktuális érték})` — de nem ismétli meg, ha az üzenet már kimondja.

    A "0" nem illeszkedhet a "12000"-be sem: az érték csak ÖNÁLLÓ számként számít
    említettnek.
    """
    benne = alert_content.clickup_task_title(
        client_name="c", platform="meta",
        message="ROAS 4.20 a cél alatt", observed_value=4.2,
    )
    kimaradt = alert_content.clickup_task_title(
        client_name="c", platform="meta",
        message="Büdzsé elfogyott", observed_value=4.2,
    )
    resz_szam = alert_content.clickup_task_title(
        client_name="c", platform="meta",
        message="12000 Ft költés", observed_value=0,
    )

    assert benne.endswith("a cél alatt")
    assert kimaradt.endswith("(4.20)")
    assert resz_szam.endswith("(0)")


def test_task_title_is_truncated_not_dropped():
    hosszu = "x" * 500
    cim = alert_content.clickup_task_title(
        client_name="c", platform="meta", message=hosszu, observed_value=None,
    )
    assert len(cim) <= 255
    assert cim.endswith("…")


def test_detector_and_clickup_format_numbers_identically():
    """A detektor `_fmt`-je és a task címe UGYANAZ a formázó — nem két másolat."""
    from src.monitoring import detector

    for value in (4.2, 12000.0, 0, None, 3.5):
        assert detector._fmt(value) == alert_content.format_number(value)


def test_task_description_uses_the_shared_timestamp_formatting():
    """Az észlelés ideje a KONFIGURÁLT időzónában jelenik meg (nem nyers UTC)."""
    szoveg = alert_content.clickup_task_description(
        campaign_name="ROAS kampány", platform="meta", message="baj",
        observed_value=4.2, threshold_value=3.6,
        detected_at_text=alert_content.detected_at_label(_alert(), "Europe/Budapest"),
        alert_id=7, campaign_id=3,
    )
    # 12:32 UTC → 14:32 Budapesten (nyári időszámítás).
    assert "2026-08-31 14:32" in szoveg
    assert "ROAS kampány" in szoveg
    assert "/campaign info campaign_id:3" in szoveg


@pytest.mark.asyncio
async def test_task_body_uses_the_mapping_list_and_assignee():
    """A task az OM listájába, rá szignálva, Urgent prioritással jön létre."""
    kuldott: dict = {}

    def _post(url, headers=None, json=None, timeout=None, **_kw):  # noqa: A002
        kuldott["url"] = url
        kuldott["body"] = json
        return SimpleNamespace(status_code=200, text="{}",
                               json=lambda: {"id": "TSK1", "url": "https://app.clickup.com/t/TSK1"})

    cfg = SimpleNamespace(clickup_api_token="pk_test", timezone="Europe/Budapest")
    with mock.patch.object(clickup_router, "get_config", return_value=cfg), \
         mock.patch.object(clickup_router.requests, "request",
                           new=lambda method, url, **kw: _post(url, **kw)):
        res = await clickup_router.create_clickup_task(
            _alert(), {"id": 3, "name": "ROAS kampány"}, {"name": "magicherb"},
            platform="meta", mapping=_MAPPING,
        )

    assert res["task_id"] == "TSK1"
    assert "/list/LS1/task" in kuldott["url"]
    assert kuldott["body"]["assignees"] == [555]
    assert kuldott["body"]["priority"] == 1, "CRITICAL → Urgent"
    assert "status" not in kuldott["body"], "a lista alapértelmezett státusza marad"
    assert kuldott["body"]["name"].startswith("magicherb - meta - ")


@pytest.mark.asyncio
async def test_a_broken_assignee_id_still_creates_the_task(caplog):
    """Elgépelt assignee ID → task assignee NÉLKÜL, warninggal — nem kimaradó task."""
    kuldott: dict = {}

    def _request(method, url, **kw):
        kuldott["body"] = kw.get("json")
        return SimpleNamespace(status_code=200, text="{}", json=lambda: {"id": "TSK1"})

    cfg = SimpleNamespace(clickup_api_token="pk_test", timezone="Europe/Budapest")
    with mock.patch.object(clickup_router, "get_config", return_value=cfg), \
         mock.patch.object(clickup_router.requests, "request", new=_request), \
         _capture(caplog, _LOG_TASK):
        res = await clickup_router.create_clickup_task(
            _alert(), {"id": 3}, None, platform="meta",
            mapping={**_MAPPING, "clickup_assignee_id": "@david"},
        )

    assert res is not None
    assert "assignees" not in kuldott["body"]
    assert "nem szám" in caplog.text


@pytest.mark.asyncio
async def test_default_severity_gate_is_critical_only():
    """Alapértelmezés: CSAK CRITICAL-ra készül task (a WARNING egy konstans odébb)."""
    assert clickup_router.TASK_SEVERITIES == frozenset({"critical"})
    assert clickup_router.is_task_severity("critical") is True
    assert clickup_router.is_task_severity("CRITICAL") is True
    assert clickup_router.is_task_severity("warning") is False
    assert clickup_router.is_task_severity("insight") is False
    # A bővítéshez a prioritás-leképezés már készen áll: WARNING → High (2).
    assert clickup_router._PRIORITY_BY_SEVERITY["warning"] == 2


@pytest.mark.asyncio
async def test_warning_alert_creates_no_task():
    with contextlib.ExitStack() as stack:
        m = _patch_router(stack, recipients=[_recipient()], mapping=_MAPPING, task_result=_TASK,
                          send_result=_SENT)
        await router.route_alert(_alert("warning"))

    m.create.assert_not_awaited()
    assert m.calls == ["discord_send"]


# ===========================================================================
# 5) Kétirányú linkelés — a sorrend a funkció lényege
# ===========================================================================

@pytest.mark.asyncio
async def test_link_order_is_task_then_discord_then_task_update():
    """task létrejön → Discord megy → task frissül. Ez a sorrend nem cserélhető."""
    with contextlib.ExitStack() as stack:
        m = _patch_router(stack, recipients=[_recipient()], mapping=_MAPPING,
                          task_result=_TASK, send_result=_SENT)
        result = await router.route_alert(_alert("critical"))

    assert m.calls == ["clickup_create", "discord_send", "clickup_append"]
    assert result["channels"] == ["discord", "clickup"]


@pytest.mark.asyncio
async def test_the_discord_message_carries_the_task_url():
    """A task linkje MÁR AZ ELSŐ üzenetben benne van (nem utólagos szerkesztés)."""
    with contextlib.ExitStack() as stack:
        m = _patch_router(stack, recipients=[_recipient()], mapping=_MAPPING,
                          task_result=_TASK, send_result=_SENT)
        await router.route_alert(_alert("critical"))

    assert m.send.await_args.kwargs["clickup_task_url"] == "https://app.clickup.com/t/TSK1"


@pytest.mark.asyncio
async def test_the_task_is_updated_with_the_discord_jump_link():
    """A visszaírt link a KÜLDÉS válaszából épül: guild/csatorna/üzenet."""
    with contextlib.ExitStack() as stack:
        m = _patch_router(stack, recipients=[_recipient()], mapping=_MAPPING,
                          task_result=_TASK, send_result=_SENT)
        await router.route_alert(_alert("critical"))

    task_id, description, url = m.append.await_args.args
    assert task_id == "TSK1"
    assert description == _TASK["description"]
    assert url == "https://discord.com/channels/333/111/222"


@pytest.mark.asyncio
async def test_the_jump_link_falls_back_to_the_configured_guild():
    """Ha a küldés válaszában nincs guild (pl. DM), a konfigurált guild ugrik be."""
    with contextlib.ExitStack() as stack:
        m = _patch_router(
            stack, recipients=[_recipient()], mapping=_MAPPING, task_result=_TASK,
            send_result={"channel_id": 111, "message_id": 222, "guild_id": None},
        )
        await router.route_alert(_alert("critical"))

    assert m.append.await_args.args[2] == "https://discord.com/channels/guild1/111/222"


def test_the_discord_message_renders_the_clickup_line():
    """A "📋 ClickUp: …" sor a közös formázóból jön, egy helyről."""
    assert alert_content.clickup_link_line("https://app.clickup.com/t/TSK1") == (
        "📋 ClickUp: https://app.clickup.com/t/TSK1"
    )
    assert alert_content.discord_link_line("https://discord.com/channels/1/2/3") == (
        "🔗 Discord: https://discord.com/channels/1/2/3"
    )


@pytest.mark.asyncio
async def test_send_discord_alert_includes_the_task_url_in_the_message():
    """Végponti ellenőrzés: a kiküldött SZÖVEGBEN tényleg ott a task linkje."""
    kuldott: list[str] = []

    class _Channel:
        id = 111
        guild = SimpleNamespace(id=333)

        async def send(self, content, allowed_mentions=None):  # noqa: ANN001
            kuldott.append(content)
            return SimpleNamespace(id=222)

    with mock.patch.object(discord_router, "_resolve_channel",
                           new=mock.AsyncMock(return_value=_Channel())):
        res = await discord_router.send_discord_alert(
            "111", _alert("critical"), campaign_label="magicherb [META] / ROAS kampány",
            clickup_task_url="https://app.clickup.com/t/TSK1",
        )

    assert "📋 ClickUp: https://app.clickup.com/t/TSK1" in kuldott[0]
    assert res == {"channel_id": 111, "message_id": 222, "guild_id": 333}


@pytest.mark.asyncio
async def test_append_discord_link_sends_the_full_description_via_put():
    """A ClickUp a description-t LECSERÉLI — a teljes (régi + link) szöveget küldjük.

    A v2-ben a task módosítása PUT (nincs PATCH végpont); a body ettől még
    részleges: csak a `description` mezőt írja felül.
    """
    hivas: dict = {}

    def _request(method, url, **kw):
        hivas["method"] = method
        hivas["url"] = url
        hivas["body"] = kw.get("json")
        return SimpleNamespace(status_code=200, text="{}", json=lambda: {"id": "TSK1"})

    cfg = SimpleNamespace(clickup_api_token="pk_test", timezone="Europe/Budapest")
    with mock.patch.object(clickup_router, "get_config", return_value=cfg), \
         mock.patch.object(clickup_router.requests, "request", new=_request):
        ok = await clickup_router.append_discord_link(
            "TSK1", "Kampány: ROAS kampány", "https://discord.com/channels/1/2/3",
        )

    assert ok is True
    assert hivas["method"] == "PUT"
    assert hivas["url"].endswith("/task/TSK1")
    assert hivas["body"]["description"] == (
        "Kampány: ROAS kampány\n\n🔗 Discord: https://discord.com/channels/1/2/3"
    )
    assert list(hivas["body"]) == ["description"], "csak a leírást írjuk felül"


# ===========================================================================
# 6) Hiányzó mapping — graceful skip WARNING loggal
# ===========================================================================

@pytest.mark.asyncio
async def test_missing_mapping_skips_the_task_but_sends_discord(caplog):
    """Nincs mapping → NINCS task, DE a Discord riasztás kimegy, warninggal."""
    with contextlib.ExitStack() as stack:
        m = _patch_router(stack, recipients=[_recipient()], mapping=None,
                          task_result=_TASK, send_result=_SENT)
        with _capture(caplog, _LOG_ROUTER, _LOG_TASK):
            result = await router.route_alert(_alert("critical"))

    m.create.assert_not_awaited()
    m.send.assert_awaited_once()
    assert result["routed"] is True
    assert result["channels"] == ["discord"]
    assert m.send.await_args.kwargs["clickup_task_url"] is None
    assert "setup-manager" in caplog.text, "a warning megmondja, hogyan javítható"


@pytest.mark.asyncio
async def test_a_recipient_without_a_user_id_skips_the_task(caplog):
    with contextlib.ExitStack() as stack:
        m = _patch_router(stack, recipients=[_recipient(user_id=None)], mapping=_MAPPING,
                          task_result=_TASK, send_result=_SENT)
        with _capture(caplog, _LOG_ROUTER, _LOG_TASK):
            result = await router.route_alert(_alert("critical"))

    m.create.assert_not_awaited()
    assert result["channels"] == ["discord"]


@pytest.mark.asyncio
async def test_without_an_assignee_there_is_no_task_but_the_admin_fallback_stands(caplog):
    """Hozzárendelés nélküli kampány: nincs cél-lista → nincs task, de az
    admin fallback riasztás változatlanul kimegy."""
    with contextlib.ExitStack() as stack:
        m = _patch_router(stack, recipients=[], mapping=_MAPPING,
                          task_result=_TASK, send_result=_SENT)
        with _capture(caplog, _LOG_ROUTER, _LOG_TASK):
            result = await router.route_alert(_alert("critical"))

    m.create.assert_not_awaited()
    m.send.assert_awaited_once()
    assert m.send.await_args.args[0] == "admin999"
    assert result["routed"] is True


@pytest.mark.asyncio
async def test_the_task_goes_to_the_primary_assignees_list():
    """Több címzettnél a PRIMARY OM mappingja dönt — nem a lista első eleme."""
    kert: list = []

    with contextlib.ExitStack() as stack:
        m = _patch_router(
            stack,
            recipients=[
                _recipient("mate", "222", role="supporter", user_id=2),
                _recipient("david", "111", role="primary", user_id=1),
            ],
            mapping=_MAPPING, task_result=_TASK, send_result=_SENT,
        )
        stack.enter_context(mock.patch.object(
            router.clickup_storage, "get_mapping_for_user",
            side_effect=lambda uid: kert.append(uid) or _MAPPING,
        ))
        await router.route_alert(_alert("critical"))

    assert kert == [1], "a primary OM user_id-jával kerestük a mappingot"
    assert m.create.await_count == 1, "több címzettnél is EGY task készül"


# ===========================================================================
# 7) Hiba-izoláció mindkét irányban
# ===========================================================================

@pytest.mark.asyncio
async def test_a_failing_clickup_task_does_not_block_the_discord_alert():
    """A ClickUp task None-t ad (API hiba) → a riasztás task-link nélkül megy ki."""
    with contextlib.ExitStack() as stack:
        m = _patch_router(stack, recipients=[_recipient()], mapping=_MAPPING,
                          task_result=None, send_result=_SENT)
        result = await router.route_alert(_alert("critical"))

    m.send.assert_awaited_once()
    assert m.send.await_args.kwargs["clickup_task_url"] is None
    assert result["routed"] is True
    assert "clickup" not in result["channels"]
    m.append.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_exploding_clickup_task_does_not_block_the_discord_alert(caplog):
    """Váratlan KIVÉTEL a ClickUp-ágon sem viheti magával a riasztást."""
    with contextlib.ExitStack() as stack:
        m = _patch_router(stack, recipients=[_recipient()], mapping=_MAPPING,
                          task_result=RuntimeError("ClickUp bumm"), send_result=_SENT)
        with _capture(caplog, _LOG_ROUTER, level=logging.ERROR):
            result = await router.route_alert(_alert("critical"))

    m.send.assert_awaited_once()
    assert result["routed"] is True
    assert "ClickUp bumm" in caplog.text


@pytest.mark.asyncio
async def test_a_failing_discord_send_leaves_the_task_without_a_link(caplog):
    """Discord-hiba → a task MEGMARAD, csak Discord-link nélkül (warning)."""
    with contextlib.ExitStack() as stack:
        m = _patch_router(stack, recipients=[_recipient()], mapping=_MAPPING,
                          task_result=_TASK, send_result=None)
        with _capture(caplog, _LOG_ROUTER, _LOG_TASK):
            result = await router.route_alert(_alert("critical"))

    m.create.assert_awaited_once()
    m.append.assert_not_awaited()
    assert "clickup" in result["channels"]
    assert "Discord-link nélkül marad" in caplog.text


@pytest.mark.asyncio
async def test_a_failing_link_writeback_does_not_break_routing(caplog):
    """A visszaírás kivétele sem dönti el a routingot — a riasztás már kiment."""
    with contextlib.ExitStack() as stack:
        m = _patch_router(stack, recipients=[_recipient()], mapping=_MAPPING,
                          task_result=_TASK, send_result=_SENT)
        stack.enter_context(mock.patch.object(
            router.clickup_router, "append_discord_link",
            new=mock.AsyncMock(side_effect=RuntimeError("PUT bumm")),
        ))
        with _capture(caplog, _LOG_ROUTER, _LOG_TASK):
            result = await router.route_alert(_alert("critical"))

    assert result["routed"] is True
    assert "clickup" in result["channels"]
    assert "PUT bumm" in caplog.text


@pytest.mark.asyncio
async def test_clickup_api_failures_never_raise_on_the_alert_path():
    """A task-modul minden HTTP hibaágon None-t ad — sosem dob."""
    cfg = SimpleNamespace(clickup_api_token="pk_test", timezone="Europe/Budapest")
    for status in (401, 404, 429, 500):
        def _request(method, url, _s=status, **kw):
            return SimpleNamespace(status_code=_s, text="hiba", json=lambda: {})

        with mock.patch.object(clickup_router, "get_config", return_value=cfg), \
             mock.patch.object(clickup_router.requests, "request", new=_request):
            assert await clickup_router.create_clickup_task(
                _alert(), {"id": 3}, None, platform="meta", mapping=_MAPPING,
            ) is None
            assert await clickup_router.append_discord_link("T1", "d", "u") is False


@pytest.mark.asyncio
async def test_network_errors_are_swallowed_on_the_alert_path():
    def _boom(*_a, **_kw):
        raise ConnectionError("nincs háló")

    cfg = SimpleNamespace(clickup_api_token="pk_test", timezone="Europe/Budapest")
    with mock.patch.object(clickup_router, "get_config", return_value=cfg), \
         mock.patch.object(clickup_router.requests, "request", new=_boom):
        assert await clickup_router.create_clickup_task(
            _alert(), {"id": 3}, None, platform="meta", mapping=_MAPPING,
        ) is None


@pytest.mark.asyncio
async def test_a_missing_token_skips_the_task_with_a_warning(caplog):
    """Hiányzó CLICKUP_API_TOKEN → warning + None (ugyanaz a minta, mint eddig)."""
    cfg = SimpleNamespace(clickup_api_token="", timezone="Europe/Budapest")
    with mock.patch.object(clickup_router, "get_config", return_value=cfg), \
         _capture(caplog, _LOG_TASK):
        res = await clickup_router.create_clickup_task(
            _alert(), {"id": 3}, None, platform="meta", mapping=_MAPPING,
        )

    assert res is None
    assert "CLICKUP_API_TOKEN" in caplog.text


@pytest.mark.asyncio
async def test_a_mapping_without_a_list_id_skips_the_task(caplog):
    cfg = SimpleNamespace(clickup_api_token="pk_test", timezone="Europe/Budapest")
    with mock.patch.object(clickup_router, "get_config", return_value=cfg), \
         _capture(caplog, _LOG_TASK):
        res = await clickup_router.create_clickup_task(
            _alert(), {"id": 3}, None, platform="meta",
            mapping={"clickup_assignee_id": "555"},
        )

    assert res is None
    assert "setup-manager" in caplog.text
