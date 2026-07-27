"""
/account add — a `account` mező (korábban `account_id`) platform-szűrt,
NÉVvel kereshető autocomplete-je.

A platformot a namespace-ből olvassuk (ugyanaz a minta, mint a
`/my lifecycle` / `/my mute` campaign-autocomplete-jénél) — Meta és Google
fiók sose keveredhet egy listában, és a platform mező kitöltése előtt a
lista üres.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from src.bot.commands import adaccounts as ad_cmd

pytestmark = pytest.mark.asyncio


class _FakeBot:
    pass


_META_MATCHES = [
    {"id": "act_668362820429286", "name": "Brands_Marquard Media"},
    {"id": "act_129819940882364", "name": "GTX reklama"},
]
_GOOGLE_MATCHES = [
    {"id": "1514469037", "name": "MyMins Google Ads"},
]


def _cog():
    return ad_cmd.AdAccountsCog(_FakeBot())


async def test_platform_nincs_kivalasztva_ures_lista():
    cog = _cog()
    interaction = SimpleNamespace(namespace=SimpleNamespace(platform=None))
    choices = await cog.add_account_autocomplete(interaction, "")
    assert choices == []


async def test_ismeretlen_platform_ures_lista():
    cog = _cog()
    interaction = SimpleNamespace(namespace=SimpleNamespace(platform="tiktok"))
    choices = await cog.add_account_autocomplete(interaction, "")
    assert choices == []


async def test_meta_platform_csak_meta_talalatokat_ad():
    cog = _cog()
    interaction = SimpleNamespace(namespace=SimpleNamespace(platform="meta"))
    with mock.patch.object(
        ad_cmd.account_catalog, "search_accounts", return_value=list(_META_MATCHES),
    ) as search, mock.patch.object(
        ad_cmd.ad_accounts_storage, "list_ad_accounts", return_value=[],
    ):
        choices = await cog.add_account_autocomplete(interaction, "")

    assert search.call_args[0][0] == "meta"
    values = {c.value for c in choices}
    assert values == {"act_668362820429286", "act_129819940882364"}
    assert all("(" in c.name and ")" in c.name for c in choices)


async def test_google_platform_csak_google_talalatokat_ad():
    cog = _cog()
    interaction = SimpleNamespace(namespace=SimpleNamespace(platform="google"))
    with mock.patch.object(
        ad_cmd.account_catalog, "search_accounts", return_value=list(_GOOGLE_MATCHES),
    ) as search, mock.patch.object(
        ad_cmd.ad_accounts_storage, "list_ad_accounts", return_value=[],
    ):
        choices = await cog.add_account_autocomplete(interaction, "")

    assert search.call_args[0][0] == "google"
    assert [c.value for c in choices] == ["1514469037"]
    assert "MyMins Google Ads (1514469037)" == choices[0].name


async def test_mar_regisztralt_fiok_megjelolve_es_hatrasorolva():
    cog = _cog()
    interaction = SimpleNamespace(namespace=SimpleNamespace(platform="meta"))
    with mock.patch.object(
        ad_cmd.account_catalog, "search_accounts", return_value=list(_META_MATCHES),
    ), mock.patch.object(
        ad_cmd.ad_accounts_storage, "list_ad_accounts",
        return_value=[{"external_account_id": "act_668362820429286"}],
    ):
        choices = await cog.add_account_autocomplete(interaction, "")

    # a mar regisztralt (act_668362820429286) hatrebb kerul, es (V)-vel jelolt
    assert choices[-1].value == "act_668362820429286"
    assert choices[-1].name.startswith("✓ ")
    assert not choices[0].name.startswith("✓ ")


async def test_kezi_beiras_akkor_is_valaszthato_ha_nincs_api_talalat():
    cog = _cog()
    interaction = SimpleNamespace(namespace=SimpleNamespace(platform="meta"))
    with mock.patch.object(
        ad_cmd.account_catalog, "search_accounts", return_value=[],
    ), mock.patch.object(
        ad_cmd.ad_accounts_storage, "list_ad_accounts", return_value=[],
    ):
        choices = await cog.add_account_autocomplete(interaction, "act_1846078935426937")

    assert len(choices) == 1
    assert choices[0].value == "act_1846078935426937"
    assert "Kézi azonosító" in choices[0].name


async def test_api_hiba_eseten_a_kezi_beiras_meg_mindig_mukodik():
    cog = _cog()
    interaction = SimpleNamespace(namespace=SimpleNamespace(platform="meta"))
    with mock.patch.object(
        ad_cmd.account_catalog, "search_accounts", return_value=[],
    ), mock.patch.object(
        ad_cmd.ad_accounts_storage, "list_ad_accounts",
        side_effect=RuntimeError("DB hiba"),
    ):
        choices = await cog.add_account_autocomplete(interaction, "act_999")

    assert [c.value for c in choices] == ["act_999"]
