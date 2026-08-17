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
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import discord

from src.config import get_config
from src.utils.logging import get_logger

log = get_logger(__name__)

# Discord üzenet-limit 2000 karakter; kis margóval dolgozunk (a `channel.send`
# HTTP 400-zal elszáll, ha átlépjük — lásd `split_message`).
_MAX_MESSAGE_CHARS = 1900

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


async def send_text_message(channel_id_raw: str, content: str) -> dict[str, Any] | None:
    """Egyszerű szöveges üzenet küldése egy csatornára (nem alert-formátum).

    A token-figyelő (17. lépés) használja az admin csatornára. A csatorna-
    feloldás és a 429 retry ugyanaz, mint az alert-küldésnél. Soha nem dob:
    hiba esetén None.
    """
    channel = await _resolve_channel(channel_id_raw)
    if channel is None:
        log.warning("send_text_message: nincs feloldható csatorna (id=%r)", channel_id_raw)
        return None

    allowed = discord.AllowedMentions.none()
    for attempt in range(3):
        try:
            msg = await channel.send(content, allowed_mentions=allowed)
            log.info("Szöveges üzenet kiküldve (csatorna=%s, msg=%s)", channel.id, msg.id)
            return {"channel_id": channel.id, "message_id": msg.id}
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429 and attempt < 2:
                await asyncio.sleep(2 ** attempt)
                continue
            log.error("send_text_message hiba: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001
            log.error("send_text_message váratlan hiba: %s", exc)
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
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d")
    except ValueError:
        return str(iso)[:10]


def _inclusive_end_date_label(iso: str | None) -> str:
    """EXKLUZÍV felső határ → az utolsó BENNE lévő nap dátuma (fejléchez).

    A munkanapi ablak szombat 00:00-val zárul, de az összefoglaló hétfő–péntek —
    a fejlécben ezért a péntek dátuma a helyes.
    """
    if not iso:
        return "?"
    try:
        return (datetime.fromisoformat(iso) - timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return str(iso)[:10]


def _local_detected_at(issue: dict[str, Any]) -> datetime | None:
    """Az anomália észlelési ideje a KONFIGURÁLT időzónában. None, ha nincs/hibás.

    A konverzió nem elhagyható: az `alerts.detected_at` `timestamptz`, amit a
    PostgREST UTC-ben ad vissza — nyers string-vágással egy 10:32-es magyar
    észlelés 08:32-ként jelenne meg.

    Sosem dob: hibás/hiányzó időbélyeg miatt nem eshet szét az összefoglaló,
    olyankor egyszerűen elmarad az időpont a sor végéről.
    """
    raw = issue.get("detected_at")
    if not raw:
        return None

    if isinstance(raw, datetime):
        parsed = raw
    else:
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    if parsed.tzinfo is None:
        # A DB UTC-ben tárol — a tz nélküli érték ennek a konvenciónak felel meg.
        parsed = parsed.replace(tzinfo=timezone.utc)

    try:
        return parsed.astimezone(ZoneInfo(get_config().timezone or "UTC"))
    except (KeyError, ValueError):  # ismeretlen/hibás időzóna a configban
        return parsed


def _detected_at_suffix(issue: dict[str, Any], *, with_date: bool) -> str:
    """` (14:32)` a problémasor végére — több napot átfogó listánál ` (06-14 14:32)`."""
    detected = _local_detected_at(issue)
    if detected is None:
        return ""
    return f" ({detected.strftime('%m-%d %H:%M' if with_date else '%H:%M')})"


# Ennyi problémáig lapos listát adunk (mint korábban); efölött severity
# szerint csoportosítunk, hogy a hosszú lista is átlátható maradjon.
_FLAT_ISSUE_LIST_MAX = 15

# Csoport-fejlécek a súlyosság szerinti bontáshoz (megjelenítési sorrendben):
# (severity, emoji, fejléc-címke, ragozott alak a "… és még N további …" sorhoz)
_SEVERITY_GROUPS: tuple[tuple[str, str, str, str], ...] = (
    ("critical", "🔴", "KRITIKUS", "kritikus riasztás"),
    ("warning", "🟡", "FIGYELMEZTETÉS", "figyelmeztetés"),
    ("insight", "💡", "INSIGHT", "insight"),
)


def _issue_line(issue: dict[str, Any]) -> str:
    """Egy problémasor: `• Ügyfél [PLATFORM · fiók] — kampány — üzenet`."""
    client = issue.get("client") or "?"
    plat = issue.get("platform")
    account_label = issue.get("account_label")
    if plat and account_label:
        tag = f" [{plat.upper()} · {account_label}]"
    elif plat:
        tag = f" [{plat.upper()}]"
    else:
        tag = ""
    return f"• {client}{tag} — {issue.get('campaign', '?')} — {issue.get('message', '')}"


def _grouped_issue_lines(
    top_issues: list[dict[str, Any]],
    summary: dict[str, Any],
    *,
    line_fn: Callable[[dict[str, Any]], str],
) -> tuple[list[str], int]:
    """Súlyosság szerint csoportosított problémalista, csoportonkénti darabszámmal.

    A csoport-fejlécben a TELJES napi darabszám áll (`critical_count` /
    `warning_count`), nem a kilistázott sorok száma — ha a biztonsági plafon
    miatt kevesebb sor jelenik meg, azt a csoport végén külön jelezzük.

    Visszatérés: (sorok, csoportszinten már jelzett rejtett sorok száma) — így
    a hívó csak a maradékot írja ki, nem számol duplán.
    """
    by_severity: dict[str, list[dict[str, Any]]] = {}
    for issue in top_issues:
        by_severity.setdefault((issue.get("severity") or "").lower(), []).append(issue)

    # A fenti táblában nem szereplő (ismeretlen) severity-k se vesszenek el.
    known = {key for key, _, _, _ in _SEVERITY_GROUPS}
    groups = list(_SEVERITY_GROUPS) + [
        (sev, "⚪", (sev or "EGYÉB").upper(), "riasztás")
        for sev in by_severity
        if sev not in known
    ]

    # A teljes darabszám csak a critical/warning-ra ismert az összesítőből;
    # a többinél a kilistázott sorok száma az igazság.
    totals = {
        "critical": summary.get("critical_count"),
        "warning": summary.get("warning_count"),
    }

    lines: list[str] = []
    accounted_hidden = 0
    for severity, emoji, label, hidden_noun in groups:
        issues = by_severity.get(severity) or []
        if not issues:
            continue
        total_for_group = totals.get(severity)
        if not isinstance(total_for_group, int) or total_for_group < len(issues):
            total_for_group = len(issues)

        lines.append("")
        lines.append(f"{emoji} **{label}: {total_for_group} db**")
        lines.extend(line_fn(issue) for issue in issues)

        hidden = total_for_group - len(issues)
        if hidden > 0:
            accounted_hidden += hidden
            lines.append(f"*… és még {hidden} további {hidden_noun}*")

    return lines, accounted_hidden


def issue_section_lines(
    top_issues: list[dict[str, Any]],
    summary: dict[str, Any],
    *,
    line_fn: Callable[[dict[str, Any]], str] = _issue_line,
) -> list[str]:
    """A problémalista sorai — a napi és az azonnali összefoglaló KÖZÖS logikája.

    Rövid listánál (≤ `_FLAT_ISSUE_LIST_MAX`) lapos felsorolás, efölött
    súlyosság szerinti csoportosítás darabszámmal. A végén — ha a summary-réteg
    biztonsági plafonja levágott sorokat (`issues_truncated`) — jelzi a
    maradékot; amit a csoport-fejlécek már jeleztek, azt nem számolja újra.

    A `line_fn` teszi hívhatóvá többféle összefoglalóból: minden nézetnek más a
    sor-formátuma (a napiban benne a platform, az azonnaliban a fiók már a
    fejlécben szerepel), a csoportosítás és a csonkítás-jelzés viszont közös.

    Minden sor végére kerül az észlelés időpontja (`detected_at`), ha az adott
    nézet megadja — így elég ide beépíteni, mindhárom összefoglaló megkapja.
    """
    if not top_issues:
        return []

    # Ha a lista több napot fog át (heti összefoglaló: péntek 22:00 → hétfő
    # 08:00), a puszta óra:perc félrevezető — péntek 14:32 és vasárnap 14:32
    # ugyanúgy nézne ki. Ilyenkor dátummal írjuk ki.
    days = {d.date() for d in map(_local_detected_at, top_issues) if d is not None}
    with_date = len(days) > 1

    def _render(issue: dict[str, Any]) -> str:
        return line_fn(issue) + _detected_at_suffix(issue, with_date=with_date)

    accounted_hidden = 0
    if len(top_issues) <= _FLAT_ISSUE_LIST_MAX:
        lines = [_render(issue) for issue in top_issues]
    else:
        lines, accounted_hidden = _grouped_issue_lines(
            top_issues, summary, line_fn=_render
        )

    remaining_hidden = (summary.get("issues_truncated") or 0) - accounted_hidden
    if remaining_hidden > 0:
        lines.append(f"*… és még {remaining_hidden} további riasztás*")
    return lines


def _format_summary(
    summary: dict[str, Any],
    is_weekly: bool = False,
    *,
    kind: str | None = None,
) -> str:
    """Összefoglaló Discord-üzenet szövege.

    Három változat (`kind`): "daily", "weekend" és "workweek" — a fejléc, a
    lista-cím és az alert-címke tér el, a tartalom-előállítás azonos.
    A `kind` elhagyható; ilyenkor az `is_weekly` dönt ("weekend" vagy "daily"),
    így a régi hívások változatlanul működnek.

    A problémalista NINCS top-N-re vágva: az ablak minden riasztása szerepel.
    Rövid listánál (≤ 15) lapos felsorolás, efölött súlyosság szerinti
    csoportosítás darabszámmal, soronként az észlelés idejével. A 2000
    karakteres Discord-limitet a küldő (`send_summary_to_user`) kezeli több
    üzenetre bontással.
    """
    kind = kind or ("weekend" if is_weekly else "daily")

    total = summary.get("total_campaigns", 0)
    crit = summary.get("critical_count", 0)
    warn = summary.get("warning_count", 0)
    healthy = summary.get("healthy_campaigns", 0)
    alert_count = summary.get("alert_count", 0)
    top_issues = summary.get("top_issues") or []

    from_label = _date_label(summary.get("from"))
    to_label = _date_label(summary.get("to"))
    # A munkanapi ablak felső határa EXKLUZÍV (szombat 00:00) — a fejlécben az
    # utolsó BENNE lévő napot (péntek) mutatjuk, különben szombatot írnánk ki
    # egy hétfő–péntek összefoglalóra.
    workweek_to_label = _inclusive_end_date_label(summary.get("to"))

    # Nincs anomália → rövid, pozitív üzenet.
    if alert_count == 0:
        if kind == "workweek":
            return (
                f"✅ **Heti összefoglaló — munkanapok (hétfő–péntek)** — "
                f"{from_label} → {workweek_to_label}\n"
                f"A héten nem volt anomália. Minden kampány rendben. 🎉"
            )
        if kind == "weekend":
            return (
                f"✅ **Hétvégi összefoglaló** — {from_label} → {to_label}\n"
                f"A hétvégén nem volt anomália. Minden kampány rendben. 🎉"
            )
        return (
            f"✅ **Napi összefoglaló** — {from_label}\n"
            f"Tegnap nem volt anomália. Minden kampány rendben. 🎉"
        )

    if kind == "workweek":
        header = (
            f"📊 **Heti összefoglaló — munkanapok (hétfő–péntek)** — "
            f"{from_label} → {workweek_to_label}"
        )
        issues_title = "**Problémák a héten:**"
        alerts_label = "Alertek a héten"
    elif kind == "weekend":
        header = f"📊 **Hétvégi összefoglaló** — {from_label} → {to_label}"
        issues_title = "**Problémák hétvégén:**"
        alerts_label = "Alertek hétvégén"
    else:
        header = f"📊 **Napi összefoglaló** — {from_label}"
        issues_title = "**Problémák:**"
        alerts_label = "Alertek tegnap"

    # Az összesítő sor MINDIG a teljes darabszám (nem a kilistázott soroké).
    lines = [
        header,
        "",
        f"🔴 Kritikus: {crit}  |  🟡 Figyelmeztetés: {warn}  |  ✅ Egészséges: {healthy}",
    ]

    if top_issues:
        lines.append("")
        lines.append(issues_title)
        lines.extend(issue_section_lines(top_issues, summary))

    lines.append("")
    lines.append(f"**Kampányok figyelve:** {total}  |  **{alerts_label}:** {alert_count}")
    lines.append("─────────────")
    return "\n".join(lines)


def split_message(content: str, limit: int = _MAX_MESSAGE_CHARS) -> list[str]:
    """Hosszú üzenet felbontása a Discord 2000 karakteres limitje alá.

    Sorhatáron vág, hogy egy problémasor ne törjön ketté. Semmi nem vész el:
    egy önmagában túl hosszú sor (elvben nem fordul elő, de a `message` mező
    szabad szöveg) darabokra bontva megy ki.

    Ez a napi/heti összefoglalónál vált szükségessé: mióta MINDEN riasztás
    szerepel benne (nem csak top 5), egy zajos nap átlépheti a 2000 karaktert —
    darabolás nélkül a `channel.send` HTTP 400-zal elszállna, és az ügyfél az
    EGÉSZ összefoglalót elveszítené.
    """
    if len(content) <= limit:
        return [content]

    parts: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in content.split("\n"):
        while len(line) > limit:
            if current:
                parts.append("\n".join(current))
                current, current_len = [], 0
            parts.append(line[:limit])
            line = line[limit:]

        projected = current_len + len(line) + (1 if current else 0)
        if current and projected > limit:
            parts.append("\n".join(current))
            current, current_len = [line], len(line)
        else:
            current.append(line)
            current_len = projected

    if current:
        parts.append("\n".join(current))
    return parts


async def _send_with_retry(
    channel: Any,
    content: str,
    allowed: discord.AllowedMentions,
    *,
    user_id: Any,
) -> Any | None:
    """Egy üzenetdarab kiküldése 429-re exponenciális visszalépéssel."""
    for attempt in range(3):
        try:
            return await channel.send(content, allowed_mentions=allowed)
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429 and attempt < 2:
                wait = 2 ** attempt
                log.warning("Discord 429 (összefoglaló) — újrapróba %ss múlva", wait)
                await asyncio.sleep(wait)
                continue
            log.error("Összefoglaló küldési hiba (user #%s): %s", user_id, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            log.error("Összefoglaló váratlan küldési hiba (user #%s): %s", user_id, exc)
            return None
    return None


async def send_summary_to_user(
    user: dict[str, Any],
    summary: dict[str, Any],
    *,
    is_weekly: bool = False,
    kind: str | None = None,
) -> dict[str, Any] | None:
    """Egy összefoglaló kiküldése a user személyes csatornájába (vagy admin fallback).

    A `kind` ("daily" | "weekend" | "workweek") választja a formátumot; ha
    nincs megadva, az `is_weekly` dönt (visszafelé kompatibilis).

    Hosszú összefoglaló több üzenetre bomlik (Discord 2000 karakteres limit).

    Visszatérés: {"channel_id", "message_id", "message_ids"} siker esetén (a
    `message_id` az ELSŐ darabé — ez a horgony), különben None.
    """
    channel_id = user.get("alerts_channel_id") or get_config().discord_admin_channel_id

    # Üzleti szándék: az admin csatorna NE kapjon napi/heti összefoglalót (sem
    # a saját alerts_channel_id-ja miatt, sem a fallback miatt) — csak a hozzá
    # nem rendelt kampányok CRITICAL alert-fallbackja mehet oda (az a
    # `router.route_alert`-en megy, ezt a függvényt nem érinti). A
    # `_parse_channel_id`-vel normalizálva hasonlítunk, mert a
    # DISCORD_ADMIN_CHANNEL_ID-t korábban már belapátolták teljes URL-ként is
    # (lásd `_parse_channel_id` docstring) — nyers string-egyezés ezt kihagyná.
    admin_channel_id = _parse_channel_id(get_config().discord_admin_channel_id)
    if admin_channel_id is not None and _parse_channel_id(channel_id) == admin_channel_id:
        log.info(
            "Összefoglaló kihagyva (user #%s) — a célcsatorna az admin csatorna",
            user.get("id"),
        )
        return None

    channel = await _resolve_channel(channel_id)
    if channel is None:
        log.warning(
            "Összefoglaló nem küldhető (user #%s) — nincs feloldható csatorna",
            user.get("id"),
        )
        return None

    content = _format_summary(summary, is_weekly, kind=kind)
    allowed = discord.AllowedMentions.none()
    parts = split_message(content)

    message_ids: list[int] = []
    for index, part in enumerate(parts):
        msg = await _send_with_retry(channel, part, allowed, user_id=user.get("id"))
        if msg is None:
            # Részleges kiküldés: ami kiment, az kiment — az első darab ID-ja
            # a horgony, hogy a hívó lássa, nem volt teljes némaság.
            log.error(
                "Összefoglaló megszakadt a %d/%d. darabnál (user #%s)",
                index + 1, len(parts), user.get("id"),
            )
            break
        message_ids.append(msg.id)

    if not message_ids:
        return None

    log.info(
        "Összefoglaló kiküldve (user #%s, típus=%s, csatorna=%s, %d üzenet, msg=%s)",
        user.get("id"), kind or ("weekend" if is_weekly else "daily"),
        channel.id, len(message_ids), message_ids[0],
    )
    return {
        "channel_id": channel.id,
        "message_id": message_ids[0],
        "message_ids": message_ids,
    }
