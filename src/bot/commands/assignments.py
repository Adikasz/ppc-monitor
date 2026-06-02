"""
/assign, /unassign, /my-clients slash parancsok.

Ezekkel a parancsokkal rendelhetők hozzá account managerek ügyfelekhez,
és kérdezhetők le a saját ügyfeleik.

Parancsok:
    /assign   client:<név> manager:<@user>  — OM hozzárendelése ügyfélhez
    /unassign client:<név> manager:<@user>  — OM eltávolítása ügyfélről
    /my-clients                             — saját ügyfeleim listája

Megjegyzések:
  - Az /assign és /unassign az admin csatornára van korlátozva.
  - A /my-clients bárhonnan hívható (ephemeral válasz).
  - Az /assign automatikusan létrehozza a users táblában az érintett
    személyt, ha még nem létezik (auto-registration).
  - Minden írási művelet auditálva van (audit_log tábla).
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from src.config import get_config
from src.storage import assignments as assignments_storage
from src.storage import clients as clients_storage
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
    # /assign client:<név> manager:<@user>
    # ------------------------------------------------------------------
    @app_commands.command(
        name="assign",
        description="Account manager hozzárendelése ügyfélhez (admin csatorna)",
    )
    @app_commands.describe(
        client_name="Az ügyfél neve (pontosan, ahogy a /clients list mutatja)",
        manager="A hozzárendelendő Discord felhasználó",
    )
    async def assign(
        self,
        interaction: discord.Interaction,
        client_name: str,
        manager: discord.Member,
    ) -> None:
        if not _is_admin_channel(interaction):
            await interaction.response.send_message(
                "Ez a parancs csak az admin csatornában használható.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        # 1) Ügyfél megkeresése
        client = clients_storage.get_client_by_name(client_name.strip())
        if client is None:
            await interaction.followup.send(
                f"Nem található ügyfél ezzel a névvel: **{client_name}**\n"
                f"Ellenőrizd a `/clients list` paranccsal az elérhető ügyfeleket."
            )
            return

        if not client.get("is_active", True):
            await interaction.followup.send(
                f"Az ügyfél **{client_name}** inaktív, nem rendelhető hozzá manager."
            )
            return

        # 2) Manager auto-regisztráció (users tábla)
        user_row, user_created = users_storage.get_or_create_user(
            discord_user_id=str(manager.id),
            display_name=_display_name(manager),
        )
        if user_created:
            log.info("Új felhasználó regisztrálva: %s (%s)", manager, manager.id)

        # 3) Hozzárendelés létrehozása
        assignment, created = assignments_storage.create_assignment(
            user_id=user_row["id"],
            client_id=client["id"],
            created_by_discord_user_id=str(interaction.user.id),
        )

        if not created:
            await interaction.followup.send(
                f"**{_display_name(manager)}** már hozzá van rendelve "
                f"a **{client['name']}** ügyfélhez."
            )
            return

        # 4) Audit log
        audit.log_action(
            discord_user_id=str(interaction.user.id),
            action="assign",
            entity_type="assignment",
            entity_id=assignment["id"],
            details={
                "client_id": client["id"],
                "client_name": client["name"],
                "manager_discord_id": str(manager.id),
                "manager_name": _display_name(manager),
            },
        )

        log.info(
            "Hozzárendelve: %s → ügyfél %s (assignment #%s)",
            manager, client["name"], assignment["id"],
        )
        await interaction.followup.send(
            f"✅ **{_display_name(manager)}** hozzárendelve a **{client['name']}** ügyfélhez.\n"
            f"*(assignment #{assignment['id']})*"
        )

    # ------------------------------------------------------------------
    # /unassign client:<név> manager:<@user>
    # ------------------------------------------------------------------
    @app_commands.command(
        name="unassign",
        description="Account manager eltávolítása ügyfélről (admin csatorna)",
    )
    @app_commands.describe(
        client_name="Az ügyfél neve",
        manager="Az eltávolítandó Discord felhasználó",
    )
    async def unassign(
        self,
        interaction: discord.Interaction,
        client_name: str,
        manager: discord.Member,
    ) -> None:
        if not _is_admin_channel(interaction):
            await interaction.response.send_message(
                "Ez a parancs csak az admin csatornában használható.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        # 1) Ügyfél megkeresése
        client = clients_storage.get_client_by_name(client_name.strip())
        if client is None:
            await interaction.followup.send(
                f"Nem található ügyfél: **{client_name}**"
            )
            return

        # 2) Felhasználó DB azonosítója
        user_row = users_storage.get_user_by_discord_id(str(manager.id))
        if user_row is None:
            await interaction.followup.send(
                f"**{_display_name(manager)}** nincs a rendszerben — "
                f"nem volt hozzárendelve ehhez az ügyfélhez."
            )
            return

        # 3) Hozzárendelés törlése
        deleted = assignments_storage.delete_assignment(
            user_id=user_row["id"],
            client_id=client["id"],
        )

        if not deleted:
            await interaction.followup.send(
                f"**{_display_name(manager)}** nem volt hozzárendelve "
                f"a **{client['name']}** ügyfélhez."
            )
            return

        # 4) Audit log
        audit.log_action(
            discord_user_id=str(interaction.user.id),
            action="unassign",
            entity_type="assignment",
            details={
                "client_id": client["id"],
                "client_name": client["name"],
                "manager_discord_id": str(manager.id),
                "manager_name": _display_name(manager),
            },
        )

        log.info(
            "Hozzárendelés törölve: %s ← ügyfél %s",
            manager, client["name"],
        )
        await interaction.followup.send(
            f"✅ **{_display_name(manager)}** eltávolítva a **{client['name']}** ügyfélről."
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
        user_row = users_storage.get_user_by_discord_id(str(interaction.user.id))
        if user_row is None:
            await interaction.followup.send(
                "Még nincs hozzárendelésed. Kérj meg egy kollégát, "
                "hogy rendelje hozzád a megfelelő ügyfeleket az `/assign` paranccsal."
            )
            return

        rows = assignments_storage.get_clients_for_user(user_row["id"])

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
