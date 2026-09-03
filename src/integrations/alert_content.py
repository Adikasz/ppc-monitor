"""
A riasztás EMBERI szövegrészei — egy helyen, két kimenethez.

Ugyanaz a riasztás két csatornán jelenik meg: Discord üzenetként és (CRITICAL
esetén) ClickUp taskként. A kettő szövegének NEM szabad eltérnie — ha a
detektor megfogalmazása változik, mindkettőnek vele kell változnia.

Ezért a közös részek ITT laknak, és mindkét kimenet innen hívja őket:

    campaign_label()            — "Ügyfél [META] / Kampány"  (Discord fejléc)
    local_detected_at()         — az észlelés ideje a konfigurált időzónában
    detected_at_label()         — ugyanez emberi szövegként (ClickUp leírás)
    format_number()             — szám emberi formázása (a detektor is ezt hívja)
    clickup_task_title()        — "ügyfél - platform - leírás (érték)"
    clickup_task_description()  — kampány / érték / észlelés blokk
    discord_jump_url()          — ugrólink a kiküldött üzenetre

FONTOS: itt SEMMILYEN riasztás-szöveg nem generálódik újra. Az anomália
megfogalmazása egyetlen helyen születik — a detektorban (`alerts.message`) —,
ez a modul azt VESZI ÁT és keretezi. Ha ez a szabály sérül, a ClickUp task és a
Discord üzenet elkezd szétcsúszni, és az eltérés csak élesben derül ki.

A modul szándékosan függőségmentes (csak stdlib): nincs config-olvasás sem — az
időzónát a hívó adja át. Így a Discord réteg továbbra is a SAJÁT `get_config`-ját
használja (a tesztek azt mockolják), a formázás pedig önmagában tesztelhető.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

# A ClickUp task neve nem lehet parttalan: a detektor üzenete küszöb-részletekkel
# együtt is elfér ennyiben, a lista-nézet viszont még olvasható marad.
_TITLE_MAX_CHARS = 255

# Hiányzó szám jelölése (a detektor `_fmt` konvenciója).
_NO_VALUE = "—"


# ---------------------------------------------------------------------------
# Számok és címkék
# ---------------------------------------------------------------------------

def format_number(v: float | None) -> str:
    """Szám emberi formázása: egész → tizedesek nélkül, egyébként 2 tizedes.

    Ez a projekt EGYETLEN szám-formázója riasztás-szövegekhez: a detektor
    (`detector._fmt`) is ezt hívja, így a Discord üzenetben és a ClickUp task
    címében ugyanaz a szám ugyanúgy néz ki.
    """
    if v is None:
        return _NO_VALUE
    try:
        value = float(v)
    except (TypeError, ValueError):
        return _NO_VALUE
    return str(int(value)) if value.is_integer() else f"{value:.2f}"


def campaign_label(
    client_name: str | None,
    platform: str | None,
    campaign_name: str | None,
) -> str:
    """"Ügyfél [META] / Kampány" — a Discord riasztás fejléc-címkéje.

    A router korábban helyben állította össze; azért került ide, hogy a
    ClickUp-ág ugyanabból az adatból ugyanazt a kliens/platform megnevezést
    lássa (kis-nagybetűvel együtt), ne egy párhuzamos változatot.
    """
    platform_tag = f" [{platform.upper()}]" if platform else ""
    return f"{client_name or '?'}{platform_tag} / {campaign_name or '?'}"


# ---------------------------------------------------------------------------
# Időbélyeg
# ---------------------------------------------------------------------------

def local_detected_at(row: dict[str, Any], tz_name: str | None) -> datetime | None:
    """Az anomália észlelési ideje a KONFIGURÁLT időzónában. None, ha nincs/hibás.

    A konverzió nem elhagyható: az `alerts.detected_at` `timestamptz`, amit a
    PostgREST UTC-ben ad vissza — nyers string-vágással egy 10:32-es magyar
    észlelés 08:32-ként jelenne meg.

    Sosem dob: hibás/hiányzó időbélyeg miatt nem eshet szét sem az összefoglaló,
    sem a ClickUp task — olyankor egyszerűen elmarad az időpont.
    """
    raw = row.get("detected_at")
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
        return parsed.astimezone(ZoneInfo(tz_name or "UTC"))
    except (KeyError, ValueError):  # ismeretlen/hibás időzóna a configban
        return parsed


def detected_at_label(row: dict[str, Any], tz_name: str | None) -> str:
    """Az észlelés ideje emberi szövegként: "2026-08-31 14:32" (helyi idő).

    Ha az alertnek nincs használható `detected_at`-je (pl. a `/alert test`
    szintetikus sora), a MOSTANI időt adja — a ClickUp taskon mindig legyen
    időpont, mert az a task egyetlen időbeli horgonya.
    """
    local = local_detected_at(row, tz_name)
    if local is None:
        try:
            local = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name or "UTC"))
        except (KeyError, ValueError):
            local = datetime.now(timezone.utc)
    return local.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# ClickUp task szövegek
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _mentions_value(message: str, value: str) -> bool:
    """Szerepel-e a formázott érték ÖNÁLLÓ számként az üzenetben.

    Szándékosan nem sima `in` vizsgálat: a "0" beleillene a "12000"-be, és egy
    ilyen ál-találat miatt pont ott maradna el az érték a címről, ahol kellene.
    Ezért a szám elé/mögé nem eshet további számjegy, tizedespont vagy vessző.
    """
    return re.search(rf"(?<![\d.,]){re.escape(value)}(?![\d.,])", message) is not None


def clickup_task_title(
    *,
    client_name: str | None,
    platform: str | None,
    message: str | None,
    observed_value: float | None,
) -> str:
    """"{ügyfél} - {platform} - {anomália leírása} ({aktuális érték})"

    Példa: `magicherb - meta - ROAS 4.20 a cél 6.00 alatt (−40% …)`

    Az "anomália leírása" a riasztás SAJÁT üzenete (`alerts.message`), szó
    szerint — ugyanaz a szöveg, amit a Discord üzenet is mutat. Itt semmi nem
    fogalmazódik újra.

    Az érték-utótag CSAK akkor kerül a végére, ha a formázott érték nincs BENNE
    az üzenetben. A detektor üzenetei jellemzően tartalmazzák a mért értéket
    ("ROAS 4.20 a cél …"), és a specifikált `({aktuális érték})` utótag ilyenkor
    szó szerint megismételné az utolsó számot — a cím ettől zajosabb lenne,
    nem informatívabb. Ahol az üzenet NEM mondja ki az értéket, ott viszont
    megjelenik, ahogy a követelmény kéri.
    """
    message_text = (message or "").strip()
    parts = [
        (client_name or "?").strip(),
        (platform or "?").strip().lower(),
        message_text,
    ]
    title = " - ".join(p for p in parts if p)

    value = format_number(observed_value)
    if value != _NO_VALUE and not _mentions_value(message_text, value):
        title = f"{title} ({value})"

    return _truncate(title, _TITLE_MAX_CHARS)


def clickup_task_description(
    *,
    campaign_name: str | None,
    platform: str | None,
    message: str | None,
    observed_value: float | None,
    threshold_value: float | None,
    detected_at_text: str,
    alert_id: Any = None,
    campaign_id: Any = None,
) -> str:
    """A ClickUp task leírása: kampány, cél vs aktuális érték, észlelés ideje.

    A záró `/campaign info …` sor szándékos: a taskról egy másolással vissza
    lehet ugrani a Discord-parancsra, ami a teljes kampány-képet megmutatja.

    A Discord üzenet linkje NEM itt kerül bele — azt a router fűzi hozzá,
    MIUTÁN az üzenet kiment és van message ID-nk (lásd `append_discord_link`).
    """
    lines = [
        f"Kampány: {campaign_name or '?'}",
        f"Platform: {platform or '?'}",
        f"Probléma: {message or ''}",
        f"Mért érték: {format_number(observed_value)} "
        f"(küszöb: {format_number(threshold_value)})",
        f"Észlelés: {detected_at_text}",
    ]
    if alert_id is not None:
        lines.append(f"Alert ID: {alert_id}")
    if campaign_id is not None:
        lines.append("")
        lines.append(f"/campaign info campaign_id:{campaign_id}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Kereszt-linkek (Discord ↔ ClickUp)
# ---------------------------------------------------------------------------

def clickup_link_line(task_url: str) -> str:
    """A ClickUp task sora a Discord üzenetben."""
    return f"📋 ClickUp: {task_url}"


def discord_link_line(message_url: str) -> str:
    """A Discord üzenet sora a ClickUp task leírásában."""
    return f"🔗 Discord: {message_url}"


def discord_jump_url(
    guild_id: Any,
    channel_id: Any,
    message_id: Any,
) -> str | None:
    """Ugrólink a kiküldött Discord üzenetre, vagy None ha bármelyik ID hiányzik.

    Formátum: `https://discord.com/channels/{guild}/{channel}/{message}`.
    None-t ad (és nem tákol össze fél linket), ha valamelyik azonosító hiányzik
    — egy törött link rosszabb, mint a link hiánya: kattintásra 404-et ad, és
    azt sugallja, hogy az üzenet eltűnt.
    """
    if not guild_id or not channel_id or not message_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
