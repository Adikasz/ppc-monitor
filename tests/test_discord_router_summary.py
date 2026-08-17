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

    assert res == {"channel_id": 12345, "message_id": 999, "message_ids": [999]}
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

    assert res == {"channel_id": 42, "message_id": 1, "message_ids": [1]}
    fake_channel.send.assert_awaited_once()


async def test_long_summary_is_sent_as_multiple_messages():
    """Egy zajos nap (sok CRITICAL/WARNING) átlépi a 2000 karaktert — a küldő
    több üzenetre bontja, különben a `channel.send` HTTP 400-zal elszállna, és
    az EGÉSZ összefoglaló elveszne."""
    fake_channel = mock.AsyncMock()
    sent: list[str] = []

    async def _send(content, **_kwargs):
        sent.append(content)
        return SimpleNamespace(id=100 + len(sent))

    fake_channel.send = mock.AsyncMock(side_effect=_send)
    fake_channel.id = 777

    noisy = {
        **_SUMMARY,
        "critical_count": 60, "warning_count": 60, "alert_count": 120,
        "top_issues": [
            {"client": "Ügyfél", "campaign": f"Kampány-{i}", "platform": "meta",
             "account_label": f"act_{i}",
             "severity": "critical" if i % 2 else "warning",
             "message": "ROAS 0.00 a cél 3.00 alatt"}
            for i in range(120)
        ],
    }

    with contextlib.ExitStack() as stack:
        _patch(stack, admin_channel_id=_ADMIN_CHANNEL_ID, fake_channel=fake_channel)
        res = await router.send_summary_to_user(
            {"id": 6, "alerts_channel_id": "777"}, noisy, is_weekly=False
        )

    assert len(sent) > 1
    assert all(len(part) <= 2000 for part in sent)
    # Minden kampány kiment, egy sem esett le a listáról.
    joined = "\n".join(sent)
    for i in range(120):
        assert f"Kampány-{i}" in joined
    # A `message_id` az első darab (horgony), a `message_ids` mind.
    assert res["message_id"] == 101
    assert res["message_ids"] == [100 + i + 1 for i in range(len(sent))]


async def test_partial_send_failure_still_reports_what_went_out():
    """Ha a második darab elhasal, az elsőt már megkapta az ügyfél — a hívó
    ne kapjon None-t (az teljes némaságot jelentene)."""
    import discord

    fake_channel = mock.AsyncMock()
    calls = {"n": 0}

    async def _send(content, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(id=555)
        raise discord.HTTPException(SimpleNamespace(status=500), "boom")

    fake_channel.send = mock.AsyncMock(side_effect=_send)
    fake_channel.id = 888

    noisy = {
        **_SUMMARY,
        "critical_count": 120, "alert_count": 120,
        "top_issues": [
            {"client": "Ügyfél", "campaign": f"Kampány-{i}", "platform": "meta",
             "account_label": f"act_{i}", "severity": "critical",
             "message": "ROAS 0.00 a cél 3.00 alatt"}
            for i in range(120)
        ],
    }

    with contextlib.ExitStack() as stack:
        _patch(stack, admin_channel_id=_ADMIN_CHANNEL_ID, fake_channel=fake_channel)
        res = await router.send_summary_to_user(
            {"id": 7, "alerts_channel_id": "888"}, noisy, is_weekly=False
        )

    assert res == {"channel_id": 888, "message_id": 555, "message_ids": [555]}
