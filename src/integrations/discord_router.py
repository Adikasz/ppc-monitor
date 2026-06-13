"""
Discord alert-küldő.

A scheduler/alert-router ezen keresztül küld riasztásokat Discord csatornákra:
  - CRITICAL → kritikus csatorna (config: DISCORD_CRITICAL_ALERTS_CHANNEL_ID),
               a felelős AM-ek megemlítésével (mention)
  - WARNING / egyéb → összefoglaló csatorna (DISCORD_MONITORING_SUMMARY_CHANNEL_ID),
               mention nélkül

A küldéshez a FUTÓ bot kliensre van szükség — a bot indulásakor (main.py
on_ready) a `set_client(bot)` köti be. Ha nincs kliens vagy nincs csatorna
konfigurálva, a küldés warning-gal kihagyásra kerül (nem hiba).

Hibatűrés:
  - 429 (rate limit) → exponenciális visszalépéssel újrapróbál
  - egyéb hiba       → log + None (a router/scheduler nem áll le)
"""
from __future__ import annotations

import asyncio
from typing import Any

import discord

from src.config import get_config
from src.utils.logging import get_logger

log = get_logger(__name__)

# A futó bot kliens (commands.Bot). A main.py on_ready állítja be.
_client: Any = None


def set_client(client: Any) -> None:
    """A Discord bot kliens bekötése (a bot indulásakor hívandó)."""
    global _client
    _client = client
    log.info("Discord router kliens beállítva")


async def _resolve_channel(channel_id_raw: str) -> Any:
    """Csatorna objektum a konfigurált ID-ből (cache vagy API). None ha nem megy."""
    if not channel_id_raw:
        return None
    if _client is None:
        log.warning("Discord router: nincs kliens beállítva (set_client) — küldés kihagyva")
        return None
    try:
        cid = int(channel_id_raw)
    except (TypeError, ValueError):
        log.warning("Discord router: érvénytelen csatorna ID: %r", channel_id_raw)
        return None

    channel = _client.get_channel(cid)
    if channel is None:
        try:
            channel = await _client.fetch_channel(cid)
        except Exception as exc:  # noqa: BLE001
            log.warning("Discord router: a csatorna nem elérhető (%s): %s", cid, exc)
            return None
    return channel


async def send_discord_alert(
    alert: dict[str, Any],
    recipients: list[dict[str, Any]],
    *,
    campaign_label: str,
) -> dict[str, Any] | None:
    """Egy riasztás kiküldése a megfelelő Discord csatornára.

    Paraméterek:
        alert          — alert sor (severity, message, campaign_id, …)
        recipients     — címzettek [{"discord_user_id": "...", …}]
        campaign_label — "Ügyfél / Kampány" emberi címke a fejléchez

    Visszatérés: {"channel_id", "message_id"} siker esetén, különben None.
    """
    severity = (alert.get("severity") or "warning").lower()
    config = get_config()

    if severity == "critical":
        channel_id = config.discord_critical_alerts_channel_id
        header_emoji, header_label = "🔴", "CRITICAL"
        mentions = " ".join(
            f"<@{r['discord_user_id']}>"
            for r in recipients
            if r.get("discord_user_id")
        )
    else:
        channel_id = config.discord_monitoring_summary_channel_id
        header_emoji = "🟡" if severity == "warning" else "🔵"
        header_label = severity.upper()
        mentions = ""  # WARNING/egyéb: nincs mention

    channel = await _resolve_channel(channel_id)
    if channel is None:
        log.warning(
            "Discord alert nem küldhető (severity=%s) — nincs konfigurált/elérhető csatorna",
            severity,
        )
        return None

    assigned_line = f"\n\nAssigned: {mentions}" if mentions else ""
    info_line = f"\n`/campaign info campaign_id:{alert.get('campaign_id')}`"
    content = (
        f"{header_emoji} **{header_label}** — {campaign_label}\n"
        f"{alert.get('message', '')}"
        f"{assigned_line}"
        f"{info_line}"
    )

    allowed = discord.AllowedMentions(users=bool(mentions), roles=False, everyone=False)

    # 429-re exponenciális retry (a discord.py belül is kezeli, ez extra védelem)
    for attempt in range(3):
        try:
            msg = await channel.send(content, allowed_mentions=allowed)
            log.info(
                "Discord alert kiküldve (severity=%s, csatorna=%s, msg=%s)",
                severity, channel.id, msg.id,
            )
            return {"channel_id": channel.id, "message_id": msg.id}
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429 and attempt < 2:
                wait = 2 ** attempt
                log.warning("Discord 429 (rate limit) — újrapróba %ss múlva", wait)
                await asyncio.sleep(wait)
                continue
            log.error("Discord küldési hiba (severity=%s): %s", severity, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            log.error("Discord váratlan küldési hiba (severity=%s): %s", severity, exc)
            return None

    return None
