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

import os
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from src.config import get_config
from src.integrations.discord_router import set_client
from src.monitoring.scheduler import start_scheduler
from src.utils.logging import get_logger

log = get_logger(__name__)


# Cog-ok, amiket a bot induláskor betölt. Új parancsmodulokat ide kell felvenni.
_EXTENSIONS: tuple[str, ...] = (
    "src.bot.commands.clients",
    "src.bot.commands.adaccounts",
    "src.bot.commands.discovery",
    "src.bot.commands.assignments",
    "src.bot.commands.campaigns",
    "src.bot.commands.alerts",
    "src.bot.commands.insights",
    "src.bot.commands.reports",
    "src.bot.commands.users",
    "src.bot.commands.my_commands",
)


def describe_synced_commands(synced: list) -> list[str]:
    """A `tree.sync()` eredményéből (list[AppCommand]) ember-olvasható sorok.

    Deploy-verifikációhoz: Railway logban ez mutatja PONTOSAN milyen mezőkkel
    regisztrálódott minden parancs a legutóbbi induláskor — anélkül, hogy
    kézzel kellene a Discord API-t hívni. Ha egy várt mező-változás
    (pl. `/account add` `client:` eltávolítása) nem látszik itt, az azt
    jelenti, hogy EZ a `setup_hook` nem a várt kód-állapottal futott le
    (lásd `main()` "commit=" log sora — hasonlítsd össze `git rev-parse HEAD`-del).
    """
    lines: list[str] = []
    for cmd in synced:
        subgroups = [o for o in cmd.options if isinstance(o, app_commands.AppCommandGroup)]
        if subgroups:
            for sub in subgroups:
                opt_names = [o.name for o in sub.options]
                lines.append(f"  /{cmd.name} {sub.name} -> mezők: {opt_names}")
        else:
            opt_names = [o.name for o in cmd.options]
            lines.append(f"  /{cmd.name} -> mezők: {opt_names}")
    return lines


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
        #
        # A sync() egy TELJES bulk-overwrite Discord oldalon (nem inkrementális
        # diff) — minden induláskor a MOST betöltött cog-kód alapján épül fel a
        # parancsfa, a korábbi (guild-szintű) regisztráció teljesen felülíródik.
        # Ha egy régi mező (pl. `/account add` `client:`) a redeploy UTÁN is
        # látszik Discordban, az nem itt a hiba forrása — azt jelenti, hogy EZ
        # a kód (a mostani setup_hook) NEM futott le a jelenlegi commit-tal.
        # Ellenőrzés: a lenti log sorban a parancsonkénti mező-lista, PLUSZ a
        # fenti "Discord bot indítása… (commit=...)" sor (main()) — hasonlítsd
        # össze `git rev-parse HEAD`-del. Ha eltér, Railway-en nem történt
        # valódi rebuild (Restart ≠ Redeploy — lásd korábbi incidens).
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

        for line in describe_synced_commands(synced):
            log.info(line)


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

        # Az alert-router Discord kliensének bekötése (riasztás-kiküldéshez).
        set_client(bot)

        # Monitoring scheduler indítása (óránkénti anomália-ciklus).
        # on_ready reconnectkor újra lefuthat — a start_scheduler idempotens.
        try:
            start_scheduler()
        except Exception:
            log.exception("Monitoring scheduler indítása sikertelen")

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Globális app command hibakezelő.

        Ha egy slash command exception-t dob és nem kezeli le magában,
        ez biztosítja, hogy:
          1. A hiba bekerül a logba (traceback-kel együtt)
          2. A felhasználó kap visszajelzést (nem marad örök "thinking...")
        """
        # Teljes traceback a logba
        tb = traceback.format_exception(type(error), error, error.__traceback__)
        log.error(
            "App command hiba [/%s]: %s\n%s",
            interaction.command.name if interaction.command else "?",
            error,
            "".join(tb),
        )

        error_msg = f"❌ Váratlan hiba: `{error}`\nNézd meg a bot logját a részletekért."

        try:
            if interaction.response.is_done():
                # Már defer()-elve volt → followup
                await interaction.followup.send(error_msg, ephemeral=True)
            else:
                # Még nem válaszolt → response
                await interaction.response.send_message(error_msg, ephemeral=True)
        except Exception:
            log.exception("Nem sikerült az error üzenetet elküldeni az interakcióhoz")

    return bot


def main() -> None:
    config = get_config()
    bot = build_bot()
    commit = os.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:7] or "?"
    log.info("Discord bot indítása… (commit=%s)", commit)
    bot.run(config.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
