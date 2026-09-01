"""
/discover slash parancscsoport — kampány auto-discovery triggerelése Discordból.

Parancsok:
    /discover client client:<név vagy id>  — egy ügyfél kampány-discoveryje
    /discover all                          — minden aktív ügyfél (csak admin csatorna)
    /discover google                       — a Google-fiókos ügyfelek (csak admin csatorna)

A discovery lekéri az ügyfél hirdetési fiókjaihoz tartozó kampányokat a
Meta/Google API-ból, és szinkronizálja a `campaigns` táblát (insert/update/
soft-delete). A részleges hibák (pl. egy fiók API-hibája) NEM állítják le a
futást — az eredmény `errors` listájában jelennek meg.

A discovery ÜTEMEZETTEN IS FUT (minden nap 03:30, lásd
`scheduler.daily_discovery_job`). Ezek a parancsok tehát nem az egyetlen útja a
kampánylista frissülésének, hanem a napi job KÉZI futtatásai — és pontosan
UGYANAZT a függvényt hívják (`/discover all` szűrő nélkül, `/discover google` a
Google-fiókos ügyfelek ID-jaival). Nincs külön "kézi változat", ami
elsodródhatna az ütemezettől.

Megjegyzések:
  - A discovery hálózati hívásokat végez → asyncio.to_thread() + defer
    (a Discord followup ablak 15 perc, ez bőven elég).
  - A /discover all és /discover google admin csatornára van korlátozva.
"""
from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from src.config import get_config
from src.monitoring import scheduler as scheduler_mod
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


def _job_had_problems(stats: dict[str, Any]) -> bool:
    """Volt-e bármilyen hiba a job futásában (embed-szín döntéshez)."""
    return bool(stats.get("errors") or stats.get("failed_clients"))


def _job_description(stats: dict[str, Any], *, scope: str | None = None) -> str:
    """A `daily_discovery_job` eredményének embed-leírása, ügyfelenkénti sorokkal.

    A fejléc SZÁNDÉKOSAN a fiók-szintű számokat is mutatja (`accounts_failed`):
    élesben pont az a néma hiba, amikor néhány ügynökségi fiók jogosultsági
    hiba miatt kimarad, és az összesítő csak annyit mond, hogy "0 új kampány".
    """
    header = (
        f"**Összesítő ({stats['clients']} ügyfél, {stats['accounts']} fiók):** "
        f"📊 {stats['inserted']} új · {stats['updated']} frissítve · "
        f"{stats['deactivated']} deaktiválva"
    )
    if scope:
        header = f"*Hatókör: {scope}*\n" + header
    if stats["accounts_failed"]:
        header += f" · 🚫 {stats['accounts_failed']} fiók nem elérhető"
    if stats["errors"] or stats["failed_clients"]:
        header += f" · ⚠️ {stats['errors']} hiba"
        if stats["failed_clients"]:
            header += f" ({stats['failed_clients']} ügyfél elszállt)"
    description = header + "\n\n"

    # Description-alapú lista, a 4096 karakteres embed limit alatt tartva.
    for entry in stats.get("per_client") or []:
        name = entry.get("name") or f"#{entry.get('client_id')}"
        if entry.get("error"):
            line = f"❌ **{name}** — `{entry['error']}`"
        else:
            line = f"**{name}** — {_summary_line(entry['result'])}"
        if len(description) + len(line) + 1 > 3900:
            description += "… (a többi ügyfél nem fért ki — lásd logok)"
            break
        description += line + "\n"
    return description


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

        log.info("/discover all indítva (a napi 03:30-s job kézi futtatása)")

        # UGYANAZ a függvény, amit a hajnali cron hív — nincs külön "kézi
        # változat", ami elsodródhatna az ütemezettől.
        stats = await scheduler_mod.daily_discovery_job()
        if not stats["clients"]:
            await interaction.followup.send("Nincs aktív ügyfél, amin futtatható lenne a discovery.")
            return

        embed = discord.Embed(
            title="🔍 Discovery — összes aktív ügyfél",
            description=_job_description(stats),
            color=discord.Color.orange() if _job_had_problems(stats) else discord.Color.green(),
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

        # Ugyanaz a job, csak az ügyfélkörre szűkítve — a Google-fiókos
        # ügyfelekre. (A discovery ügyfél-szintű: ezeknél a MÉTA fiókok is
        # frissülnek, ami nem baj — a parancs a Google-kör lefedését garantálja.)
        stats = await scheduler_mod.daily_discovery_job(client_ids=client_ids)

        embed = discord.Embed(
            title="🔍 Discovery — Google fiókok",
            description=_job_description(stats, scope=f"{len(client_ids)} Google-ügyfél"),
            color=discord.Color.orange() if _job_had_problems(stats) else discord.Color.green(),
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(DiscoveryCog(bot))
