"""
send_summary_to_user — az admin csatorna NE kapjon napi/heti összefoglalót.

Sem a saját alerts_channel_id-ja (ha véletlenül az admin csatornára van
állítva), sem a fallback (nincs alerts_channel_id → admin csatorna) nem
eredményezhet kiküldött összefoglalót. Más csatornák viselkedése változatlan.
A csatorna-egyezést `_parse_channel_id`-vel normalizálva vizsgáljuk, mert a
DISCORD_ADMIN_CHANNEL_ID korábban már URL-ként is elő tudott fordulni.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest import mock

import pytest

from src.integrations import discord_router as router

pytestmark = pytest.mark.asyncio

_SUMMARY = {
    "total_campaigns": 2, "critical_count": 0, "warning_count": 0,
    "alert_count": 0, "healthy_campaigns": 2, "top_issues": [],
    "from": "2026-07-15T00:00:00+02:00", "to": "2026-07-16T00:00:00+02:00",
}

_ADMIN_CHANNEL_ID = "1509567720868417656"


def _patch(stack, *, admin_channel_id, fake_channel):
    stack.enter_context(mock.patch.object(
        router, "get_config",
        return_value=SimpleNamespace(discord_admin_channel_id=admin_channel_id),
    ))
    stack.enter_context(mock.patch.object(
        router, "_resolve_channel", new=mock.AsyncMock(return_value=fake_channel),
    ))


async def test_summary_skipped_when_fallback_resolves_to_admin_channel():
    fake_channel = mock.AsyncMock()
    with contextlib.ExitStack() as stack:
        _patch(stack, admin_channel_id=_ADMIN_CHANNEL_ID, fake_channel=fake_channel)
        res = await router.send_summary_to_user(
            {"id": 1, "alerts_channel_id": None}, _SUMMARY, is_weekly=False
        )

    assert res is None
    fake_channel.send.assert_not_awaited()


async def test_summary_skipped_when_alerts_channel_id_equals_admin_channel():
    fake_channel = mock.AsyncMock()
    with contextlib.ExitStack() as stack:
        _patch(stack, admin_channel_id=_ADMIN_CHANNEL_ID, fake_channel=fake_channel)
        res = await router.send_summary_to_user(
            {"id": 2, "alerts_channel_id": _ADMIN_CHANNEL_ID}, _SUMMARY, is_weekly=False
        )

    assert res is None
    fake_channel.send.assert_not_awaited()


async def test_summary_skipped_when_admin_channel_configured_as_url():
    """DISCORD_ADMIN_CHANNEL_ID korábban már teljes URL-ként is be lett állítva —
    a normalizált (nem nyers string-) összehasonlításnak ekkor is el kell
    kapnia az egyezést, ha a user csatornája ugyanaz a fizikai csatorna."""
    fake_channel = mock.AsyncMock()
    admin_url = f"https://discordapp.com/channels/111111111111111111/{_ADMIN_CHANNEL_ID}"
    with contextlib.ExitStack() as stack:
        _patch(stack, admin_channel_id=admin_url, fake_channel=fake_channel)
        res = await router.send_summary_to_user(
            {"id": 5, "alerts_channel_id": _ADMIN_CHANNEL_ID}, _SUMMARY, is_weekly=False
        )

    assert res is None
    fake_channel.send.assert_not_awaited()


async def test_summary_sent_normally_for_personal_channel():
    fake_channel = mock.AsyncMock()
    fake_channel.send = mock.AsyncMock(return_value=SimpleNamespace(id=999))
    fake_channel.id = 12345
    with contextlib.ExitStack() as stack:
        _patch(stack, admin_channel_id=_ADMIN_CHANNEL_ID, fake_channel=fake_channel)
        res = await router.send_summary_to_user(
            {"id": 3, "alerts_channel_id": "12345"}, _SUMMARY, is_weekly=False
        )

    assert res == {"channel_id": 12345, "message_id": 999}
    fake_channel.send.assert_awaited_once()


async def test_summary_sent_when_admin_channel_not_configured():
    """Ha nincs DISCORD_ADMIN_CHANNEL_ID beállítva, nincs mit kiszűrni — küldés megy."""
    fake_channel = mock.AsyncMock()
    fake_channel.send = mock.AsyncMock(return_value=SimpleNamespace(id=1))
    fake_channel.id = 42
    with contextlib.ExitStack() as stack:
        _patch(stack, admin_channel_id="", fake_channel=fake_channel)
        res = await router.send_summary_to_user(
            {"id": 4, "alerts_channel_id": None}, _SUMMARY, is_weekly=False
        )

    assert res == {"channel_id": 42, "message_id": 1}
    fake_channel.send.assert_awaited_once()
