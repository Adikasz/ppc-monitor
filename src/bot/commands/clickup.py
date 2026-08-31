"""
/clickup slash parancscsoport — a ClickUp anomália-struktúra beállítása.

Parancsok (mind admin csatorna):
    /clickup setup                                       — a Space létrehozása/megkeresése
    /clickup setup-manager user:@X clickup_user_id:Y     — OM Folder + List + assignee
    /clickup list-members                                — a Workspace tagjai + ClickUp ID-juk
    /clickup status                                      — mi van beállítva (Space + mappingek)

MIÉRT PARANCS ÉS NEM ENV VÁLTOZÓ:
    A ClickUp Space/Folder/List azonosítók csak akkor léteznek, amikor a
    rendszer LÉTREHOZTA őket — előre nem lehet őket .env-be írni. Ezért a setup
    az API-n keresztül hozza létre a struktúrát, és az ID-kat DB-be menti
    (0014 migration). Új PPC manager felvétele innentől egyetlen parancs:
    nincs kód-módosítás, nincs Railway redeploy.

IDEMPOTENS:
    Mindkét setup parancs NÉV SZERINT KERES, és csak akkor hoz létre újat, ha
    nincs találat. Kétszer lefuttatva ugyanazt az eredményt adja (a válasz
    megmondja, most készült-e vagy már megvolt).

A parancsok SZÁNDÉKOSAN hangosak a hibákra: a `clickup_admin` modul kivételt
dob emberi üzenettel, amit itt egy az egyben kiírunk. Egy néma "nem sikerült"
válasz ugyanaz a hibaosztály lenne, ami az insight scan-nél hetekig rejtve
maradt.
"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from src.bot.commands._common import is_admin_channel as _is_admin_channel
from src.integrations import clickup_admin
from src.integrations.clickup_admin import ClickUpAdminError
from src.storage import audit
from src.storage import clickup_structure as clickup_storage
from src.storage import users as users_storage
from src.utils.logging import get_logger

log = get_logger(__name__)

# A Discord üzenet 2000 karakteres — hosszú listáknál vágunk, de NEM némán.
_MAX_LISTED = 25

_ADMIN_ONLY = "Ez a parancs csak az admin csatornában használható."


def _display_name(member: discord.Member | discord.User) -> str:
    """Emberi olvasható név Discord member/user objektumból (mint az /assign-nél)."""
    if isinstance(member, discord.Member) and member.nick:
        return member.nick
    return member.display_name or member.name


class ClickUpCog(commands.GroupCog, group_name="clickup"):
    """A `clickup` parancscsoport — struktúra-setup és OM-mapping."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /clickup setup
    # ------------------------------------------------------------------
    @app_commands.command(
        name="setup",
        description="A ClickUp anomália-Space létrehozása vagy megkeresése (admin)",
    )
    async def setup_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not _is_admin_channel(interaction):
            await interaction.followup.send(_ADMIN_ONLY)
            return

        problem = clickup_admin.config_error()
        if problem:
            await interaction.followup.send(
                f"❌ A ClickUp beállítás hiányos — {problem}.\n"
                f"*Állítsd be a Railway environment variables között, majd futtasd újra.*"
            )
            return

        try:
            space = await clickup_admin.ensure_space()
        except ClickUpAdminError as exc:
            log.warning("/clickup setup — ClickUp hiba: %s", exc)
            await interaction.followup.send(f"❌ {exc}")
            return

        saved = await asyncio.to_thread(
            clickup_storage.save_space,
            clickup_admin.team_id(), space["name"], space["id"],
        )
        if saved is None:
            await interaction.followup.send(
                f"⚠️ A Space megvan a ClickUp-ban (**{space['name']}**, "
                f"`{space['id']}`), de az adatbázisba MENTÉS nem sikerült — így "
                f"a riasztás-router nem fogja megtalálni.\n"
                f"*Lefutott a `0014_clickup_anomaly_tasks` migration? "
                f"A részletek a Railway logban.*"
            )
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "clickup_setup",
            entity_type="clickup_structure",
            details={"space_id": space["id"], "space_name": space["name"],
                     "created": space["created"]},
        )

        allapot = "létrehozva" if space["created"] else "már létezett (nem duplikáltuk)"
        await interaction.followup.send(
            f"✅ **ClickUp Space {allapot}**\n"
            f"Név: **{space['name']}**\n"
            f"ID: `{space['id']}`\n\n"
            f"Következő lépés: `/clickup setup-manager user:@OM clickup_user_id:…` "
            f"minden PPC managerre.\n"
            f"*A ClickUp user ID-kat a `/clickup list-members` listázza.*"
        )

    # ------------------------------------------------------------------
    # /clickup setup-manager user:@X clickup_user_id:Y
    # ------------------------------------------------------------------
    @app_commands.command(
        name="setup-manager",
        description="OM ClickUp Folder + List létrehozása és mappelése (admin)",
    )
    @app_commands.describe(
        user="A PPC manager Discord felhasználója",
        clickup_user_id="A ClickUp user ID az assignee-hez (/clickup list-members mutatja)",
    )
    async def setup_manager(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        clickup_user_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not _is_admin_channel(interaction):
            await interaction.followup.send(_ADMIN_ONLY)
            return

        problem = clickup_admin.config_error()
        if problem:
            await interaction.followup.send(f"❌ A ClickUp beállítás hiányos — {problem}.")
            return

        assignee_id = (clickup_user_id or "").strip()
        if not assignee_id.isdigit():
            # NEM blokkoló hiba lenne (a task assignee nélkül is létrejön), de
            # itt még olcsó megfogni — élesben egy elgépelt ID néma, assignee
            # nélküli taskokat eredményezne.
            await interaction.followup.send(
                f"❌ A `clickup_user_id` numerikus ClickUp azonosító kell legyen "
                f"(kaptam: `{assignee_id or '—'}`).\n"
                f"Listázd a helyeseket: `/clickup list-members`"
            )
            return

        # A Space-nek már léteznie kell — enélkül nincs hova Foldert tenni.
        space_row = await asyncio.to_thread(
            clickup_storage.get_space, clickup_admin.team_id(), clickup_admin.SPACE_NAME,
        )
        if not space_row:
            await interaction.followup.send(
                "❌ Még nincs beállított ClickUp Space ehhez a Workspace-hez.\n"
                "Futtasd először: `/clickup setup`"
            )
            return

        # Auto-regisztráció: ha az OM még nincs a users táblában, létrehozzuk —
        # ugyanaz a minta, mint az /assign és a /user set-channel esetén.
        user_row, created_user = await asyncio.to_thread(
            users_storage.get_or_create_user, str(user.id), _display_name(user),
        )
        if created_user:
            log.info("Új felhasználó regisztrálva (clickup setup-manager): %s", user)

        folder_name = user_row.get("display_name") or _display_name(user)
        try:
            folder = await clickup_admin.ensure_folder(
                space_row["clickup_space_id"], folder_name,
            )
            lista = await clickup_admin.ensure_list(folder["id"])
        except ClickUpAdminError as exc:
            log.warning("/clickup setup-manager — ClickUp hiba: %s", exc)
            await interaction.followup.send(f"❌ {exc}")
            return

        saved = await asyncio.to_thread(
            clickup_storage.upsert_mapping,
            user_row["id"],
            clickup_folder_id=folder["id"],
            clickup_list_id=lista["id"],
            clickup_assignee_id=assignee_id,
        )
        if saved is None:
            await interaction.followup.send(
                f"⚠️ A ClickUp Folder és List megvan (`{folder['id']}` / "
                f"`{lista['id']}`), de a mapping MENTÉSE nem sikerült — a "
                f"riasztásokhoz így nem készül task.\n"
                f"*Lefutott a `0014_clickup_anomaly_tasks` migration? "
                f"A részletek a Railway logban.*"
            )
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "clickup_setup_manager",
            entity_type="user",
            entity_id=user_row["id"],
            details={
                "folder_id": folder["id"], "list_id": lista["id"],
                "assignee_id": assignee_id,
                "folder_created": folder["created"], "list_created": lista["created"],
            },
        )

        def _allapot(res: dict) -> str:
            return "új" if res["created"] else "meglévő"

        await interaction.followup.send(
            f"✅ **{folder_name}** ClickUp mappingja elmentve\n"
            f"📁 Folder ({_allapot(folder)}): **{folder['name']}** — `{folder['id']}`\n"
            f"📋 List ({_allapot(lista)}): **{lista['name']}** — `{lista['id']}`\n"
            f"👤 Assignee: `{assignee_id}`\n\n"
            f"Mostantól a hozzá rendelt kampányok **CRITICAL** riasztásaihoz "
            f"ebbe a listába készül task, rá szignálva."
        )

    # ------------------------------------------------------------------
    # /clickup list-members
    # ------------------------------------------------------------------
    @app_commands.command(
        name="list-members",
        description="A ClickUp Workspace tagjai és a user ID-juk (admin)",
    )
    async def list_members(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not _is_admin_channel(interaction):
            await interaction.followup.send(_ADMIN_ONLY)
            return

        problem = clickup_admin.config_error()
        if problem:
            await interaction.followup.send(f"❌ A ClickUp beállítás hiányos — {problem}.")
            return

        try:
            members = await clickup_admin.list_members()
        except ClickUpAdminError as exc:
            log.warning("/clickup list-members — ClickUp hiba: %s", exc)
            await interaction.followup.send(f"❌ {exc}")
            return

        if not members:
            await interaction.followup.send(
                "A ClickUp Workspace-nek egyetlen tagja sem jött vissza az API-ból."
            )
            return

        sorok = []
        for m in members[:_MAX_LISTED]:
            email = f" — {m['email']}" if m.get("email") else ""
            sorok.append(f"• **{m['username']}**{email} → `{m['id']}`")
        if len(members) > _MAX_LISTED:
            sorok.append(f"• *…és még {len(members) - _MAX_LISTED} további tag*")

        await interaction.followup.send(
            "👥 **ClickUp Workspace tagok**\n"
            + "\n".join(sorok)
            + "\n\n*A jobb oldali szám a `clickup_user_id` a "
              "`/clickup setup-manager` parancshoz.*"
        )

    # ------------------------------------------------------------------
    # /clickup status
    # ------------------------------------------------------------------
    @app_commands.command(
        name="status",
        description="A ClickUp integráció állapota: Space és OM-mappingek (admin)",
    )
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not _is_admin_channel(interaction):
            await interaction.followup.send(_ADMIN_ONLY)
            return

        problem = clickup_admin.config_error()
        space_row = None
        if not problem:
            space_row = await asyncio.to_thread(
                clickup_storage.get_space, clickup_admin.team_id(), clickup_admin.SPACE_NAME,
            )
        mappings = await asyncio.to_thread(clickup_storage.list_mappings)

        sorok = ["📋 **ClickUp integráció — állapot**", ""]
        if problem:
            sorok.append(f"❌ Konfiguráció: {problem}")
        else:
            sorok.append("✅ Konfiguráció: `CLICKUP_API_TOKEN` + `CLICKUP_TEAM_ID` megvan")

        if space_row:
            sorok.append(
                f"✅ Space: **{space_row.get('space_name')}** — "
                f"`{space_row.get('clickup_space_id')}`"
            )
        else:
            sorok.append("❌ Space: nincs beállítva — futtasd: `/clickup setup`")

        sorok.append("")
        if not mappings:
            sorok.append(
                "❌ **Egyetlen OM-nek sincs ClickUp mappingja** — a CRITICAL "
                "riasztások Discord-only routinggal mennek ki (task nélkül).\n"
                "Beállítás: `/clickup setup-manager user:@OM clickup_user_id:…`"
            )
        else:
            sorok.append(f"**OM-mappingek ({len(mappings)}):**")
            for row in mappings[:_MAX_LISTED]:
                user = row.get("users") or {}
                nev = user.get("display_name") or f"user #{row.get('user_id')}"
                assignee = row.get("clickup_assignee_id") or "⚠️ nincs"
                sorok.append(
                    f"• **{nev}** — lista `{row.get('clickup_list_id')}` · "
                    f"assignee `{assignee}`"
                )
            if len(mappings) > _MAX_LISTED:
                sorok.append(f"• *…és még {len(mappings) - _MAX_LISTED} további*")

        await interaction.followup.send("\n".join(sorok))


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(ClickUpCog(bot))
