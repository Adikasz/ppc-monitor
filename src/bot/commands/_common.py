"""
Közös segédek a parancsokhoz (`/insight scan-now`, `/report weekly-now`, `/my …`).

Ezek a függvények szándékosan NEM tartalmaznak üzleti logikát — csak azt a
Discord-plumbingot, ami több parancsnál ugyanaz:

    - admin csatorna ellenőrzés
    - a csatorna OM-jének feloldása, ütközés-kezeléssel
    - ügyfél feloldása név VAGY numerikus ID alapján
    - válaszküldés, ami túléli a 15 perces interakciós token lejáratát

Miért közös modul: a második admin parancsnál ezek egy az egyben másolódtak
volna. A projektben pontosan a másolt implementáció okozta a leghosszabb néma
hibát (az insight scan duplikált verziója), ezért itt inkább egy közös,
tesztelhető helyre kerültek. Az ÜZLETI logika (mit csinál a scan / a riport)
továbbra sem duplikálódik: azt mindkét parancs a scheduler moduljából hívja.
"""
from __future__ import annotations

import logging

import discord

from src.config import get_config
from src.storage import clients as clients_storage
from src.storage import users as users_storage
from src.utils.logging import get_logger

log = get_logger(__name__)


def admin_channel_id() -> int | None:
    """A konfigurált admin csatorna ID-ja, vagy None ha nincs beállítva/hibás."""
    raw = get_config().discord_admin_channel_id
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("DISCORD_ADMIN_CHANNEL_ID nem szám: %r — auth check kikapcsol", raw)
        return None


def is_admin_channel(interaction: discord.Interaction) -> bool:
    """True, ha az interakció az admin csatornában történt (vagy nincs konfigurálva)."""
    admin = admin_channel_id()
    if admin is None:
        return True
    return interaction.channel_id == admin


AMBIGUOUS_OWNER_MESSAGE = (
    "❌ Ez a csatorna több felhasználóhoz is hozzá van rendelve — szólj adminnak.\n"
    "Amíg ez fennáll, a csatorna-szkópolt parancsok (`/my …`, `/account kpi`, "
    "`/account summary-now`) itt nem használhatók, mert nem eldönthető, "
    "kinek a hatókörében futnának."
)


def is_ambiguous_owner(owner: dict | None) -> bool:
    """True, ha a csatorna-feloldás TÖBB usert talált (nem egy konkrét OM).

    A `users_storage.get_user_by_alerts_channel` ilyenkor
    `{"ambiguous": True, "matches": [...]}`-t ad vissza a user sor helyett —
    lásd az ottani docstringet arról, miért nem választ önkényesen.
    """
    return bool(owner) and bool(owner.get("ambiguous"))


def ambiguous_owner_message(owner: dict | None) -> str:
    """Az ütközés emberi üzenete, az érintett userek megnevezésével.

    Az admin így a Discord-válaszból megtudja, KIT kell átállítania — nem kell
    a Railway logot bogarásznia (oda a `get_user_by_alerts_channel` warningja megy).
    """
    nevek = ", ".join(
        f"**{m.get('display_name') or '?'}** (#{m.get('id')})"
        for m in (owner or {}).get("matches") or []
    )
    if not nevek:
        return AMBIGUOUS_OWNER_MESSAGE
    return (
        f"{AMBIGUOUS_OWNER_MESSAGE}\n"
        f"Érintettek: {nevek} — egyiküknél állíts be másik csatornát a "
        f"`/user set-channel` paranccsal."
    )


def unique_channel_owner(channel_id: object) -> dict | None:
    """A csatorna EGYÉRTELMŰ OM-je; ütközésnél és hiánynál egyaránt None.

    Az autocomplete-ek használják: ott nem lehet hibaüzenetet küldeni (a Discord
    csak választék-listát vár), ezért az ütközés néma üres listát eredményez —
    a felhasználó a parancs futtatásakor kapja meg a magyarázatot.
    """
    owner = users_storage.get_user_by_alerts_channel(str(channel_id))
    return None if is_ambiguous_owner(owner) else owner


def resolve_client(value: str) -> dict | None:
    """Ügyfél feloldása név VAGY numerikus ID alapján (ugyanaz, mint a /client-nél).

    Ha a bemenet csak számjegy, előbb ID-ként próbáljuk; ha nincs ilyen ID,
    névként is megkíséreljük.
    """
    val = (value or "").strip()
    if not val:
        return None
    if val.isdigit():
        row = clients_storage.get_client(int(val))
        if row is not None:
            return row
    return clients_storage.get_client_by_name(val)


async def reply_or_channel(
    interaction: discord.Interaction,
    content: str,
    *,
    logger: logging.Logger,
    what: str,
) -> None:
    """Válasz a followupon; ha az interakciós token lejárt, a csatornába.

    Egy teljes scan/riport túlfuthat a Discord 15 perces interakciós ablakán —
    ilyenkor a `followup.send` 401/404-gyel elszáll, és az admin semmit nem
    látna a több perces várakozás után.
    """
    try:
        await interaction.followup.send(content)
        return
    except discord.HTTPException as exc:
        logger.warning(
            "%s válasz: a followup elszállt (%s) — csatornába küldjük", what, exc,
        )

    channel = interaction.channel
    if channel is None:
        logger.error("%s válasz: nincs csatorna a fallbackhez — elveszett", what)
        return
    try:
        await channel.send(f"<@{interaction.user.id}>\n{content}")
    except Exception:  # noqa: BLE001
        logger.exception("%s válasz: a csatornába küldés is elszállt", what)
