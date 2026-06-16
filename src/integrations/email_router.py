"""
Email-küldő — CRITICAL riasztások az ügyfélnek (14. lépés).

A CRITICAL alertekről emailt küldünk az ügyfél kapcsolattartói címére
(clients.contact_email), SMTP-n keresztül (smtplib, STARTTLS).

Mikor NEM küld (mindegyik csak warning/skip, sosem dob a routingba):
  - nem CRITICAL az alert
  - az ügyfélnek nincs contact_email-je
  - az SMTP nincs konfigurálva (.env: SMTP_HOST/USER/PASSWORD/FROM)
  - már ment email erről az alertről (a dedupot a router végzi email_sent_at-tal)

Az smtplib blokkoló, ezért a tényleges küldés asyncio.to_thread()-ben fut.
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

from src.config import get_config
from src.utils.logging import get_logger

log = get_logger(__name__)


def _smtp_configured(cfg: Any) -> bool:
    return bool(cfg.smtp_host and cfg.smtp_user and cfg.smtp_password and cfg.smtp_from)


def _build_message(
    alert: dict[str, Any],
    client: dict[str, Any],
    campaign: dict[str, Any] | None,
    *,
    from_addr: str,
    to_addr: str,
) -> EmailMessage:
    """Plain text + HTML email összeállítása."""
    client_name = client.get("name") or "Ügyfelünk"
    campaign_name = (
        (campaign or {}).get("name")
        or f"#{alert.get('campaign_id')}"
    )
    metric = alert.get("metric") or "anomália"
    message = alert.get("message") or ""
    detected_at = alert.get("detected_at") or "—"

    subject = f"[RIASZTÁS] {client_name} — {campaign_name}: {metric}"

    plain = (
        f"Tisztelt {client_name}!\n\n"
        f"Hirdetési kampányában anomáliát észleltünk:\n\n"
        f"Kampány: {campaign_name}\n"
        f"Probléma: {message}\n"
        f"Észlelés: {detected_at}\n\n"
        f"Kezelőnk hamarosan felveszi Önnel a kapcsolatot.\n\n"
        f"Üdvözlettel,\n"
        f"MyMins csapata"
    )

    html = (
        f"<html><body style=\"font-family:Arial,sans-serif;color:#222;\">"
        f"<p>Tisztelt <strong>{client_name}</strong>!</p>"
        f"<p>Hirdetési kampányában anomáliát észleltünk:</p>"
        f"<ul>"
        f"<li><strong>Kampány:</strong> {campaign_name}</li>"
        f"<li><strong>Probléma:</strong> {message}</li>"
        f"<li><strong>Észlelés:</strong> {detected_at}</li>"
        f"</ul>"
        f"<p>Kezelőnk hamarosan felveszi Önnel a kapcsolatot.</p>"
        f"<p>Üdvözlettel,<br>MyMins csapata</p>"
        f"</body></html>"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    return msg


def _send_via_smtp(cfg: Any, msg: EmailMessage) -> None:
    """Blokkoló SMTP küldés (to_thread-ből hívva)."""
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(cfg.smtp_user, cfg.smtp_password)
        server.send_message(msg)


async def send_client_email(
    alert: dict[str, Any],
    client: dict[str, Any],
    campaign: dict[str, Any] | None = None,
) -> bool:
    """Egy CRITICAL riasztás kiküldése emailben az ügyfélnek.

    Visszatérés: True ha kiment, False ha kihagytuk/hiba (a routing nem áll le).
    """
    import asyncio

    severity = (alert.get("severity") or "").lower()
    if severity != "critical":
        return False

    to_addr = client.get("contact_email")
    if not to_addr:
        log.warning(
            "Email skip — az ügyfélnek (%s) nincs contact_email-je (alert #%s)",
            client.get("name"), alert.get("id"),
        )
        return False

    cfg = get_config()
    if not _smtp_configured(cfg):
        log.warning(
            "Email skip — SMTP nincs konfigurálva (.env: SMTP_HOST/USER/PASSWORD/FROM)"
        )
        return False

    msg = _build_message(alert, client, campaign, from_addr=cfg.smtp_from, to_addr=to_addr)

    try:
        await asyncio.to_thread(_send_via_smtp, cfg, msg)
        log.info(
            "Ügyfél-email kiküldve: %s → %s (alert #%s)",
            cfg.smtp_from, to_addr, alert.get("id"),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — email hiba sosem állítja le a routingot
        log.error("Email küldési hiba (alert #%s, címzett=%s): %s", alert.get("id"), to_addr, exc)
        return False
