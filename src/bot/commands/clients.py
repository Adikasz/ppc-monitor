"""
/clients slash parancscsoport.

Három parancsot ad ebben a lépésben:
    /clients list              — aktív ügyfelek felsorolása
    /clients info <client_id>  — egy ügyfél részletei
    /clients add  <name>       — új ügyfél (csak az admin csatornában)

Az írási műveletek (add) az admin csatornára vannak korlátozva
(`DISCORD_ADMIN_CHANNEL_ID`). Ha az env nincs beállítva, a korlátozás kikapcsol
— ez fejlesztési placeholder, élesben kötelező lesz a beállítása.

Megjegyzés:
  - Minden Supabase hívás asyncio.to_thread()-ben fut, hogy ne blokkolja az event loop-ot.
"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from src.config import get_config
from src.storage import clients as clients_storage
from src.utils.logging import get_logger

log = get_logger(__name__)


def _admin_channel_id() -> int | None:
    raw = get_config().discord_admin_channel_id
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("DISCORD_ADMIN_CHANNEL_ID nem szám: %r — auth check kikapcsol", raw)
        return None


def _is_admin_channel(interaction: discord.Interaction) -> bool:
    """True, ha az interakció az admin csatornában történt (vagy ha nincs konfigurálva)."""
    admin = _admin_channel_id()
    if admin is None:
        return True
    return interaction.channel_id == admin


class ClientsCog(commands.GroupCog, group_name="clients"):
    """A `clients` parancscsoport implementációja."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="list", description="Aktív ügyfelek listázása")
    async def list_(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        rows = await asyncio.to_thread(clients_storage.list_clients)
        if not rows:
            await interaction.followup.send("Nincs aktív ügyfél.")
            return

        embed = discord.Embed(title="Ügyfelek", color=discord.Color.blue())
        for c in rows:
            embed.add_field(
                name=f"#{c['id']} — {c['name']}",
                value=c.get("notes") or "—",
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="info", description="Egy ügyfél részletei")
    @app_commands.describe(client_id="Az ügyfél numerikus azonosítója")
    async def info(self, interaction: discord.Interaction, client_id: int) -> None:
        await interaction.response.defer(ephemeral=True)

        c = await asyncio.to_thread(clients_storage.get_client, client_id)
        if c is None:
            await interaction.followup.send(f"Nincs ilyen ügyfél: #{client_id}")
            return

        embed = discord.Embed(
            title=f"#{c['id']} — {c['name']}",
            color=discord.Color.blue() if c["is_active"] else discord.Color.dark_grey(),
        )
        embed.add_field(name="Aktív", value="igen" if c["is_active"] else "nem")
        embed.add_field(
            name="Discord csatorna",
            value=f"<#{c['discord_channel_id']}>" if c.get("discord_channel_id") else "—",
        )
        embed.add_field(name="Megjegyzés", value=c.get("notes") or "—", inline=False)
        embed.add_field(name="Létrehozva", value=str(c.get("created_at", "—")), inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="add",
        description="Új ügyfél létrehozása (csak az admin csatornában)",
    )
    @app_commands.describe(name="Az ügyfél neve (egyedi)")
    async def add(self, interaction: discord.Interaction, name: str) -> None:
        # ELSŐ sor: defer — azonnal jelzi Discordnak, hogy dolgozunk
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send(
                "Ez a parancs csak az admin csatornában használható."
            )
            return

        name = name.strip()
        if not name:
            await interaction.followup.send("A név nem lehet üres.")
            return

        existing = await asyncio.to_thread(clients_storage.get_client_by_name, name)
        if existing:
            await interaction.followup.send(
                f"Már létezik ügyfél ezzel a névvel: **{name}**"
            )
            return

        try:
            row = await asyncio.to_thread(clients_storage.create_client, name)
        except Exception as exc:  # noqa: BLE001 — felhasználói visszajelzéshez kell
            log.exception("Hiba az ügyfél létrehozásakor (%s)", name)
            await interaction.followup.send(f"Hiba a létrehozáskor: `{exc}`")
            return

        await interaction.followup.send(
            f"✓ Létrehozva: **#{row['id']} — {row['name']}**"
        )


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(ClientsCog(bot))
