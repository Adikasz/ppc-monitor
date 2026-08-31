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
  - A set-channel FIGYELMEZTET, ha a választott csatorna már egy másik userhez
    tartozik, de NEM blokkol: lehet jogos átmeneti állapot, és egy blokkoló hiba
    rosszabb lenne, mint az ütközés (a user csatorna nélkül maradna). Az ütközés
    következményeit lásd `storage.users.get_user_by_alerts_channel` —
    élesben előfordult, és a /my parancsok a rossz OM hatókörén dolgozhattak.
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


def _channel_occupancy(rows: list[dict]) -> dict[str, list[dict]]:
    """{alerts_channel_id: [user, …]} — melyik csatornához hány user tartozik.

    Tiszta Python a már lekért user-listán (nincs plusz DB kör). A `/user list`
    ebből jelöli az ütközéseket.
    """
    out: dict[str, list[dict]] = {}
    for row in rows:
        channel_id = row.get("alerts_channel_id")
        if channel_id:
            out.setdefault(str(channel_id), []).append(row)
    return out


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

        # Foglaltság-ellenőrzés a beállítás ELŐTT: kié most ez a csatorna?
        # FIGYELMEZTET, DE NEM BLOKKOL — lehet jogos átmeneti állapot (átadás,
        # tudatos csapat-csatorna), és egy blokkoló hiba itt rosszabb lenne,
        # mint egy ütközés: a user csatorna nélkül maradna, és semmit nem kapna.
        elozo_tulajok = await asyncio.to_thread(
            users_storage.find_users_by_alerts_channel, str(channel.id)
        )
        masok = [u for u in elozo_tulajok if u.get("id") != user_row.get("id")]

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
            details={
                "channel_id": str(channel.id),
                # Az audit sorból utólag is kiderül, ha ütközésbe futott —
                # enélkül csak a Railway log őrizné, ami rotálódik.
                "conflicts_with": [
                    {"id": u.get("id"), "display_name": u.get("display_name")}
                    for u in masok
                ],
            },
        )

        log.info("Alert csatorna beállítva: %s → %s", interaction.user, channel.id)

        valasz = (
            f"✅ Alert csatornád beállítva: <#{channel.id}>\n"
            f"Mostantól a hozzád rendelt kampányok riasztásai ide érkeznek."
        )
        if masok:
            nevek = ", ".join(
                f"**{u.get('display_name') or '?'}** (#{u.get('id')})" for u in masok
            )
            log.warning(
                "Csatorna-ütközés a set-channel után: %s csatornán már ott van %s",
                channel.id, nevek,
            )
            valasz += (
                f"\n\n⚠️ **Figyelem — ez a csatorna már foglalt:** {nevek}.\n"
                f"Amíg ez így marad, ide MINDKETTŐTÖK riasztásai és napi "
                f"összefoglalói megérkeznek (tehát több üzenet, idegen "
                f"kampányokkal), a csatorna-szkópolt parancsok (`/my …`) pedig "
                f"itt nem fognak működni, mert nem eldönthető, ki az OM.\n"
                f"Ha ez nem szándékos, valamelyikőtök válasszon másik csatornát."
            )
        await interaction.followup.send(valasz)

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

        # Csatorna-ütközés jelölése: ha két usernél ugyanaz a csatorna, az
        # riasztás-duplázást és működésképtelen `/my` parancsokat okoz. Itt a
        # lista az a hely, ahol az admin ezt egy pillantással észreveheti.
        foglaltsag = _channel_occupancy(rows)

        lines: list[str] = []
        for row in rows:
            uid = row.get("id", "?")
            name = row.get("display_name") or f"#{uid}"
            inactive = "" if row.get("is_active", True) else " ⏸ *(inaktív)*"
            channel_id = row.get("alerts_channel_id")
            if not channel_id:
                lines.append(f"- **{name}** (#{uid}) ❌ (nincs beállítva){inactive}")
                continue
            utkozes = ""
            others = [u for u in foglaltsag[str(channel_id)] if u.get("id") != row.get("id")]
            if others:
                nevek = ", ".join(u.get("display_name") or f"#{u.get('id')}" for u in others)
                utkozes = f" ⚠️ **ütközik**: {nevek}"
            lines.append(f"- **{name}** (#{uid}) ✅ <#{channel_id}>{inactive}{utkozes}")

        embed = discord.Embed(
            title="👥 Felhasználók — alert csatornák",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        utkozo_csatornak = sum(1 for us in foglaltsag.values() if len(us) > 1)
        labjegyzet = f"Összesen: {len(rows)} felhasználó"
        if utkozo_csatornak:
            labjegyzet += (
                f" · ⚠️ {utkozo_csatornak} csatorna több userhez tartozik "
                f"(dupla értesítés, a /my parancsok ott nem működnek)"
            )
        embed.set_footer(text=labjegyzet)
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(UsersCog(bot))
