"""
campaign_id (int) autocomplete — /campaign info|set-state|kpi (egyszeres mód) és
/my mute|unmute. Ugyanaz a minta, mint a már meglévő /campaign end és /my
lifecycle campaign autocomplete-je: a Choice.value int marad (backward compat
a nyers numerikus ID-vel), csak a keresés/megjelenítés kap név-alapú találatot.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest import mock

import pytest

from src.bot.commands import campaigns as campaigns_cmd
from src.bot.commands import my_commands as my_cmd

pytestmark = pytest.mark.asyncio


class _FakeBot:
    pass


# ---------------------------------------------------------------------------
# /campaign info | set-state | kpi (egyszeres campaign_id:) — globális keresés
# ---------------------------------------------------------------------------

_SEARCH_ROWS = [
    {"id": 10, "name": "Sales-LEGRAND", "lifecycle_state": "mature"},
    {"id": 11, "name": "Sales-Other", "lifecycle_state": "new"},
]


async def test_info_campaign_autocomplete_returns_int_choices():
    cog = campaigns_cmd.CampaignsCog(_FakeBot())
    with mock.patch.object(
        campaigns_cmd.campaigns_storage, "search_campaigns", return_value=_SEARCH_ROWS,
    ) as m:
        choices = await cog.info_campaign_autocomplete(None, "Sales")

    m.assert_called_once_with("Sales")
    assert [c.value for c in choices] == [10, 11]
    assert all(isinstance(c.value, int) for c in choices)
    assert "#10 Sales-LEGRAND (mature)" in choices[0].name


async def test_set_state_campaign_autocomplete_returns_int_choices():
    cog = campaigns_cmd.CampaignsCog(_FakeBot())
    with mock.patch.object(
        campaigns_cmd.campaigns_storage, "search_campaigns", return_value=_SEARCH_ROWS,
    ):
        choices = await cog.set_state_campaign_autocomplete(None, "Sales")

    assert [c.value for c in choices] == [10, 11]


async def test_kpi_campaign_id_autocomplete_returns_int_choices():
    cog = campaigns_cmd.CampaignsCog(_FakeBot())
    with mock.patch.object(
        campaigns_cmd.campaigns_storage, "search_campaigns", return_value=_SEARCH_ROWS,
    ):
        choices = await cog.kpi_campaign_id_autocomplete(None, "Sales")

    assert [c.value for c in choices] == [10, 11]


async def test_kpi_campaigns_plural_autocomplete_unaffected():
    """A többes 'campaigns' mező (vesszős lista) autocomplete-je nem változott."""
    cog = campaigns_cmd.CampaignsCog(_FakeBot())
    with mock.patch.object(
        campaigns_cmd.campaigns_storage, "search_campaigns", return_value=_SEARCH_ROWS,
    ):
        choices = await cog.kpi_campaigns_autocomplete(None, "A,Sal")

    assert choices[0].value == "A,10"
    assert isinstance(choices[0].value, str)


# ---------------------------------------------------------------------------
# /my mute | unmute campaign_id: — SAJÁT fiókra szkópolt keresés
# ---------------------------------------------------------------------------

def _patch_owned(stack, *, owner, resolve_status="ok", resolve_accounts=None, owned_ids=None, campaign_rows=None):
    stack.enter_context(mock.patch.object(
        my_cmd, "_channel_owner", return_value=owner,
    ))
    stack.enter_context(mock.patch.object(
        my_cmd.account_assignments_storage, "resolve_accounts",
        return_value={"status": resolve_status, "accounts": resolve_accounts or []},
    ))
    stack.enter_context(mock.patch.object(
        my_cmd.account_assignments_storage, "get_account_ids_for_user",
        return_value=owned_ids or [],
    ))
    stack.enter_context(mock.patch.object(
        my_cmd.campaigns_storage, "search_campaign_choices",
        return_value=campaign_rows or [],
    ))


async def test_owned_campaign_choices_scoped_to_single_owned_account():
    cog = my_cmd.MyCommandsCog(_FakeBot())
    interaction = SimpleNamespace(channel_id=123, namespace=SimpleNamespace(account="16"))
    with contextlib.ExitStack() as stack:
        _patch_owned(
            stack,
            owner={"id": 7},
            resolve_accounts=[{"id": 16, "platform": "meta"}],
            owned_ids=[16],
            campaign_rows=[{"id": 115, "name": "kreatívok"}, {"id": 116, "name": "katalógus"}],
        )
        choices = await cog._owned_campaign_choices(interaction, "")

    assert [c.value for c in choices] == [115, 116]
    assert all(isinstance(c.value, int) for c in choices)


async def test_owned_campaign_choices_empty_when_account_not_owned():
    cog = my_cmd.MyCommandsCog(_FakeBot())
    interaction = SimpleNamespace(channel_id=123, namespace=SimpleNamespace(account="999"))
    with contextlib.ExitStack() as stack:
        _patch_owned(
            stack,
            owner={"id": 7},
            resolve_accounts=[{"id": 999, "platform": "meta"}],
            owned_ids=[16],  # 999 nincs benne — nem a sajátja
            campaign_rows=[{"id": 1, "name": "X"}],
        )
        choices = await cog._owned_campaign_choices(interaction, "")

    assert choices == []


async def test_owned_campaign_choices_empty_when_no_account_typed_yet():
    cog = my_cmd.MyCommandsCog(_FakeBot())
    interaction = SimpleNamespace(channel_id=123, namespace=SimpleNamespace(account=None))
    with contextlib.ExitStack() as stack:
        _patch_owned(stack, owner={"id": 7})
        choices = await cog._owned_campaign_choices(interaction, "")

    assert choices == []


async def test_owned_campaign_choices_empty_when_no_channel_owner():
    cog = my_cmd.MyCommandsCog(_FakeBot())
    interaction = SimpleNamespace(channel_id=123, namespace=SimpleNamespace(account="16"))
    with contextlib.ExitStack() as stack:
        _patch_owned(stack, owner=None)
        choices = await cog._owned_campaign_choices(interaction, "")

    assert choices == []


async def test_mute_and_unmute_autocomplete_delegate_to_owned_campaign_choices():
    cog = my_cmd.MyCommandsCog(_FakeBot())
    interaction = SimpleNamespace(channel_id=123, namespace=SimpleNamespace(account="16"))
    sentinel = [mock.Mock()]
    with mock.patch.object(
        my_cmd.MyCommandsCog, "_owned_campaign_choices",
        new=mock.AsyncMock(return_value=sentinel),
    ) as m:
        mute_result = await cog.mute_campaign_autocomplete(interaction, "abc")
        unmute_result = await cog.unmute_campaign_autocomplete(interaction, "abc")

    assert mute_result is sentinel
    assert unmute_result is sentinel
    assert m.await_count == 2
