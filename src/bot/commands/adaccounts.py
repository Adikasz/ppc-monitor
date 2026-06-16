"""
/adaccount slash parancscsoport — hirdetési fiók kezelés Discordból.

Parancsok:
    /adaccount add    client:<> platform:<meta|google> account_id:<>
                                              — fiók regisztrálása ügyfélhez (admin)
    /adaccount list   client:<>               — ügyfél fiókjai (platform + kampányszám)
    /adaccount remove account_id:<>           — fiók soft-delete (is_active=false, admin)

Megjegyzések:
  - Az account_id automatikusan normalizálódik: Meta → `act_` prefix,
    Google → kötőjelek eltávolítása (lásd storage.normalize_external_account_id).
  - A remove SOFT delete (is_active=false) — fizikai törlés nincs, az adat marad.
  - Az írási parancsok (add, remove) az admin csatornára vannak korlátozva.
  - Minden Supabase hívás asyncio.to_thread()-ben fut.
"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from src.config import get_config
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import audit
from src.storage import campaigns as campaigns_storage
from src.storage import clients as clients_storage
from src.utils.logging import get_logger

log = get_logger(__name__)

_PLATFORM_CHOICES = [
    app_commands.Choice(name="Meta Ads", value="meta"),
    app_commands.Choice(name="Google Ads", value="google"),
]


# ---------------------------------------------------------------------------
# Segédfüggvények
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
    admin = _admin_channel_id()
    if admin is None:
        return True
    return interaction.channel_id == admin


def _resolve_client(value: str) -> dict | None:
    """Ügyfél feloldása név VAGY numerikus ID alapján. None, ha nincs ilyen."""
    val = (value or "").strip()
    if not val:
        return None
    if val.isdigit():
        row = clients_storage.get_client(int(val))
        if row is not None:
            return row
    return clients_storage.get_client_by_name(val)


def _count_active_campaigns(ad_account_id: int) -> int:
    """Egy fiók nem-'ended' kampányainak száma (ugyanaz a láthatóság, mint /campaign list)."""
    rows = campaigns_storage.get_campaigns_by_ad_account(ad_account_id)
    return sum(1 for c in rows if c.get("lifecycle_state") != "ended")


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class AdAccountsCog(commands.GroupCog, group_name="adaccount"):
    """Az `adaccount` parancscsoport implementációja."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /adaccount add client:<> platform:<> account_id:<>
    # ------------------------------------------------------------------
    @app_commands.command(name="add", description="Hirdetési fiók regisztrálása ügyfélhez (admin)")
    @app_commands.describe(
        client="Az ügyfél neve vagy numerikus azonosítója",
        platform="Hirdetési platform: meta vagy google",
        account_id="Fiók azonosító (Meta: act_123 vagy 123, Google: 123-456-7890)",
        account_name="Opcionális: a fiók emberi neve",
    )
    @app_commands.choices(platform=_PLATFORM_CHOICES)
    async def add(
        self,
        interaction: discord.Interaction,
        client: str,
        platform: str,
        account_id: str,
        account_name: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        account_id = (account_id or "").strip()
        if not account_id:
            await interaction.followup.send("❌ A fiók azonosító nem lehet üres.")
            return

        c = await asyncio.to_thread(_resolve_client, client)
        if c is None:
            await interaction.followup.send(
                f"❌ Nem található ügyfél: **{client}**\nNézd meg: `/client list`"
            )
            return
        if not c.get("is_active", True):
            await interaction.followup.send(f"❌ Az ügyfél **{c['name']}** inaktív.")
            return

        normalized = ad_accounts_storage.normalize_external_account_id(platform, account_id)

        try:
            row, created = await asyncio.to_thread(
                ad_accounts_storage.get_or_create_ad_account,
                c["id"],
                platform,
                normalized,
                account_name=account_name.strip() if account_name else None,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Hiba a fiók regisztrálásakor (%s / %s)", c["name"], normalized)
            await interaction.followup.send(f"❌ Hiba: `{exc}`")
            return

        if not created:
            # Ha újra-aktiváljuk a korábban eltávolított fiókot, az hasznos UX.
            reactivated = False
            if not row.get("is_active", True):
                await asyncio.to_thread(
                    ad_accounts_storage.set_ad_account_active, row["id"], True
                )
                reactivated = True
            note = " — újra aktiválva ✅" if reactivated else ""
            await interaction.followup.send(
                f"ℹ️ Ez a fiók már regisztrálva van: **{platform}** / `{normalized}` "
                f"→ ügyfél **{c['name']}** *(ad_account #{row['id']})*{note}"
            )
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "adaccount_add",
            entity_type="ad_account",
            entity_id=row["id"],
            details={
                "client_id": c["id"],
                "client_name": c["name"],
                "platform": platform,
                "external_account_id": normalized,
                "account_name": account_name,
            },
        )

        log.info(
            "Hirdetési fiók regisztrálva: %s / %s → %s (ad_account #%s)",
            platform, normalized, c["name"], row["id"],
        )
        await interaction.followup.send(
            f"✅ Hirdetési fiók regisztrálva: **{platform}** / `{normalized}`\n"
            f"Ügyfél: **{c['name']}** *(ad_account #{row['id']})*\n"
            f"Következő lépés: `/discover client:{c['name']}`"
        )

    # ------------------------------------------------------------------
    # /adaccount list client:<>
    # ------------------------------------------------------------------
    @app_commands.command(name="list", description="Egy ügyfél hirdetési fiókjai (platform + kampányszám)")
    @app_commands.describe(client="Az ügyfél neve vagy numerikus azonosítója")
    async def list_(self, interaction: discord.Interaction, client: str) -> None:
        await interaction.response.defer(ephemeral=True)

        c = await asyncio.to_thread(_resolve_client, client)
        if c is None:
            await interaction.followup.send(
                f"❌ Nem található ügyfél: **{client}**\nNézd meg: `/client list`"
            )
            return

        accounts = await asyncio.to_thread(
            ad_accounts_storage.get_ad_accounts_for_client, c["id"], active_only=False
        )
        if not accounts:
            await interaction.followup.send(
                f"**{c['name']}** ügyfélhez még nincs hirdetési fiók.\n"
                f"Adj hozzá egyet: `/adaccount add` vagy `/client onboard`"
            )
            return

        lines = []
        for a in accounts:
            n_camp = await asyncio.to_thread(_count_active_campaigns, a["id"])
            name_part = f" ({a['account_name']})" if a.get("account_name") else ""
            inactive = "" if a.get("is_active", True) else " · ⏸ inaktív"
            lines.append(
                f"🖥 **`{a['platform']}`** · `{a['external_account_id']}`{name_part}\n"
                f"　📊 {n_camp} aktív kampány · *(ad_account #{a['id']})*{inactive}"
            )

        embed = discord.Embed(
            title=f"🗂 {c['name']} — hirdetési fiókok",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"Összesen: {len(accounts)} fiók")
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # /adaccount remove account_id:<>
    # ------------------------------------------------------------------
    @app_commands.command(name="remove", description="Hirdetési fiók eltávolítása — soft delete (admin)")
    @app_commands.describe(account_id="A fiók külső azonosítója (act_123 / 123 / 123-456-7890)")
    async def remove(self, interaction: discord.Interaction, account_id: str) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        account_id = (account_id or "").strip()
        if not account_id:
            await interaction.followup.send("❌ A fiók azonosító nem lehet üres.")
            return

        # Platform nélkül érkezik → több normalizált változatra is keresünk,
        # hogy bárhogy is gépelte be a felhasználó, megtaláljuk a sort.
        candidates = [
            account_id,
            ad_accounts_storage.normalize_external_account_id("meta", account_id),
            ad_accounts_storage.normalize_external_account_id("google", account_id),
        ]
        matches = await asyncio.to_thread(
            ad_accounts_storage.find_ad_accounts_by_external_id, candidates
        )
        if not matches:
            await interaction.followup.send(
                f"❌ Nem található hirdetési fiók ezzel az azonosítóval: `{account_id}`\n"
                f"Tipp: `/adaccount list client:<ügyfél>` mutatja a pontos azonosítókat."
            )
            return

        active_matches = [m for m in matches if m.get("is_active", True)]
        if not active_matches:
            await interaction.followup.send(
                f"ℹ️ A fiók már eltávolítva (inaktív): `{account_id}` "
                f"*(ad_account #{matches[0]['id']})*"
            )
            return

        removed = []
        for m in active_matches:
            await asyncio.to_thread(ad_accounts_storage.set_ad_account_active, m["id"], False)
            removed.append(m)
            await asyncio.to_thread(
                audit.log_action,
                str(interaction.user.id),
                "adaccount_remove",
                entity_type="ad_account",
                entity_id=m["id"],
                details={
                    "platform": m["platform"],
                    "external_account_id": m["external_account_id"],
                    "client_id": m.get("client_id"),
                },
            )
            log.info(
                "Hirdetési fiók soft-deleted: %s / %s (ad_account #%s)",
                m["platform"], m["external_account_id"], m["id"],
            )

        lines = "\n".join(
            f"🖥 `{m['platform']}` / `{m['external_account_id']}` *(ad_account #{m['id']})*"
            for m in removed
        )
        await interaction.followup.send(
            f"✅ Fiók eltávolítva (soft delete — az adat megmarad):\n{lines}"
        )


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(AdAccountsCog(bot))
