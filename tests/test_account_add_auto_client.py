"""
/account add — a `client` mező eltávolítása után a kliens AUTOMATIKUSAN
feloldódik/létrejön a fiók API-neve alapján (vagy a `client_name` kézi
fallback paraméterből, ha a fiók nincs a platform API-katalógusában).

Lefedett szabályok:
  - API-listás fiók (van API-név) → a kliens neve az API-név, `client_name`
    paraméter EZ ESETBEN nem szükséges (és nem is befolyásolja a kliensnevet).
  - Kis/nagybetű-független egyezés → nem duplikál klienst.
  - Kézi azonosító (nincs API-név) → `client_name` KÖTELEZŐ, hiánya esetén
    a parancs elutasít, MIELŐTT bármit írna a DB-be.
  - `account_name` felülírás csak az `ad_accounts.account_name`-re hat, a
    kliensnév-feloldásra NEM (az mindig az API-név vagy a `client_name`).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from src.bot.commands import adaccounts as ad_cmd

pytestmark = pytest.mark.asyncio


class _FakeBot:
    pass


class _FakeResponse:
    async def defer(self, ephemeral=True):
        pass


class _FakeFollowup:
    def __init__(self):
        self.messages: list[str] = []

    async def send(self, content=None, **kwargs):
        self.messages.append(content)


def _interaction():
    followup = _FakeFollowup()
    itn = SimpleNamespace(
        response=_FakeResponse(),
        followup=followup,
        channel_id=1,
        user=SimpleNamespace(id=999),
    )
    return itn, followup


def _cog():
    return ad_cmd.AdAccountsCog(_FakeBot())


async def _call_add(cog, interaction, **kwargs):
    """`add` az `@app_commands.command` miatt egy `Command` objektum a cog-on
    — a nyers coroutine-t a `.callback`-en át kell hívni, explicit `self`-fel."""
    return await cog.add.callback(cog, interaction, **kwargs)


def _patches(
    stack,
    *,
    api_name=None,
    existing_client=None,
    created_client=None,
    account_row=None,
    account_created=True,
):
    stack.enter_context(mock.patch.object(ad_cmd, "_is_admin_channel", return_value=True))
    stack.enter_context(mock.patch.object(
        ad_cmd.account_catalog, "find_account_name", return_value=api_name,
    ))
    find_client = stack.enter_context(mock.patch.object(
        ad_cmd.clients_storage, "find_client_by_name_ci", return_value=existing_client,
    ))
    create_client = stack.enter_context(mock.patch.object(
        ad_cmd.clients_storage, "create_client",
        return_value=created_client or {"id": 500, "name": "Uj Kliens", "is_active": True},
    ))
    get_or_create = stack.enter_context(mock.patch.object(
        ad_cmd.ad_accounts_storage, "get_or_create_ad_account",
        return_value=(account_row or {"id": 900, "is_active": True}, account_created),
    ))
    stack.enter_context(mock.patch.object(ad_cmd.audit, "log_action"))
    return find_client, create_client, get_or_create


async def test_api_named_fiok_uj_klienst_hoz_letre():
    itn, followup = _interaction()
    import contextlib
    with contextlib.ExitStack() as stack:
        find_client, create_client, get_or_create = _patches(
            stack, api_name="Brands_Marquard Media", existing_client=None,
            created_client={"id": 40, "name": "Brands_Marquard Media", "is_active": True},
        )
        await _call_add(_cog(), itn, platform="meta", account="act_668362820429286")

    create_client.assert_called_once_with("Brands_Marquard Media")
    assert get_or_create.call_args[0][0] == 40  # client_id
    assert get_or_create.call_args[1]["account_name"] == "Brands_Marquard Media"
    assert any("ÚJ ügyfél létrehozva" in m for m in followup.messages)
    assert any("Brands_Marquard Media" in m for m in followup.messages)


async def test_api_named_fiok_meglevo_klienshez_kotodik_kis_nagybetu_fuggetlenul():
    itn, followup = _interaction()
    import contextlib
    with contextlib.ExitStack() as stack:
        find_client, create_client, get_or_create = _patches(
            stack, api_name="gtx reklama",  # kis/nagybetűs eltérés a DB-hez képest
            existing_client={"id": 74, "name": "GTX reklama", "is_active": True},
        )
        await _call_add(_cog(), itn, platform="meta", account="act_129819940882364")

    create_client.assert_not_called()
    assert get_or_create.call_args[0][0] == 74
    assert any("meglévő ügyfélhez kötve" in m for m in followup.messages)
    assert any("GTX reklama" in m for m in followup.messages)


async def test_kezi_azonosito_client_name_nelkul_elutasit_es_nem_ir_db_be():
    itn, followup = _interaction()
    import contextlib
    with contextlib.ExitStack() as stack:
        find_client, create_client, get_or_create = _patches(stack, api_name=None)
        await _call_add(_cog(), itn, platform="meta", account="act_1846078935426937")

    create_client.assert_not_called()
    get_or_create.assert_not_called()
    assert any("client_name" in m for m in followup.messages)


async def test_kezi_azonosito_client_name_vel_mukodik():
    itn, followup = _interaction()
    import contextlib
    with contextlib.ExitStack() as stack:
        find_client, create_client, get_or_create = _patches(
            stack, api_name=None, existing_client=None,
            created_client={"id": 77, "name": "Életerő.info", "is_active": True},
        )
        await _call_add(
            _cog(), itn, platform="meta", account="act_1846078935426937",
            client_name="Életerő.info",
        )

    create_client.assert_called_once_with("Életerő.info")
    assert get_or_create.call_args[1]["account_name"] == "Életerő.info"
    assert any("Életerő.info" in m for m in followup.messages)


async def test_account_name_felulirja_a_tarolt_nevet_de_a_klienst_nem():
    """account_name CSAK az ad_accounts.account_name-re hat, a kliens-
    feloldásra nem — a kliens neve mindig az API-név marad."""
    itn, followup = _interaction()
    import contextlib
    with contextlib.ExitStack() as stack:
        find_client, create_client, get_or_create = _patches(
            stack, api_name="Brands_Marquard Media", existing_client=None,
            created_client={"id": 40, "name": "Brands_Marquard Media", "is_active": True},
        )
        await _call_add(
            _cog(), itn, platform="meta", account="act_668362820429286",
            account_name="Egyedi megjelenítési név",
        )

    create_client.assert_called_once_with("Brands_Marquard Media")
    assert get_or_create.call_args[1]["account_name"] == "Egyedi megjelenítési név"


async def test_mar_regisztralt_fiok_ujraaktivalasa():
    itn, followup = _interaction()
    import contextlib
    with contextlib.ExitStack() as stack:
        find_client, create_client, get_or_create = _patches(
            stack, api_name="GTX reklama",
            existing_client={"id": 74, "name": "GTX reklama", "is_active": True},
            account_row={"id": 900, "is_active": False, "client_id": 74},
            account_created=False,
        )
        set_active = stack.enter_context(mock.patch.object(
            ad_cmd.ad_accounts_storage, "set_ad_account_active",
        ))
        await _call_add(_cog(), itn, platform="meta", account="act_129819940882364")

    set_active.assert_called_once_with(900, True)
    assert any("már regisztrálva van" in m for m in followup.messages)
    assert any("újra aktiválva" in m for m in followup.messages)


async def test_mar_regisztralt_fiok_a_valodi_tulajdonos_nevet_mutatja():
    """Ha a fiók API-neve megváltozott és most más klienshez oldódna fel,
    a 'már regisztrálva van' válasz a TÉNYLEGES tulajdonost mutatja, nem a
    most (félrevezetően) feloldott klienst."""
    itn, followup = _interaction()
    import contextlib
    with contextlib.ExitStack() as stack:
        find_client, create_client, get_or_create = _patches(
            stack, api_name="Uj Nev Kft",
            existing_client={"id": 55, "name": "Uj Nev Kft", "is_active": True},
            account_row={"id": 900, "is_active": True, "client_id": 74},  # a VALODI tulajdonos: 74
            account_created=False,
        )
        get_client = stack.enter_context(mock.patch.object(
            ad_cmd.clients_storage, "get_client",
            return_value={"id": 74, "name": "GTX reklama", "is_active": True},
        ))
        await _call_add(_cog(), itn, platform="meta", account="act_129819940882364")

    get_client.assert_called_once_with(74)
    assert any("GTX reklama" in m for m in followup.messages)
    assert not any("Uj Nev Kft" in m for m in followup.messages)


async def test_inaktiv_kliens_eseten_elutasit():
    itn, followup = _interaction()
    import contextlib
    with contextlib.ExitStack() as stack:
        find_client, create_client, get_or_create = _patches(
            stack, api_name="GTX reklama",
            existing_client={"id": 74, "name": "GTX reklama", "is_active": False},
        )
        await _call_add(_cog(), itn, platform="meta", account="act_129819940882364")

    create_client.assert_not_called()
    get_or_create.assert_not_called()
    assert any("INAKTÍV" in m for m in followup.messages)


async def test_ures_account_mezo_elutasit():
    itn, followup = _interaction()
    import contextlib
    with contextlib.ExitStack() as stack:
        _patches(stack, api_name="X")
        await _call_add(_cog(), itn, platform="meta", account="   ")

    assert any("nem lehet üres" in m for m in followup.messages)


async def test_nem_admin_csatornabol_elutasit():
    itn, followup = _interaction()
    with mock.patch.object(ad_cmd, "_is_admin_channel", return_value=False):
        await _call_add(_cog(), itn, platform="meta", account="act_129819940882364")

    assert any("admin csatorná" in m for m in followup.messages)
