"""
/alert slash parancscsoport — kampány-némítás kezelése.

Parancsok:
    /alert mute    campaign_id:<id> [hours:<2>]   — kampány némítása X órára
    /alert unmute  campaign_id:<id>               — némítás korai feloldása

A némítás a `mutes` táblán keresztül történik (src.storage.mutes). Muted
kampányra a detektor nem generál, az alert-router nem küld riasztást.
Minden Supabase hívás asyncio.to_thread()-ben fut (event loop védelme).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from src.storage import audit
from src.storage import campaigns as campaigns_storage
from src.storage import mutes as mutes_storage
from src.utils.logging import get_logger

log = get_logger(__name__)

_DEFAULT_MUTE_HOURS = 2
_MAX_MUTE_HOURS = 24 * 30  # 30 nap felső korlát (elgépelés-védelem)


class AlertsCog(commands.GroupCog, group_name="alert"):
    """Az `alert` parancscsoport (némítás)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /alert mute campaign_id:<id> hours:<2>
    # ------------------------------------------------------------------
    @app_commands.command(name="mute", description="Kampány némítása X órára (nem jelez riasztást)")
    @app_commands.describe(
        campaign_id="A kampány numerikus azonosítója",
        hours="Hány órára némítsd (alapértelmezés: 2)",
    )
    async def mute(
        self,
        interaction: discord.Interaction,
        campaign_id: int,
        hours: int = _DEFAULT_MUTE_HOURS,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if hours <= 0 or hours > _MAX_MUTE_HOURS:
            await interaction.followup.send(
                f"Az órák számának 1 és {_MAX_MUTE_HOURS} között kell lennie."
            )
            return

        c = await asyncio.to_thread(campaigns_storage.get_campaign, campaign_id)
        if c is None:
            await interaction.followup.send(f"Nincs ilyen kampány: #{campaign_id}")
            return

        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        try:
            mute_row = await asyncio.to_thread(
                mutes_storage.mute_campaign,
                campaign_id,
                until,
                reason=None,
                created_by_discord_user_id=str(interaction.user.id),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Hiba a némításkor (#%s)", campaign_id)
            await interaction.followup.send(f"Hiba: `{exc}`")
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "mute",
            entity_type="campaign",
            entity_id=campaign_id,
            details={
                "campaign_name": c["name"],
                "hours": hours,
                "muted_until": until.isoformat(),
                "mute_id": mute_row["id"],
            },
        )

        log.info("Kampány némítva: #%s %s (%d óra)", campaign_id, c["name"], hours)
        await interaction.followup.send(
            f"🔇 **#{campaign_id} — {c['name']}** némítva **{hours} órára**.\n"
            f"Eddig: `{until.strftime('%Y-%m-%d %H:%M')} UTC` — addig nem megy riasztás.\n"
            f"Korai feloldás: `/alert unmute campaign_id:{campaign_id}`"
        )

    # ------------------------------------------------------------------
    # /alert unmute campaign_id:<id>
    # ------------------------------------------------------------------
    @app_commands.command(name="unmute", description="Kampány némításának feloldása")
    @app_commands.describe(campaign_id="A kampány numerikus azonosítója")
    async def unmute(
        self,
        interaction: discord.Interaction,
        campaign_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        c = await asyncio.to_thread(campaigns_storage.get_campaign, campaign_id)
        if c is None:
            await interaction.followup.send(f"Nincs ilyen kampány: #{campaign_id}")
            return

        unmuted = await asyncio.to_thread(mutes_storage.unmute_campaign, campaign_id)
        if not unmuted:
            await interaction.followup.send(
                f"**#{campaign_id} — {c['name']}** nem volt némítva."
            )
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "unmute",
            entity_type="campaign",
            entity_id=campaign_id,
            details={"campaign_name": c["name"]},
        )

        log.info("Kampány némítása feloldva: #%s %s", campaign_id, c["name"])
        await interaction.followup.send(
            f"🔔 **#{campaign_id} — {c['name']}** némítása feloldva — újra figyeljük."
        )


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point."""
    await bot.add_cog(AlertsCog(bot))
