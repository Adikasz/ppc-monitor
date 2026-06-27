"""
/assign, /unassign, /my-clients slash parancsok.

Ezekkel a parancsokkal rendelhetők hozzá userek (felhasználók) KAMPÁNYOKHOZ
(campaign_id alapján), és kérdezhetők le a saját hozzárendeléseik.

Parancsok:
    /assign   campaign_id:<id> user:<@user> [role:primary|supporter]
                                            — OM hozzárendelése kampányhoz
    /unassign campaign_id:<id> user:<@user> — OM eltávolítása kampányról
    /my-clients                             — saját (ügyfél-szintű) hozzárendeléseim

Megjegyzések:
  - Az /assign és /unassign az admin csatornára van korlátozva.
  - A /my-clients bárhonnan hívható (ephemeral válasz).
  - Az /assign automatikusan létrehozza a users táblában az érintett
    személyt, ha még nem létezik (auto-registration).
  - A hozzárendelés a campaigns táblában keresett campaign_id-hez köt
    (assignments.campaign_id). A role 'primary' vagy 'supporter'.
  - Minden írási művelet auditálva van (audit_log tábla).
  - Minden Supabase hívás asyncio.to_thread()-ben fut, hogy ne blokkolja az event loop-ot.
"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from src.config import get_config
from src.storage import assignments as assignments_storage
from src.storage import campaigns as campaigns_storage
from src.storage import users as users_storage
from src.storage import audit
from src.utils.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Segédfüggvények (admin csatorna — ugyanaz a logika mint clients.py-ban)
# ---------------------------------------------------------------------------

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
    """True, ha az interakció az admin csatornában történt (vagy nincs konfigurálva)."""
    admin = _admin_channel_id()
    if admin is None:
        return True
    return interaction.channel_id == admin


def _display_name(member: discord.Member | discord.User) -> str:
    """Emberi olvasható név Discord member/user objektumból."""
    if isinstance(member, discord.Member) and member.nick:
        return member.nick
    return member.display_name or member.name


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class AssignmentsCog(commands.Cog):
    """Hozzárendelés parancsok — /assign, /unassign, /my-clients."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /assign campaign_id:<id> user:<@user> [role]
    # ------------------------------------------------------------------
    @app_commands.command(
        name="assign",
        description="Felhasználó hozzárendelése kampányhoz (admin csatorna)",
    )
    @app_commands.describe(
        campaign_id="A kampány numerikus azonosítója (/campaign list mutatja)",
        user="A hozzárendelendő Discord felhasználó",
        role="primary (elsődleges felelős) vagy supporter (helyettes) — alap: primary",
    )
    @app_commands.choices(
        role=[
            app_commands.Choice(name="primary — elsődleges felelős", value="primary"),
            app_commands.Choice(name="supporter — helyettes OM", value="supporter"),
        ]
    )
    async def assign(
        self,
        interaction: discord.Interaction,
        campaign_id: int,
        user: discord.Member,
        role: app_commands.Choice[str] | None = None,
    ) -> None:
        # ELSŐ sor: defer — azonnal jelzi Discordnak, hogy dolgozunk
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send(
                "Ez a parancs csak az admin csatornában használható."
            )
            return

        if user.bot:
            await interaction.followup.send(
                f"❌ {user.mention} egy bot — nem rendelhető OM-ként kampányhoz. "
                f"Válassz valódi felhasználót a `user:` mezőben."
            )
            return

        role_value = role.value if role else "primary"

        # 1) Kampány megkeresése — campaign_id alapján a campaigns táblában
        campaign = await asyncio.to_thread(campaigns_storage.get_campaign, campaign_id)
        if campaign is None:
            await interaction.followup.send(
                f"❌ Kampány nem található: **#{campaign_id}**\n"
                f"Ellenőrizd a `/campaign list` paranccsal az elérhető kampányokat."
            )
            return

        # 2) Felhasználó auto-regisztráció (users tábla)
        user_row, user_created = await asyncio.to_thread(
            users_storage.get_or_create_user,
            str(user.id),
            _display_name(user),
        )
        if user_created:
            log.info("Új felhasználó regisztrálva: %s (%s)", user, user.id)

        # 3) Hozzárendelés létrehozása (kampány-szintű)
        assignment, created = await asyncio.to_thread(
            assignments_storage.create_assignment,
            user_id=user_row["id"],
            campaign_id=campaign_id,
            role=role_value,
            created_by_discord_user_id=str(interaction.user.id),
        )

        if not created:
            await interaction.followup.send(
                f"**{_display_name(user)}** már hozzá van rendelve a "
                f"**#{campaign_id} — {campaign['name']}** kampányhoz (`{role_value}`)."
            )
            return

        # 4) Audit log
        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "assign",
            entity_type="assignment",
            entity_id=assignment["id"],
            details={
                "campaign_id": campaign_id,
                "campaign_name": campaign["name"],
                "user_discord_id": str(user.id),
                "user_name": _display_name(user),
                "role": role_value,
            },
        )

        log.info(
            "Hozzárendelve: %s → kampány #%s (%s, assignment #%s)",
            user, campaign_id, role_value, assignment["id"],
        )
        await interaction.followup.send(
            f"✅ **{_display_name(user)}** hozzárendelve a "
            f"**#{campaign_id} — {campaign['name']}** kampányhoz (`{role_value}`).\n"
            f"*(assignment #{assignment['id']})*"
        )

    # ------------------------------------------------------------------
    # /unassign campaign_id:<id> user:<@user>
    # ------------------------------------------------------------------
    @app_commands.command(
        name="unassign",
        description="Felhasználó eltávolítása kampányról (admin csatorna)",
    )
    @app_commands.describe(
        campaign_id="A kampány numerikus azonosítója",
        user="Az eltávolítandó Discord felhasználó",
    )
    async def unassign(
        self,
        interaction: discord.Interaction,
        campaign_id: int,
        user: discord.Member,
    ) -> None:
        # ELSŐ sor: defer — azonnal jelzi Discordnak, hogy dolgozunk
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send(
                "Ez a parancs csak az admin csatornában használható."
            )
            return

        # 1) Kampány megkeresése
        campaign = await asyncio.to_thread(campaigns_storage.get_campaign, campaign_id)
        if campaign is None:
            await interaction.followup.send(
                f"❌ Kampány nem található: **#{campaign_id}**"
            )
            return

        # 2) Felhasználó DB azonosítója
        user_row = await asyncio.to_thread(
            users_storage.get_user_by_discord_id, str(user.id)
        )
        if user_row is None:
            await interaction.followup.send(
                f"**{_display_name(user)}** nincs a rendszerben — "
                f"nem volt hozzárendelve ehhez a kampányhoz."
            )
            return

        # 3) Hozzárendelés törlése (kampány-szintű)
        deleted = await asyncio.to_thread(
            assignments_storage.delete_assignment,
            user_id=user_row["id"],
            campaign_id=campaign_id,
        )

        if not deleted:
            await interaction.followup.send(
                f"**{_display_name(user)}** nem volt hozzárendelve "
                f"a **#{campaign_id} — {campaign['name']}** kampányhoz."
            )
            return

        # 4) Audit log
        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "unassign",
            entity_type="assignment",
            details={
                "campaign_id": campaign_id,
                "campaign_name": campaign["name"],
                "user_discord_id": str(user.id),
                "user_name": _display_name(user),
            },
        )

        log.info(
            "Hozzárendelés törölve: %s ← kampány #%s",
            user, campaign_id,
        )
        await interaction.followup.send(
            f"✅ **{_display_name(user)}** eltávolítva a "
            f"**#{campaign_id} — {campaign['name']}** kampányról."
        )

    # ------------------------------------------------------------------
    # /my-clients — saját ügyfeleim
    # ------------------------------------------------------------------
    @app_commands.command(
        name="my-clients",
        description="A saját hozzárendelt ügyfeleim listája",
    )
    async def my_clients(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        # Felhasználó keresése (ha még nincs a rendszerben → üres lista)
        user_row = await asyncio.to_thread(
            users_storage.get_user_by_discord_id, str(interaction.user.id)
        )
        if user_row is None:
            await interaction.followup.send(
                "Még nincs hozzárendelésed. Kérj meg egy kollégát, "
                "hogy rendelje hozzád a megfelelő ügyfeleket az `/assign` paranccsal."
            )
            return

        rows = await asyncio.to_thread(
            assignments_storage.get_clients_for_user, user_row["id"]
        )

        if not rows:
            await interaction.followup.send(
                "Nincs hozzárendelt ügyfeled.\n"
                "Kérj meg egy kollégát, hogy rendelje hozzád őket az `/assign` paranccsal."
            )
            return

        embed = discord.Embed(
            title=f"📋 {_display_name(interaction.user)} ügyfelei",
            color=discord.Color.green(),
        )
        for row in rows:
            client_data = row.get("clients") or {}
            client_name = client_data.get("name", f"#{row.get('client_id', '?')}")
            channel_id = client_data.get("discord_channel_id")
            channel_str = f"<#{channel_id}>" if channel_id else "—"
            active_str = "✅ aktív" if client_data.get("is_active", True) else "⏸ inaktív"
            embed.add_field(
                name=client_name,
                value=f"{active_str} · csatorna: {channel_str}",
                inline=False,
            )

        embed.set_footer(text=f"Összesen: {len(rows)} ügyfél")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(AssignmentsCog(bot))
