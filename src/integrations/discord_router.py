"""
Discord alert-küldő.

A scheduler/alert-router ezen keresztül küld riasztásokat Discord csatornákra.
A CSATORNÁT a router választja ki (per-OM személyes csatorna vagy admin
fallback), ez a modul csak a formázásért és a tényleges küldésért felel:
  - megkapja a cél `channel_id`-t és az alertet
  - severity szerint formáz (🔴 CRITICAL / 🟡 WARNING / 🔵 INSIGHT)
  - opcionálisan kiegészíti a "Értesítve még: …" sorral (other_recipients),
    illetve admin-fallback figyelmeztetésekkel (missing_channel_user / no_assignee)

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


def _parse_channel_id(raw: str) -> int | None:
    """Csatorna ID kinyerése nyers ID-ből VAGY Discord URL-ből.

    Elfogad:
      - "1509567720868417656"                      → 1509567720868417656
      - ".../channels/<guild>/<channel>"           → <channel> (utolsó szegmens)
      - "<#1509567720868417656>" mention formátum  → 1509567720868417656

    Így a .env-be véletlenül beillesztett teljes csatorna-URL is működik
    (gyakori hiba), nem csak a tiszta numerikus ID.
    """
    if raw is None:
        return None
    text = str(raw).strip().strip("<#>")
    if not text:
        return None
    # URL vagy bármilyen elválasztó esetén az utolsó nem-üres szegmenst vesszük.
    if "/" in text:
        segments = [s for s in text.split("/") if s]
        text = segments[-1] if segments else ""
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


async def _resolve_channel(channel_id_raw: str) -> Any:
    """Csatorna objektum a konfigurált ID-ből (cache vagy API). None ha nem megy."""
    if not channel_id_raw:
        return None
    if _client is None:
        log.warning("Discord router: nincs kliens beállítva (set_client) — küldés kihagyva")
        return None
    cid = _parse_channel_id(channel_id_raw)
    if cid is None:
        log.warning("Discord router: érvénytelen csatorna ID/URL: %r", channel_id_raw)
        return None

    channel = _client.get_channel(cid)
    if channel is None:
        try:
            channel = await _client.fetch_channel(cid)
        except Exception as exc:  # noqa: BLE001
            log.warning("Discord router: a csatorna nem elérhető (%s): %s", cid, exc)
            return None
    return channel


_SEVERITY_STYLE = {
    "critical": ("🔴", "CRITICAL"),
    "warning": ("🟡", "WARNING"),
    "insight": ("🔵", "INSIGHT"),
}


def _severity_style(severity: str) -> tuple[str, str]:
    """(emoji, címke) a severityhez — ismeretlen severity → 🔵 + felirat."""
    return _SEVERITY_STYLE.get(severity, ("🔵", severity.upper()))


async def send_discord_alert(
    channel_id: str,
    alert: dict[str, Any],
    *,
    campaign_label: str,
    other_recipients: list[str] | None = None,
    missing_channel_user: str | None = None,
    no_assignee: bool = False,
) -> dict[str, Any] | None:
    """Egy riasztás kiküldése egy KONKRÉT Discord csatornára.

    A csatorna kiválasztása a router feladata; ez a függvény csak formáz és küld.

    Paraméterek:
        channel_id           — a cél Discord csatorna ID-ja (string)
        alert                — alert sor (severity, message, campaign_id, …)
        campaign_label       — "Ügyfél / Kampány" emberi címke a fejléchez
        other_recipients     — már megformázott "@Név (role)" stringek; ha meg
                               van adva, a "Értesítve még: …" sor kerül az
                               üzenetbe (csak megjelenítés, NEM pingel)
        missing_channel_user — admin-fallback: ennek a usernek (discord_user_id)
                               nincs személyes csatornája; figyelmeztető sor +
                               ping kerül az üzenetbe
        no_assignee          — admin-fallback: a kampánynak nincs hozzárendeltje

    Visszatérés: {"channel_id", "message_id"} siker esetén, különben None.
    """
    severity = (alert.get("severity") or "warning").lower()
    header_emoji, header_label = _severity_style(severity)

    channel = await _resolve_channel(channel_id)
    if channel is None:
        log.warning(
            "Discord alert nem küldhető (severity=%s) — nincs konfigurált/elérhető csatorna (id=%r)",
            severity, channel_id,
        )
        return None

    lines = [
        f"{header_emoji} **{header_label}** — {campaign_label}",
        alert.get("message", ""),
    ]

    if other_recipients:
        lines.append("")
        lines.append(f"Értesítve még: {', '.join(other_recipients)}")

    if missing_channel_user:
        lines.append("")
        lines.append(
            f"⚠️ <@{missing_channel_user}> alert csatornája nincs beállítva — "
            f"a `/user set-channel` paranccsal állítható."
        )
    elif no_assignee:
        lines.append("")
        lines.append(
            f"⚠️ Nem hozzárendelt kampány — "
            f"`/assign campaign_id:{alert.get('campaign_id')} user:@valaki`"
        )

    if severity == "critical":
        lines.append(f"`/campaign info campaign_id:{alert.get('campaign_id')}`")

    lines.append("─────────────")
    content = "\n".join(lines)

    # Pingelni CSAK az admin-fallback érintettjét pingeljük (hogy észrevegye,
    # hogy az ő alertje admin csatornán landolt). A "Értesítve még" csak vizuális.
    if missing_channel_user:
        try:
            allowed = discord.AllowedMentions(
                users=[discord.Object(id=int(missing_channel_user))],
                roles=False,
                everyone=False,
            )
        except (TypeError, ValueError):
            allowed = discord.AllowedMentions.none()
    else:
        allowed = discord.AllowedMentions.none()

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


# ---------------------------------------------------------------------------
# Napi / heti összefoglaló (13. lépés)
# ---------------------------------------------------------------------------

def _date_label(iso: str | None) -> str:
    """ISO timestamp → 'YYYY-MM-DD' (a dátum-fejlécekhez). Üres ha nincs."""
    if not iso:
        return "?"
    try:
        from datetime import datetime
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d")
    except ValueError:
        return str(iso)[:10]


def _format_summary(summary: dict[str, Any], is_weekly: bool) -> str:
    """Összefoglaló Discord-üzenet szövege (napi vagy heti formátum)."""
    total = summary.get("total_campaigns", 0)
    crit = summary.get("critical_count", 0)
    warn = summary.get("warning_count", 0)
    healthy = summary.get("healthy_campaigns", 0)
    alert_count = summary.get("alert_count", 0)
    top_issues = summary.get("top_issues") or []

    from_label = _date_label(summary.get("from"))
    to_label = _date_label(summary.get("to"))

    # Nincs anomália → rövid, pozitív üzenet.
    if alert_count == 0:
        if is_weekly:
            return (
                f"✅ **Hétvégi összefoglaló** — {from_label} → {to_label}\n"
                f"A hétvégén nem volt anomália. Minden kampány rendben. 🎉"
            )
        return (
            f"✅ **Napi összefoglaló** — {from_label}\n"
            f"Tegnap nem volt anomália. Minden kampány rendben. 🎉"
        )

    if is_weekly:
        header = f"📊 **Hétvégi összefoglaló** — {from_label} → {to_label}"
        issues_title = "**Top problémák hétvégén:**"
        alerts_label = "Alertek hétvégén"
    else:
        header = f"📊 **Napi összefoglaló** — {from_label}"
        issues_title = "**Top problémák:**"
        alerts_label = "Alertek tegnap"

    lines = [
        header,
        "",
        f"🔴 Kritikus: {crit}  |  🟡 Figyelmeztetés: {warn}  |  ✅ Egészséges: {healthy}",
    ]

    if top_issues:
        lines.append("")
        lines.append(issues_title)
        for issue in top_issues:
            lines.append(f"• {issue.get('campaign', '?')} — {issue.get('message', '')}")

    lines.append("")
    lines.append(f"**Kampányok figyelve:** {total}  |  **{alerts_label}:** {alert_count}")
    lines.append("─────────────")
    return "\n".join(lines)


async def send_summary_to_user(
    user: dict[str, Any],
    summary: dict[str, Any],
    *,
    is_weekly: bool = False,
) -> dict[str, Any] | None:
    """Egy összefoglaló kiküldése a user személyes csatornájába (vagy admin fallback).

    Visszatérés: {"channel_id", "message_id"} siker esetén, különben None.
    """
    channel_id = user.get("alerts_channel_id") or get_config().discord_admin_channel_id
    channel = await _resolve_channel(channel_id)
    if channel is None:
        log.warning(
            "Összefoglaló nem küldhető (user #%s) — nincs feloldható csatorna",
            user.get("id"),
        )
        return None

    content = _format_summary(summary, is_weekly)
    allowed = discord.AllowedMentions.none()

    for attempt in range(3):
        try:
            msg = await channel.send(content, allowed_mentions=allowed)
            log.info(
                "Összefoglaló kiküldve (user #%s, weekly=%s, csatorna=%s, msg=%s)",
                user.get("id"), is_weekly, channel.id, msg.id,
            )
            return {"channel_id": channel.id, "message_id": msg.id}
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429 and attempt < 2:
                wait = 2 ** attempt
                log.warning("Discord 429 (összefoglaló) — újrapróba %ss múlva", wait)
                await asyncio.sleep(wait)
                continue
            log.error("Összefoglaló küldési hiba (user #%s): %s", user.get("id"), exc)
            return None
        except Exception as exc:  # noqa: BLE001
            log.error("Összefoglaló váratlan küldési hiba (user #%s): %s", user.get("id"), exc)
            return None

    return None
