"""
/user slash parancscsoport — személyes alert csatorna kezelése.

A 11. lépéssel minden OM a SAJÁT csatornájába kapja a hozzárendelt kampányai
riasztásait (lásd src/monitoring/router.py). Ezekkel a parancsokkal állítható be
és kérdezhető le, melyik csatorna kihez tartozik.

Parancsok:
    /user set-channel channel:<csatorna>  — a saját alert csatornám beállítása
    /user info                            — saját adataim (név, csatorna, assignmentek)
    /user list                            — összes OM + alert csatorna státusz

Megjegyzések:
  - A /user set-channel bárhol futtatható (admin csatorna VAGY DM is) — mindenki
    a saját csatornáját állítja, ezért nincs admin-korlátozás.
  - A set-channel automatikusan létrehozza a felhasználót a users táblában,
    ha még nem létezik (auto-registration, mint az /assign-nél).
  - Minden Supabase hívás asyncio.to_thread()-ben fut, hogy ne blokkolja az event loop-ot.
"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from src.storage import audit
from src.storage import users as users_storage
from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

log = get_logger(__name__)


def _display_name(member: discord.Member | discord.User) -> str:
    """Emberi olvasható név Discord member/user objektumból."""
    if isinstance(member, discord.Member) and member.nick:
        return member.nick
    return member.display_name or member.name


def _count_assignments(user_id: int) -> int:
    """Egy felhasználóhoz tartozó hozzárendelések száma (ügyfél + kampány)."""
    res = (
        get_supabase()
        .table("assignments")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .execute()
    )
    if res.count is not None:
        return res.count
    return len(res.data or [])


class UsersCog(commands.GroupCog, group_name="user"):
    """Felhasználói parancsok — /user set-channel, /user info, /user list."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /user set-channel channel:<csatorna>
    # ------------------------------------------------------------------
    @app_commands.command(
        name="set-channel",
        description="A saját személyes alert csatornám beállítása",
    )
    @app_commands.describe(channel="A csatorna, ahova a hozzárendelt kampányaid alertjeit kéred")
    async def set_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # Auto-regisztráció: ha a user még nincs a rendszerben, létrehozzuk.
        user_row, created = await asyncio.to_thread(
            users_storage.get_or_create_user,
            str(interaction.user.id),
            _display_name(interaction.user),
        )
        if created:
            log.info("Új felhasználó regisztrálva (set-channel): %s (%s)",
                     interaction.user, interaction.user.id)

        updated = await asyncio.to_thread(
            users_storage.set_user_alerts_channel,
            interaction.user.id,
            str(channel.id),
        )
        if not updated:
            await interaction.followup.send(
                "❌ Nem sikerült beállítani az alert csatornát — próbáld újra."
            )
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "set_alerts_channel",
            entity_type="user",
            entity_id=user_row["id"],
            details={"channel_id": str(channel.id)},
        )

        log.info("Alert csatorna beállítva: %s → %s", interaction.user, channel.id)
        await interaction.followup.send(
            f"✅ Alert csatornád beállítva: <#{channel.id}>\n"
            f"Mostantól a hozzád rendelt kampányok riasztásai ide érkeznek."
        )

    # ------------------------------------------------------------------
    # /user info
    # ------------------------------------------------------------------
    @app_commands.command(name="info", description="Saját adataim (név, alert csatorna, assignmentek)")
    async def info(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        user_row = await asyncio.to_thread(
            users_storage.get_user_by_discord_id, str(interaction.user.id)
        )
        if user_row is None:
            await interaction.followup.send(
                "Még nem vagy a rendszerben. Állítsd be az alert csatornádat a "
                "`/user set-channel` paranccsal, vagy kérj egy hozzárendelést `/assign`-nal."
            )
            return

        count = await asyncio.to_thread(_count_assignments, user_row["id"])
        channel_id = user_row.get("alerts_channel_id")
        channel_str = f"<#{channel_id}>" if channel_id else "❌ nincs beállítva"

        embed = discord.Embed(
            title=f"👤 {user_row.get('display_name') or _display_name(interaction.user)}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Alert csatorna", value=channel_str, inline=False)
        embed.add_field(name="Hozzárendelések", value=str(count), inline=False)
        if not channel_id:
            embed.set_footer(text="Állítsd be: /user set-channel")
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # /user list
    # ------------------------------------------------------------------
    @app_commands.command(name="list", description="Összes OM és alert csatorna státusz")
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        # Közvetlenül a Supabase users táblát listázzuk (NINCS Discord guild
        # member lookup / @mention feloldás). active_only=False: az inaktív
        # usereket is mutatjuk (⏸ jelzéssel), hogy SENKI ne maradjon ki némán.
        rows = await asyncio.to_thread(users_storage.list_users, active_only=False)
        if not rows:
            await interaction.followup.send("Még nincs egyetlen felhasználó sem a rendszerben.")
            return

        lines: list[str] = []
        for row in rows:
            uid = row.get("id", "?")
            name = row.get("display_name") or f"#{uid}"
            inactive = "" if row.get("is_active", True) else " ⏸ *(inaktív)*"
            channel_id = row.get("alerts_channel_id")
            if channel_id:
                lines.append(f"- **{name}** (#{uid}) ✅ <#{channel_id}>{inactive}")
            else:
                lines.append(f"- **{name}** (#{uid}) ❌ (nincs beállítva){inactive}")

        embed = discord.Embed(
            title="👥 Felhasználók — alert csatornák",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Összesen: {len(rows)} felhasználó")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(UsersCog(bot))
