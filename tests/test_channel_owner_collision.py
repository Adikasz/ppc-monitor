"""
Csatorna-ütközés: két user ugyanazon az `alerts_channel_id`-n.

Élesben előfordult (Adam_PlanSmart #1 és Máté #5 ugyanazon a csatornán), és két
hibát okozott:

1. A riasztások/összefoglalók MINDKÉT user nevében megérkeztek abba az egy
   csatornába — így Máté olyan ügyfelek (pl. MagicHerb) alertjeit látta,
   amikhez nincs is hozzárendelve.
2. A `users.get_user_by_alerts_channel` `.limit(1)`-gyel, RENDEZÉS NÉLKÜL
   keresett, tehát NEM DETERMINISZTIKUSAN adta vissza az egyik usert — a
   csatorna-szkópolt `/my` parancsok véletlenszerűen a rossz OM hatókörén
   dolgozhattak (idegen kampányok összefoglalója, idegen kampány némítása).

A javítás a (2)-re: ütközésnél a függvény NEM választ, hanem
`{"ambiguous": True, "matches": [...]}`-t ad vissza + WARNING-ot logol, a
parancsok pedig fail-closed módon elutasítanak. Az (1) adat-szintű kérdés,
amire a `/user set-channel` figyelmeztetése és a `/user list` jelölése hívja
fel a figyelmet.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest import mock

import pytest

from src.bot.commands import _common
from src.bot.commands import my_commands as my_cmd
from src.bot.commands import users as users_cmd
from src.storage import users as users_storage


def _user(uid: int, name: str, channel: str | None = "chan-1"):
    return {
        "id": uid,
        "display_name": name,
        "discord_user_id": f"d{uid}",
        "alerts_channel_id": channel,
        "is_active": True,
    }


# ---------------------------------------------------------------------------
# storage: a feloldás nem választ önkényesen
# ---------------------------------------------------------------------------

def test_single_owner_returns_the_user_row():
    with mock.patch.object(users_storage, "find_users_by_alerts_channel",
                           return_value=[_user(5, "Máté")]):
        assert users_storage.get_user_by_alerts_channel("chan-1")["id"] == 5


def test_no_owner_returns_none():
    with mock.patch.object(users_storage, "find_users_by_alerts_channel", return_value=[]):
        assert users_storage.get_user_by_alerts_channel("chan-1") is None


def test_collision_returns_the_ambiguous_marker_not_an_arbitrary_user():
    """A KULCS-teszt: két usernél NEM választ egyet.

    Korábban `.limit(1)` volt rendezés nélkül — a visszaadott user attól függött,
    milyen sorrendben adta vissza a PostgREST a sorokat.
    """
    matches = [_user(1, "Adam_PlanSmart"), _user(5, "Máté")]
    with mock.patch.object(users_storage, "find_users_by_alerts_channel",
                           return_value=matches):
        res = users_storage.get_user_by_alerts_channel("chan-1")

    assert res["ambiguous"] is True
    assert "id" not in res, "nem szabad user sornak látszania"
    assert [m["id"] for m in res["matches"]] == [1, 5]


def test_collision_logs_a_warning_naming_both_users(caplog):
    """A Railway logból is ki kell derülnie, KI ütközik kivel."""
    logger = logging.getLogger("src.storage.users")
    logger.addHandler(caplog.handler)
    try:
        with mock.patch.object(
            users_storage, "find_users_by_alerts_channel",
            return_value=[_user(1, "Adam_PlanSmart"), _user(5, "Máté")],
        ):
            users_storage.get_user_by_alerts_channel("chan-1")
    finally:
        logger.removeHandler(caplog.handler)

    sorok = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(sorok) == 1
    assert "Adam_PlanSmart" in sorok[0] and "Máté" in sorok[0]
    assert "chan-1" in sorok[0]


def test_lookup_is_ordered_so_the_result_is_deterministic():
    """A lekérdezés `order("id")`-vel megy — enélkül a sorrend nem garantált."""
    hivasok: list[str] = []

    class _Q:
        def select(self, *_a, **_k):
            hivasok.append("select")
            return self

        def eq(self, *_a, **_k):
            hivasok.append("eq")
            return self

        def order(self, col, **_k):
            hivasok.append(f"order:{col}")
            return self

        def execute(self):
            return SimpleNamespace(data=[])

    with mock.patch.object(users_storage, "get_supabase",
                           return_value=SimpleNamespace(table=lambda _t: _Q())):
        users_storage.find_users_by_alerts_channel("chan-1")

    assert "order:id" in hivasok
    assert not any(h.startswith("limit") for h in hivasok)


# ---------------------------------------------------------------------------
# _common segédek
# ---------------------------------------------------------------------------

def test_is_ambiguous_owner_distinguishes_the_three_cases():
    assert _common.is_ambiguous_owner({"ambiguous": True, "matches": []}) is True
    assert _common.is_ambiguous_owner(_user(5, "Máté")) is False
    assert _common.is_ambiguous_owner(None) is False


def test_ambiguous_message_names_the_users_so_the_admin_can_act():
    msg = _common.ambiguous_owner_message(
        {"ambiguous": True, "matches": [_user(1, "Adam_PlanSmart"), _user(5, "Máté")]}
    )
    assert "több felhasználóhoz is hozzá van rendelve" in msg
    assert "Adam_PlanSmart" in msg and "Máté" in msg
    assert "/user set-channel" in msg


def test_unique_channel_owner_hides_the_collision_from_autocomplete():
    """Autocomplete-ben nem lehet hibaüzenetet küldeni → néma None."""
    with mock.patch.object(_common.users_storage, "get_user_by_alerts_channel",
                           return_value={"ambiguous": True, "matches": []}):
        assert _common.unique_channel_owner("chan-1") is None

    with mock.patch.object(_common.users_storage, "get_user_by_alerts_channel",
                           return_value=_user(5, "Máté")):
        assert _common.unique_channel_owner("chan-1")["id"] == 5


# ---------------------------------------------------------------------------
# /my parancsok: fail-closed
# ---------------------------------------------------------------------------

class _Interaction:
    def __init__(self, channel_id="chan-1"):
        self.channel_id = channel_id
        self.user = mock.Mock(id=42)
        self.response = mock.AsyncMock()
        self.followup = mock.AsyncMock()
        self.channel = mock.AsyncMock()

    def sent(self) -> list[str]:
        return [c.args[0] for c in self.followup.send.call_args_list if c.args]


@pytest.mark.asyncio
async def test_my_command_refuses_to_run_on_an_ambiguous_channel():
    """Inkább semmit, mint a ROSSZ OM hatókörén dolgozni."""
    cog = my_cmd.MyCommandsCog(mock.Mock())
    interaction = _Interaction()

    with mock.patch.object(
        my_cmd, "_channel_owner",
        return_value={"ambiguous": True,
                      "matches": [_user(1, "Adam_PlanSmart"), _user(5, "Máté")]},
    ):
        owner = await cog._owner_or_reject(interaction)

    assert owner is None, "ütközésnél nem adhat vissza OM-et"
    uzenet = " ".join(interaction.sent())
    assert "több felhasználóhoz" in uzenet
    assert "Adam_PlanSmart" in uzenet and "Máté" in uzenet
    # NEM a "nincs OM rendelve" üzenet megy ki — az félrevezető lenne.
    assert "nincs OM rendelve" not in uzenet


@pytest.mark.asyncio
async def test_my_command_still_works_on_a_clean_channel():
    """A javítás nem törheti el a normál esetet."""
    cog = my_cmd.MyCommandsCog(mock.Mock())
    interaction = _Interaction()

    with mock.patch.object(my_cmd, "_channel_owner", return_value=_user(5, "Máté")):
        owner = await cog._owner_or_reject(interaction)

    assert owner["id"] == 5
    assert interaction.sent() == []


@pytest.mark.asyncio
async def test_my_command_message_for_an_unowned_channel_is_unchanged():
    cog = my_cmd.MyCommandsCog(mock.Mock())
    interaction = _Interaction()

    with mock.patch.object(my_cmd, "_channel_owner", return_value=None):
        owner = await cog._owner_or_reject(interaction)

    assert owner is None
    assert "nincs OM rendelve" in " ".join(interaction.sent())


def test_my_autocomplete_helper_returns_none_on_collision():
    with mock.patch.object(
        my_cmd, "_channel_owner",
        return_value={"ambiguous": True, "matches": [_user(1, "A"), _user(5, "B")]},
    ):
        assert my_cmd._unique_channel_owner("chan-1") is None


# ---------------------------------------------------------------------------
# /user set-channel: figyelmeztet, de nem blokkol
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_channel_warns_but_still_sets_when_the_channel_is_taken():
    """Az ütközés figyelmeztetés, NEM hiba — a beállítás megtörténik.

    Blokkolni rosszabb lenne: a user csatorna nélkül maradna, és semmit nem
    kapna. A figyelmeztetés viszont megnevezi, kivel ütközik.
    """
    cog = users_cmd.UsersCog(mock.Mock())
    interaction = _Interaction()
    interaction.user = mock.Mock(id=555, display_name="Máté", name="mate", nick=None)
    channel = SimpleNamespace(id=999)

    with mock.patch.object(users_cmd.users_storage, "get_or_create_user",
                           return_value=(_user(5, "Máté"), False)), \
         mock.patch.object(users_cmd.users_storage, "find_users_by_alerts_channel",
                           return_value=[_user(1, "Adam_PlanSmart", "999")]), \
         mock.patch.object(users_cmd.users_storage, "set_user_alerts_channel",
                           return_value=True) as beallit, \
         mock.patch.object(users_cmd.audit, "log_action") as naplo:
        await users_cmd.UsersCog.set_channel.callback(cog, interaction, channel)

    beallit.assert_called_once(), "a csatorna beállítása nem maradhat el"
    uzenet = " ".join(interaction.sent())
    assert "beállítva" in uzenet
    assert "már foglalt" in uzenet and "Adam_PlanSmart" in uzenet
    # Az audit sorban is ott a konfliktus — a log rotálódhat, az audit nem.
    assert naplo.call_args.kwargs["details"]["conflicts_with"] == [
        {"id": 1, "display_name": "Adam_PlanSmart"}
    ]


@pytest.mark.asyncio
async def test_set_channel_is_silent_when_the_channel_is_free():
    cog = users_cmd.UsersCog(mock.Mock())
    interaction = _Interaction()
    interaction.user = mock.Mock(id=555, display_name="Máté", name="mate", nick=None)
    channel = SimpleNamespace(id=999)

    with mock.patch.object(users_cmd.users_storage, "get_or_create_user",
                           return_value=(_user(5, "Máté"), False)), \
         mock.patch.object(users_cmd.users_storage, "find_users_by_alerts_channel",
                           return_value=[]), \
         mock.patch.object(users_cmd.users_storage, "set_user_alerts_channel",
                           return_value=True), \
         mock.patch.object(users_cmd.audit, "log_action"):
        await users_cmd.UsersCog.set_channel.callback(cog, interaction, channel)

    uzenet = " ".join(interaction.sent())
    assert "beállítva" in uzenet
    assert "foglalt" not in uzenet and "⚠️" not in uzenet


@pytest.mark.asyncio
async def test_set_channel_does_not_warn_about_the_user_themselves():
    """Ugyanarra a csatornára újra beállítva NINCS figyelmeztetés."""
    cog = users_cmd.UsersCog(mock.Mock())
    interaction = _Interaction()
    interaction.user = mock.Mock(id=555, display_name="Máté", name="mate", nick=None)
    channel = SimpleNamespace(id=999)

    with mock.patch.object(users_cmd.users_storage, "get_or_create_user",
                           return_value=(_user(5, "Máté"), False)), \
         mock.patch.object(users_cmd.users_storage, "find_users_by_alerts_channel",
                           return_value=[_user(5, "Máté", "999")]), \
         mock.patch.object(users_cmd.users_storage, "set_user_alerts_channel",
                           return_value=True), \
         mock.patch.object(users_cmd.audit, "log_action"):
        await users_cmd.UsersCog.set_channel.callback(cog, interaction, channel)

    assert "foglalt" not in " ".join(interaction.sent())


# ---------------------------------------------------------------------------
# /user list: az admin egy pillantással látja az ütközést
# ---------------------------------------------------------------------------

def test_channel_occupancy_groups_users_by_channel():
    rows = [_user(1, "A", "x"), _user(5, "B", "x"), _user(6, "C", "y"),
            _user(4, "bot", None)]
    foglaltsag = users_cmd._channel_occupancy(rows)

    assert [u["id"] for u in foglaltsag["x"]] == [1, 5]
    assert [u["id"] for u in foglaltsag["y"]] == [6]
    assert None not in foglaltsag


@pytest.mark.asyncio
async def test_user_list_marks_the_colliding_channels():
    cog = users_cmd.UsersCog(mock.Mock())
    interaction = _Interaction()
    rows = [_user(1, "Adam_PlanSmart", "x"), _user(5, "Máté", "x"),
            _user(6, "Nándi", "y")]

    with mock.patch.object(users_cmd.users_storage, "list_users", return_value=rows):
        await users_cmd.UsersCog.list_cmd.callback(cog, interaction)

    embed = interaction.followup.send.call_args.kwargs["embed"]
    leiras = embed.description
    assert "ütközik" in leiras
    assert leiras.count("ütközik") == 2, "mindkét érintett soron ott a jelölés"
    assert "Nándi" in leiras and leiras.split("Nándi")[1].count("ütközik") == 0
    assert "1 csatorna több userhez tartozik" in embed.footer.text
