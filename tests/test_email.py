"""
Email router unit tesztek (14. lépés) — formátum + skip/küldés feltételek.

Nincs valódi SMTP: a tényleges küldést (_send_via_smtp) mockoljuk.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from src.integrations import email_router

_SMTP_CFG = SimpleNamespace(
    smtp_host="smtp.example.com",
    smtp_port=587,
    smtp_user="bot@example.com",
    smtp_password="app-password",
    smtp_from="noreply@mymins.hu",
)
_NO_SMTP_CFG = SimpleNamespace(
    smtp_host="", smtp_port=587, smtp_user="", smtp_password="", smtp_from="",
)

_ALERT = {
    "id": 1, "campaign_id": 2, "severity": "critical",
    "metric": "cpa_spike", "message": "CPA +67%", "detected_at": "2026-06-16T10:00:00+02:00",
}
_CLIENT = {"id": 1, "name": "Stopvill", "contact_email": "ugyfel@example.com"}
_CAMPAIGN = {"id": 2, "name": "Sales - LEGRAND"}


def test_build_message_subject_and_body():
    msg = email_router._build_message(
        _ALERT, _CLIENT, _CAMPAIGN, from_addr="noreply@mymins.hu", to_addr="ugyfel@example.com",
    )
    assert msg["Subject"] == "[RIASZTÁS] Stopvill — Sales - LEGRAND: cpa_spike"
    assert msg["To"] == "ugyfel@example.com"
    assert msg["From"] == "noreply@mymins.hu"
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert "Tisztelt Stopvill!" in body
    assert "CPA +67%" in body
    assert "MyMins csapata" in body
    # van HTML alternatíva is
    assert msg.get_body(preferencelist=("html",)) is not None


@pytest.mark.asyncio
async def test_send_skips_non_critical():
    with mock.patch.object(email_router, "_send_via_smtp") as send:
        ok = await email_router.send_client_email({**_ALERT, "severity": "warning"}, _CLIENT, _CAMPAIGN)
    assert ok is False
    send.assert_not_called()


@pytest.mark.asyncio
async def test_send_skips_without_contact_email():
    with mock.patch.object(email_router, "_send_via_smtp") as send:
        ok = await email_router.send_client_email(_ALERT, {**_CLIENT, "contact_email": None}, _CAMPAIGN)
    assert ok is False
    send.assert_not_called()


@pytest.mark.asyncio
async def test_send_skips_when_smtp_not_configured():
    with mock.patch.object(email_router, "get_config", return_value=_NO_SMTP_CFG), \
         mock.patch.object(email_router, "_send_via_smtp") as send:
        ok = await email_router.send_client_email(_ALERT, _CLIENT, _CAMPAIGN)
    assert ok is False
    send.assert_not_called()


@pytest.mark.asyncio
async def test_send_success_calls_smtp():
    with mock.patch.object(email_router, "get_config", return_value=_SMTP_CFG), \
         mock.patch.object(email_router, "_send_via_smtp") as send:
        ok = await email_router.send_client_email(_ALERT, _CLIENT, _CAMPAIGN)
    assert ok is True
    send.assert_called_once()
    # a cfg és a megépített üzenet ment át
    cfg_arg, msg_arg = send.call_args.args
    assert cfg_arg is _SMTP_CFG
    assert msg_arg["To"] == "ugyfel@example.com"


@pytest.mark.asyncio
async def test_send_returns_false_on_smtp_error():
    with mock.patch.object(email_router, "get_config", return_value=_SMTP_CFG), \
         mock.patch.object(email_router, "_send_via_smtp", side_effect=OSError("connrefused")):
        ok = await email_router.send_client_email(_ALERT, _CLIENT, _CAMPAIGN)
    assert ok is False  # hiba sosem dob a routingba
