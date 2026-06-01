"""
Discord bot belépési pont.

Felelőssége ebben a lépésben minimális: betölti a konfigurációt, létrehoz
egy `commands.Bot` példányt, és bejelentkezik a Discord API-ra. A parancsok,
ütemezett feladatok és integrációk a következő lépésekben jönnek.

Indítás:
    python -m src.bot.main
"""
from __future__ import annotations

import discord
from discord.ext import commands

from src.config import get_config
from src.utils.logging import get_logger

log = get_logger(__name__)


def build_bot() -> commands.Bot:
    # A `default()` intents elegendő a bejelentkezéshez és slash parancsokhoz;
    # a message_content intentre csak prefix-alapú szöveges parancsoknál van
    # szükség, azt majd a megfelelő lépésben kapcsoljuk be.
    intents = discord.Intents.default()

    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready() -> None:
        guilds = ", ".join(f"{g.name} ({g.id})" for g in bot.guilds) or "—"
        log.info("Bejelentkezve mint %s (id=%s)", bot.user, bot.user.id if bot.user else "?")
        log.info("Csatlakozott szerverek: %s", guilds)

    return bot


def main() -> None:
    config = get_config()
    bot = build_bot()
    log.info("Discord bot indítása…")
    bot.run(config.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
