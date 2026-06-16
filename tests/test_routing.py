"""
Routing unit tesztek — per-OM csatornák, admin fallback, multi-assignee, csendes idő.

A teszt a `src.monitoring.router.route_alert` döntéseit ellenőrzi izoláltan:
minden külső függést (Supabase storage, Discord küldés, config, csendes idő)
mockolunk, így nincs hálózat és valós idő-függés.

Futtatás:
    .venv\\Scripts\\python.exe -m pytest tests/ -v
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest import mock

import pytest

from src.monitoring import router

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Segéd: a router külső függéseinek mockolása egy ExitStack-ben
# ---------------------------------------------------------------------------

def _patch_router(
    stack: contextlib.ExitStack,
    *,
    recipients: list[dict],
    admin_channel_id: str = "admin999",
    is_quiet: bool = False,
    send_result: dict | None = None,
    clickup_result: dict | None = None,
) -> mock.AsyncMock:
    """Beállítja a router összes külső függőségét, és visszaadja a Discord-küldő AsyncMock-ot."""
    if send_result is None:
        send_result = {"channel_id": 1, "message_id": 42}

    stack.enter_context(mock.patch.object(
        router.campaigns_storage, "get_campaign",
        return_value={"id": 1, "name": "TestKampány", "campaign_type": "meta", "ad_account_id": 5},
    ))
    stack.enter_context(mock.patch.object(router.mutes_storage, "is_muted", return_value=False))
    stack.enter_context(mock.patch.object(
        router, "_resolve_client", return_value={"id": 10, "name": "TestÜgyfél"},
    ))
    stack.enter_context(mock.patch.object(router, "_resolve_recipients", return_value=recipients))
    stack.enter_context(mock.patch.object(
        router, "get_config", return_value=SimpleNamespace(discord_admin_channel_id=admin_channel_id),
    ))
    stack.enter_context(mock.patch.object(router.quiet_hours, "is_quiet_now", return_value=is_quiet))
    stack.enter_context(mock.patch.object(router.alerts_storage, "mark_alert_routed"))
    stack.enter_context(mock.patch.object(router.alerts_storage, "mark_alert_suppressed"))
    stack.enter_context(mock.patch.object(
        router.clickup_router, "create_clickup_task",
        new=mock.AsyncMock(return_value=clickup_result),
    ))

    send = mock.AsyncMock(return_value=send_result)
    stack.enter_context(mock.patch.object(router.discord_router, "send_discord_alert", new=send))
    return send


def _alert(severity: str = "warning") -> dict:
    return {
        "id": 1,
        "campaign_id": 1,
        "severity": severity,
        "metric": "test_alert",
        "message": "🧪 TEST",
    }


def _recipient(discord_id: str, channel: str | None, role: str = "primary") -> dict:
    return {
        "discord_user_id": discord_id,
        "display_name": f"User{discord_id}",
        "role": role,
        "alerts_channel_id": channel,
    }


# ---------------------------------------------------------------------------
# Tesztek
# ---------------------------------------------------------------------------

async def test_route_alert_with_assignee_and_channel():
    """Van assignee saját csatornával → az ő csatornájára megy."""
    with contextlib.ExitStack() as stack:
        send = _patch_router(stack, recipients=[_recipient("david", "111")])
        result = await router.route_alert(_alert("warning"))

    assert result["routed"] is True
    send.assert_awaited_once()
    # Az első pozicionális argumentum a cél csatorna ID.
    assert send.await_args.args[0] == "111"
    assert "discord" in result["channels"]


async def test_route_alert_admin_fallback_no_channel():
    """Assignee csatorna nélkül → admin csatorna + missing_channel_user figyelmeztetés."""
    with contextlib.ExitStack() as stack:
        send = _patch_router(stack, recipients=[_recipient("david", None)], admin_channel_id="admin999")
        result = await router.route_alert(_alert("warning"))

    send.assert_awaited_once()
    assert send.await_args.args[0] == "admin999"
    assert send.await_args.kwargs.get("missing_channel_user") == "david"
    assert result["routed"] is True


async def test_route_alert_no_assignee():
    """Nincs assignee → admin csatorna + no_assignee figyelmeztetés (/assign hint)."""
    with contextlib.ExitStack() as stack:
        send = _patch_router(stack, recipients=[], admin_channel_id="admin999")
        result = await router.route_alert(_alert("warning"))

    send.assert_awaited_once()
    assert send.await_args.args[0] == "admin999"
    assert send.await_args.kwargs.get("no_assignee") is True


async def test_route_alert_multi_assignee():
    """Két assignee, mindkettőnek van csatornája → mindkettő megkapja, 'Értesítve még'-gel."""
    recipients = [
        _recipient("david", "111", role="primary"),
        _recipient("adam", "222", role="supporter"),
    ]
    with contextlib.ExitStack() as stack:
        send = _patch_router(stack, recipients=recipients)
        result = await router.route_alert(_alert("warning"))

    assert send.await_count == 2
    target_channels = [call.args[0] for call in send.await_args_list]
    assert "111" in target_channels and "222" in target_channels

    # Dávid üzenetében Ádám szerepeljen "Értesítve még"-ként (és fordítva).
    by_channel = {call.args[0]: call.kwargs.get("other_recipients") for call in send.await_args_list}
    david_others = " ".join(by_channel["111"] or [])
    assert "adam" in david_others
    assert result["routed"] is True


async def test_route_alert_multi_assignee_formats_notified_line():
    """A valódi discord_router formázás tartalmazza a 'Értesítve még:' sort."""
    fake_channel = SimpleNamespace(id=111, send=mock.AsyncMock(return_value=SimpleNamespace(id=7)))
    recipients = [
        _recipient("david", "111", role="primary"),
        _recipient("adam", "222", role="supporter"),
    ]
    with contextlib.ExitStack() as stack:
        # A router belső függéseit mockoljuk, DE a discord_router.send_discord_alert
        # valódi marad — csak a csatorna-feloldást cseréljük.
        stack.enter_context(mock.patch.object(
            router.campaigns_storage, "get_campaign",
            return_value={"id": 1, "name": "K", "campaign_type": "meta", "ad_account_id": 5},
        ))
        stack.enter_context(mock.patch.object(router.mutes_storage, "is_muted", return_value=False))
        stack.enter_context(mock.patch.object(router, "_resolve_client", return_value={"id": 10, "name": "Ü"}))
        stack.enter_context(mock.patch.object(router, "_resolve_recipients", return_value=recipients))
        stack.enter_context(mock.patch.object(
            router, "get_config", return_value=SimpleNamespace(discord_admin_channel_id="admin999"),
        ))
        stack.enter_context(mock.patch.object(router.quiet_hours, "is_quiet_now", return_value=False))
        stack.enter_context(mock.patch.object(router.alerts_storage, "mark_alert_routed"))
        stack.enter_context(mock.patch.object(
            router.discord_router, "_resolve_channel", new=mock.AsyncMock(return_value=fake_channel),
        ))
        await router.route_alert(_alert("warning"))

    sent_contents = [call.args[0] for call in fake_channel.send.await_args_list]
    assert any("Értesítve még:" in c for c in sent_contents)


async def test_parse_channel_id_accepts_url_and_raw():
    """A _parse_channel_id nyers ID-t, Discord URL-t és mention-t is elfogad."""
    from src.integrations.discord_router import _parse_channel_id

    assert _parse_channel_id("1509567720868417656") == 1509567720868417656
    assert _parse_channel_id(
        "https://discordapp.com/channels/1509562591305924709/1509567720868417656"
    ) == 1509567720868417656
    assert _parse_channel_id("<#1509567720868417656>") == 1509567720868417656
    assert _parse_channel_id("") is None
    assert _parse_channel_id("nem-szam") is None


async def test_route_alert_reason_no_channel_when_send_fails():
    """Ha egyetlen küldés sem sikerül (pl. nem feloldható csatorna) → reason='no_channel'."""
    with contextlib.ExitStack() as stack:
        send = _patch_router(stack, recipients=[_recipient("david", "111")])
        send.return_value = None  # minden küldés sikertelen
        result = await router.route_alert(_alert("critical"))

    assert result["routed"] is False
    assert result["reason"] == "no_channel"


async def test_quiet_hours_suppresses_warning():
    """Csendes időben a WARNING elnyomódik (status='suppressed'), de a CRITICAL kimegy."""
    # WARNING — csendes időben NEM megy ki
    with contextlib.ExitStack() as stack:
        send = _patch_router(stack, recipients=[_recipient("david", "111")], is_quiet=True)
        mark_suppressed = stack.enter_context(
            mock.patch.object(router.alerts_storage, "mark_alert_suppressed")
        )
        result = await router.route_alert(_alert("warning"))

    send.assert_not_awaited()
    assert result["reason"] == "quiet_hours"
    mark_suppressed.assert_called_once_with(1)

    # CRITICAL — csendes idő NEM blokkolja
    with contextlib.ExitStack() as stack:
        send = _patch_router(stack, recipients=[_recipient("david", "111")], is_quiet=True)
        result = await router.route_alert(_alert("critical"))

    send.assert_awaited_once()
    assert result["routed"] is True
