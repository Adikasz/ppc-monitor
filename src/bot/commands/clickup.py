"""
/clickup slash parancscsoport — az OM → ClickUp lista leképezés kezelése.

Parancsok (mind admin csatorna):
    /clickup setup-manager user:@X clickup_user_id:Y folder_id:Z list_id:W
                            — egy PPC manager mappingjának mentése (ellenőrzéssel)
    /clickup list-members   — a Workspace tagjai + ClickUp user ID-juk
    /clickup status         — mi van beállítva, és él-e még a ClickUp oldalon

A STRUKTÚRÁT NEM A BOT HOZZA LÉTRE:
    A Space-t ("PPC Anomália Riasztások"), a managerenkénti Foldert és a benne
    lévő Listát az ügyfél készíti el KÉZZEL a ClickUp felületén. A bot csak a
    kész azonosítókat kapja meg, és MENTÉS ELŐTT ellenőrzi őket a ClickUp
    API-n — így érvénytelen ID nem kerülhet az adatbázisba.

    (Korábban volt egy `/clickup setup` parancs, ami API-ból hozta létre a
    Space-t. Megszűnt: a struktúra tulajdonosa a csapat, nem a bot. Nem
    hagytuk bent no-op parancsként — egy parancs, ami semmit nem csinál,
    rosszabb, mint a hiánya; a `/clickup status` viszont kiírja a kézi
    beállítás lépéseit, ha még nincs egyetlen mapping sem.)

Ez a parancscsoport SZÁNDÉKOSAN hangos a hibákra: a `clickup_admin` modul
kivételt dob emberi üzenettel, amit itt egy az egyben kiírunk. Egy néma "nem
sikerült" válasz ugyanaz a hibaosztály lenne, ami az insight scan-nél hetekig
rejtve maradt.
"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from src.bot.commands._common import (
    is_admin_channel as _is_admin_channel,
    reply_or_channel,
)
from src.integrations import clickup_admin
from src.integrations.clickup_admin import ClickUpAdminError
from src.storage import audit
from src.storage import clickup_mapping as clickup_storage
from src.storage import users as users_storage
from src.utils.logging import get_logger

log = get_logger(__name__)

# A Discord üzenet 2000 karakteres — hosszú listáknál vágunk, de NEM némán.
_MAX_LISTED = 25

# A `/clickup status` ennyi mappingot ellenőriz élőben a ClickUp API-n. A többit
# kilistázza, de "nem ellenőrzött" jelöléssel — a ClickUp percenkénti kérés-
# limitjébe egy nagy csapatnál bele lehetne futni, és a némán csonkolt
# ellenőrzés rosszabb, mint a bevallott.
_MAX_VALIDATED = 15

_ADMIN_ONLY = "Ez a parancs csak az admin csatornában használható."

_SETUP_STEPS = (
    "**A ClickUp struktúrát kézzel kell létrehozni:**\n"
    f"1. Egy Space a ClickUp-ban (javasolt név: **{clickup_admin.SPACE_NAME}**)\n"
    "2. Minden PPC managerhez egy Folder a Space-en belül\n"
    "3. Mindegyik Folderben egy List (ide kerülnek a taskok)\n"
    "4. A Folder és a List ID-ját másold ki (jobb klikk → *Copy link*), majd:\n"
    "`/clickup setup-manager user:@OM clickup_user_id:… folder_id:… list_id:…`\n"
    "*A ClickUp user ID-kat a `/clickup list-members` listázza.*"
)


def _display_name(member: discord.Member | discord.User) -> str:
    """Emberi olvasható név Discord member/user objektumból (mint az /assign-nél)."""
    if isinstance(member, discord.Member) and member.nick:
        return member.nick
    return member.display_name or member.name


def _id_error(label: str, raw: str, kind: str) -> str:
    """Hibaüzenet be nem olvasható azonosítóra, a helyes formákkal."""
    pelda = (
        "https://app.clickup.com/9012345/v/li/901234567890"
        if kind == "list"
        else "https://app.clickup.com/9012345/v/o/f/90123456"
    )
    return (
        f"❌ A **{label}** nem olvasható ki ebből: `{raw}`\n"
        f"Add meg a nyers numerikus ID-t, vagy illeszd be a ClickUp linket "
        f"(jobb klikk az elemen → *Copy link*), pl. `{pelda}`."
    )


class ClickUpCog(commands.GroupCog, group_name="clickup"):
    """A `clickup` parancscsoport — OM-mapping és állapot."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /clickup setup-manager user:@X clickup_user_id:Y folder_id:Z list_id:W
    # ------------------------------------------------------------------
    @app_commands.command(
        name="setup-manager",
        description="OM ClickUp listájának mappelése a megadott ID-kkal (admin)",
    )
    @app_commands.describe(
        user="A PPC manager Discord felhasználója",
        clickup_user_id="A ClickUp user ID az assignee-hez (/clickup list-members mutatja)",
        folder_id="A manager ClickUp Folderének ID-ja vagy linkje",
        list_id="A Folderben lévő List ID-ja vagy linkje — ide kerülnek a taskok",
    )
    async def setup_manager(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        clickup_user_id: str,
        folder_id: str,
        list_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not _is_admin_channel(interaction):
            await interaction.followup.send(_ADMIN_ONLY)
            return

        problem = clickup_admin.config_error()
        if problem:
            await interaction.followup.send(f"❌ A ClickUp beállítás hiányos — {problem}.")
            return

        # --- 1) Bemenet-beolvasás (nyers ID vagy beillesztett ClickUp link) ---
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

        folder_ref = clickup_admin.parse_id(folder_id, "folder")
        if folder_ref is None:
            await interaction.followup.send(_id_error("Folder ID", folder_id, "folder"))
            return

        list_ref = clickup_admin.parse_id(list_id, "list")
        if list_ref is None:
            await interaction.followup.send(_id_error("List ID", list_id, "list"))
            return

        # --- 2) Ellenőrzés a ClickUp API-n, MENTÉS ELŐTT --------------------
        # Egy 404-es lista némán nyelné el az összes későbbi riasztás-taskot,
        # ezért inkább itt állunk meg, mint hogy érvénytelen ID-t mentsünk.
        try:
            lista = await clickup_admin.get_list(list_ref)
        except ClickUpAdminError as exc:
            log.warning("/clickup setup-manager — lista-ellenőrzés hiba: %s", exc)
            await interaction.followup.send(
                f"❌ {exc}\n"
                f"*A mapping NEM lett elmentve. Ellenőrizd, hogy a lista létezik-e, "
                f"és hogy a ClickUp token tulajdonosa látja-e.*"
            )
            return

        # A lista TÉNYLEG a megadott Folderben van? Ez fogja meg a leggyakoribb
        # kézi hibát: egy másik manager Folderéből kimásolt lista-ID.
        if lista["folder_hidden"]:
            await interaction.followup.send(
                f"❌ A **{lista['name']}** lista nem Folderben van (folderless lista), "
                f"a megadott Folder ID (`{folder_ref}`) így nem tartozhat hozzá.\n"
                f"*Hozd létre a listát a manager Folderén BELÜL, és másold ki újra az ID-kat.*"
            )
            return
        if lista["folder_id"] and lista["folder_id"] != folder_ref:
            await interaction.followup.send(
                f"❌ A **{lista['name']}** lista NEM a megadott Folderben van.\n"
                f"Megadott Folder: `{folder_ref}` · "
                f"A lista tényleges Foldere: **{lista['folder_name']}** (`{lista['folder_id']}`)\n"
                f"*A mapping NEM lett elmentve — ellenőrizd, melyik manager Folderéből másoltál.*"
            )
            return

        # --- 3) Mentés -----------------------------------------------------
        # Auto-regisztráció: ha az OM még nincs a users táblában, létrehozzuk —
        # ugyanaz a minta, mint az /assign és a /user set-channel esetén.
        user_row, created_user = await asyncio.to_thread(
            users_storage.get_or_create_user, str(user.id), _display_name(user),
        )
        if created_user:
            log.info("Új felhasználó regisztrálva (clickup setup-manager): %s", user)

        saved = await asyncio.to_thread(
            clickup_storage.upsert_mapping,
            user_row["id"],
            clickup_folder_id=folder_ref,
            clickup_list_id=list_ref,
            clickup_assignee_id=assignee_id,
        )
        if saved is None:
            await interaction.followup.send(
                f"⚠️ A ClickUp azonosítók rendben vannak (lista: **{lista['name']}**), "
                f"de a mapping MENTÉSE nem sikerült — a riasztásokhoz így nem "
                f"készül task.\n"
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
                "folder_id": folder_ref, "list_id": list_ref,
                "assignee_id": assignee_id, "list_name": lista["name"],
            },
        )

        nev = user_row.get("display_name") or _display_name(user)
        await interaction.followup.send(
            f"✅ **{nev}** ClickUp mappingja elmentve\n"
            f"📁 Folder: **{lista['folder_name'] or '?'}** — `{folder_ref}`\n"
            f"📋 List: **{lista['name']}** — `{list_ref}`\n"
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
        description="A ClickUp integráció állapota és a mappingek ellenőrzése (admin)",
    )
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not _is_admin_channel(interaction):
            await interaction.followup.send(_ADMIN_ONLY)
            return

        problem = clickup_admin.config_error()
        mappings = await asyncio.to_thread(clickup_storage.list_mappings)

        sorok = ["📋 **ClickUp integráció — állapot**", ""]
        if problem:
            sorok.append(f"❌ Konfiguráció: {problem}")
        else:
            sorok.append("✅ Konfiguráció: `CLICKUP_API_TOKEN` + `CLICKUP_TEAM_ID` megvan")

        sorok.append("")
        if not mappings:
            sorok.append(
                "❌ **Egyetlen OM-nek sincs ClickUp mappingja** — a CRITICAL "
                "riasztások Discord-only routinggal mennek ki (task nélkül).\n"
            )
            sorok.append(_SETUP_STEPS)
            await self._reply(interaction, "\n".join(sorok))
            return

        sorok.append(f"**OM-mappingek ({len(mappings)}):**")
        for index, row in enumerate(mappings[:_MAX_LISTED]):
            user = row.get("users") or {}
            nev = user.get("display_name") or f"user #{row.get('user_id')}"
            assignee = row.get("clickup_assignee_id") or "⚠️ nincs"
            allapot = await self._validate_row(
                row, validate=(problem is None and index < _MAX_VALIDATED),
            )
            sorok.append(
                f"{allapot} **{nev}** — lista `{row.get('clickup_list_id')}` · "
                f"assignee `{assignee}`"
            )

        if len(mappings) > _MAX_LISTED:
            sorok.append(f"• *…és még {len(mappings) - _MAX_LISTED} további (nem listázva)*")
        if problem is None and len(mappings) > _MAX_VALIDATED:
            sorok.append(
                f"\n*A ClickUp-ellenőrzés az első {_MAX_VALIDATED} sorra futott "
                f"(kérés-limit); a többi ❔ jelet kapott.*"
            )

        await self._reply(interaction, "\n".join(sorok))

    async def _reply(self, interaction: discord.Interaction, content: str) -> None:
        """Válasz a followupon, csatorna-fallbackkel.

        A státusz akár `_MAX_VALIDATED` ClickUp hívást is indít; ha a ClickUp
        lassú, a 15 perces interakciós token lejárhat, és az admin a hosszú
        várakozás után semmit nem látna (ugyanaz a minta, mint az `/insight
        scan-now` és a `/report weekly-now` esetén).
        """
        await reply_or_channel(interaction, content, logger=log, what="ClickUp státusz")

    async def _validate_row(self, row: dict, *, validate: bool) -> str:
        """Egy mapping sor állapot-jele: él-e még a lista a ClickUp-ban.

        ✅ elérhető · ⚠️ elérhető, de már MÁS Folderben van (áthelyezték) ·
        ❌ nem érhető el (törölték / elveszett a jogosultság) · ❔ nem ellenőriztük

        Az átNEVEZÉS szándékosan nem hiba: a taskokat a lista ID-ja alapján
        hozzuk létre, az együtt él az átnevezéssel. A törlés és az áthelyezés
        viszont valódi eltérés a mentett állapottól.
        """
        if not validate:
            return "❔"
        try:
            lista = await clickup_admin.get_list(str(row.get("clickup_list_id")))
        except ClickUpAdminError as exc:
            log.warning(
                "/clickup status — a(z) %s lista nem ellenőrizhető: %s",
                row.get("clickup_list_id"), exc,
            )
            return "❌"
        stored_folder = str(row.get("clickup_folder_id") or "")
        if lista["folder_id"] and stored_folder and lista["folder_id"] != stored_folder:
            return "⚠️"
        return "✅"


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(ClickUpCog(bot))
