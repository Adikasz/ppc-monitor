"""
Discord bot belépési pont.

Felelős:
  - betölti a konfigurációt és a logolást
  - létrehoz egy `commands.Bot` példányt
  - a `setup_hook`-ban betölti a cog-okat és szinkronizálja a slash parancsokat
    a fejlesztői guild-re (gyors deploy; globálisan akár 1 óra is lehet)
  - bejelentkezik a Discord API-ra

Indítás:
    python -m src.bot.main
"""
from __future__ import annotations

import discord
from discord.ext import commands

from src.config import get_config
from src.utils.logging import get_logger

log = get_logger(__name__)


# Cog-ok, amiket a bot induláskor betölt. Új parancsmodulokat ide kell felvenni.
_EXTENSIONS: tuple[str, ...] = (
    "src.bot.commands.clients",
)


class PpcBot(commands.Bot):
    """A projekt bot-osztálya. A cog-betöltés és slash-szinkron itt történik,
    hogy a `commands.Bot` event loopjához igazítva, biztonságosan fusson."""

    async def setup_hook(self) -> None:
        # 1) Cog-ok betöltése
        for ext in _EXTENSIONS:
            await self.load_extension(ext)
            log.info("Cog betöltve: %s", ext)

        # 2) Slash parancsok szinkronizálása. Guild-szintű sync azonnal él,
        # globális sync akár 1 órát is várhat. Fejlesztésben mindig guildre.
        guild_id_raw = get_config().discord_guild_id
        if guild_id_raw:
            guild = discord.Object(id=int(guild_id_raw))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info(
                "Slash parancsok szinkronizálva guild=%s (%d parancs)",
                guild_id_raw, len(synced),
            )
        else:
            synced = await self.tree.sync()
            log.info("Slash parancsok globálisan szinkronizálva (%d)", len(synced))


def build_bot() -> commands.Bot:
    # A `default()` intents elegendő a bejelentkezéshez és slash parancsokhoz;
    # a message_content intentre csak prefix-alapú szöveges parancsoknál van
    # szükség, azt majd a megfelelő lépésben kapcsoljuk be.
    intents = discord.Intents.default()

    bot = PpcBot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready() -> None:
        guilds = ", ".join(f"{g.name} ({g.id})" for g in bot.guilds) or "—"
        log.info(
            "Bejelentkezve mint %s (id=%s)",
            bot.user, bot.user.id if bot.user else "?",
        )
        log.info("Csatlakozott szerverek: %s", guilds)

    return bot


def main() -> None:
    config = get_config()
    bot = build_bot()
    log.info("Discord bot indítása…")
    bot.run(config.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
