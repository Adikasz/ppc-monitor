"""
/discover slash parancscsoport — kampány auto-discovery triggerelése Discordból.

Parancsok:
    /discover client client:<név vagy id>  — egy ügyfél kampány-discoveryje
    /discover all                          — minden aktív ügyfél (csak admin csatorna)

A discovery lekéri az ügyfél hirdetési fiókjaihoz tartozó kampányokat a
Meta/Google API-ból, és szinkronizálja a `campaigns` táblát (insert/update/
soft-delete). A részleges hibák (pl. egy fiók API-hibája) NEM állítják le a
futást — az eredmény `errors` listájában jelennek meg.

Megjegyzések:
  - A discovery hálózati hívásokat végez → asyncio.to_thread() + defer
    (a Discord followup ablak 15 perc, ez bőven elég).
  - A /discover all admin csatornára van korlátozva (sok ügyfélen futhat).
"""
from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from src.config import get_config
from src.monitoring.discovery import discover_campaigns_for_client
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import clients as clients_storage
from src.utils.logging import get_logger

log = get_logger(__name__)

# Hány hiba-sort mutassunk maximum a válaszban (a többit a log tartalmazza).
_MAX_ERROR_LINES = 5


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


def _summary_line(result: dict[str, Any]) -> str:
    """Egy discovery-eredmény tömör összefoglaló sora."""
    line = (
        f"📊 {result['inserted']} új · {result['updated']} frissítve · "
        f"{result['deactivated']} deaktiválva"
    )
    if result["errors"]:
        line += f" · ⚠️ {len(result['errors'])} hiba"
    return line


def _format_errors(errors: list[dict[str, Any]]) -> str:
    """Hibalista formázása (legfeljebb _MAX_ERROR_LINES sor)."""
    lines = []
    for err in errors[:_MAX_ERROR_LINES]:
        acct = err.get("account", "?")
        camp = err.get("campaign")
        target = f"`{acct}`" + (f" / kampány `{camp}`" if camp else "")
        lines.append(f"• {target}: {err.get('error', '?')}")
    if len(errors) > _MAX_ERROR_LINES:
        lines.append(f"… és még {len(errors) - _MAX_ERROR_LINES} hiba (lásd logok)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class DiscoveryCog(commands.GroupCog, group_name="discover"):
    """A `discover` parancscsoport implementációja."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /discover client client:<név vagy id>
    # ------------------------------------------------------------------
    @app_commands.command(name="client", description="Egy ügyfél kampány-discoveryje")
    @app_commands.describe(client="Az ügyfél neve vagy numerikus azonosítója")
    async def client(self, interaction: discord.Interaction, client: str) -> None:
        await interaction.response.defer(ephemeral=True)

        c = await asyncio.to_thread(_resolve_client, client)
        if c is None:
            await interaction.followup.send(
                f"❌ Nem található ügyfél: **{client}**\nNézd meg: `/client list`"
            )
            return

        log.info("/discover client indítva: %s (#%s)", c["name"], c["id"])
        try:
            result = await asyncio.to_thread(discover_campaigns_for_client, c["id"])
        except Exception as exc:  # noqa: BLE001
            log.exception("Discovery fatális hiba (client_id=%s)", c["id"])
            await interaction.followup.send(f"❌ Discovery hiba: `{exc}`")
            return

        embed = discord.Embed(
            title=f"🔍 Discovery — {c['name']}",
            description=_summary_line(result),
            color=discord.Color.orange() if result["errors"] else discord.Color.green(),
        )
        if result["errors"]:
            embed.add_field(name="Hibák", value=_format_errors(result["errors"]), inline=False)
        await interaction.followup.send(embed=embed)

    @client.autocomplete("client")
    async def client_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        rows = await asyncio.to_thread(clients_storage.search_clients, current, active=True)
        return [app_commands.Choice(name=r["name"][:100], value=str(r["id"])) for r in rows][:25]

    # ------------------------------------------------------------------
    # /discover all
    # ------------------------------------------------------------------
    @app_commands.command(name="all", description="Minden aktív ügyfél discoveryje (csak admin csatorna)")
    async def all_(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send(
                "Ez a parancs csak az admin csatornában használható (sok ügyfélen futhat)."
            )
            return

        clients = await asyncio.to_thread(clients_storage.list_clients, active_only=True)
        if not clients:
            await interaction.followup.send("Nincs aktív ügyfél, amin futtatható lenne a discovery.")
            return

        log.info("/discover all indítva: %d aktív ügyfél", len(clients))

        totals = {"inserted": 0, "updated": 0, "deactivated": 0, "errors": 0}
        per_client_lines: list[str] = []

        for c in clients:
            try:
                result = await asyncio.to_thread(discover_campaigns_for_client, c["id"])
            except Exception as exc:  # noqa: BLE001
                log.exception("Discovery fatális hiba (client_id=%s)", c["id"])
                per_client_lines.append(f"**{c['name']}** — ❌ `{exc}`")
                totals["errors"] += 1
                continue

            totals["inserted"] += result["inserted"]
            totals["updated"] += result["updated"]
            totals["deactivated"] += result["deactivated"]
            totals["errors"] += len(result["errors"])
            per_client_lines.append(f"**{c['name']}** — {_summary_line(result)}")

        # Description-alapú lista, a 4096 karakteres embed limit alatt tartva.
        header = (
            f"**Összesítő ({len(clients)} ügyfél):** "
            f"📊 {totals['inserted']} új · {totals['updated']} frissítve · "
            f"{totals['deactivated']} deaktiválva · ⚠️ {totals['errors']} hiba\n\n"
        )
        description = header
        for line in per_client_lines:
            if len(description) + len(line) + 1 > 3900:
                description += "… (a többi ügyfél nem fért ki)"
                break
            description += line + "\n"

        embed = discord.Embed(
            title="🔍 Discovery — összes aktív ügyfél",
            description=description,
            color=discord.Color.orange() if totals["errors"] else discord.Color.green(),
        )
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # /discover google  — csak a Google-fiókos ügyfelek (Railway-en fut a SDK)
    # ------------------------------------------------------------------
    @app_commands.command(
        name="google",
        description="Csak a Google-fiókos ügyfelek discoveryje (csak admin csatorna)",
    )
    async def google(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send(
                "Ez a parancs csak az admin csatornában használható (sok ügyfélen futhat)."
            )
            return

        # Aktív Google fiókok → érintett ügyfelek (client_id szerint dedup, mert a
        # discovery ügyfél-szintű: egy kliens minden fiókját nézi).
        accounts = await asyncio.to_thread(ad_accounts_storage.list_ad_accounts, active_only=True)
        google_accounts = [a for a in accounts if a.get("platform") == "google"]
        if not google_accounts:
            await interaction.followup.send("Nincs aktív Google (platform=`google`) fiók.")
            return

        client_ids: list[int] = []
        seen: set[int] = set()
        for a in google_accounts:
            cid = a.get("client_id")
            if cid is not None and cid not in seen:
                seen.add(cid)
                client_ids.append(cid)

        log.info(
            "/discover google indítva: %d Google fiók, %d ügyfél",
            len(google_accounts), len(client_ids),
        )

        totals = {"inserted": 0, "updated": 0, "deactivated": 0, "errors": 0}
        per_client_lines: list[str] = []

        for cid in client_ids:
            c = await asyncio.to_thread(clients_storage.get_client, cid)
            cname = (c or {}).get("name", f"#{cid}")
            try:
                result = await asyncio.to_thread(discover_campaigns_for_client, cid)
            except Exception as exc:  # noqa: BLE001
                log.exception("Google discovery fatális hiba (client_id=%s)", cid)
                per_client_lines.append(f"❌ **{cname}** — `{exc}`")
                totals["errors"] += 1
                continue

            totals["inserted"] += result["inserted"]
            totals["updated"] += result["updated"]
            totals["deactivated"] += result["deactivated"]
            totals["errors"] += len(result["errors"])
            note = f" ⚠️ {len(result['errors'])} hiba" if result["errors"] else ""
            per_client_lines.append(f"✅ **{cname}**: {result['inserted']} kampány{note}")

        header = (
            f"**Összesen: {totals['inserted']} új kampány** "
            f"({len(client_ids)} Google-ügyfél · {totals['updated']} frissítve · "
            f"{totals['deactivated']} deaktiválva · ⚠️ {totals['errors']} hiba)\n\n"
        )
        description = header
        for line in per_client_lines:
            if len(description) + len(line) + 1 > 3900:
                description += "… (a többi ügyfél nem fért ki)"
                break
            description += line + "\n"

        embed = discord.Embed(
            title="🔍 Discovery — Google fiókok",
            description=description,
            color=discord.Color.orange() if totals["errors"] else discord.Color.green(),
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(DiscoveryCog(bot))
