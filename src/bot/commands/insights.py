"""
/insight slash parancscsoport — az AI insight motor manuális futtatása.

Parancsok:
    /insight scan-now [client:<név|#id>] [limit:<N>]   — insight scan MOST (admin)

Miért kell: az insight scan naponta EGYSZER, 08:00-kor fut (scheduler cron),
így egy javítás hatása normál esetben csak másnap reggel derülne ki. Ez a
parancs UGYANAZT a függvényt hívja, amit a cron job — nem egy párhuzamos
másolatot —, így amit itt látsz, az pontosan az, amit reggel kapnál.

SZÁNDÉKOSAN NEM bypassolja a csendes időt. Ha bypassolna, egy este lefuttatott
teszt sikeresnek látszana, miközben a 08:00-s éles futás némán elhalna. Ehelyett
a válasz KÜLÖN kiírja, hány insightot nyomott el a csendes idő — így a
konfigurációs hiba (pl. QUIET_HOURS_END=9 mellett a 08:00-s scan minden
insightja elnyomódik) azonnal látszik, ahelyett hogy „0 insight"-ként jelenne meg.
"""
from __future__ import annotations

import asyncio
import time

import discord
from discord import app_commands
from discord.ext import commands

from src.bot.commands._common import (
    admin_channel_id as _admin_channel_id,  # noqa: F401 — konzisztens felület
    is_admin_channel as _is_admin_channel,
    reply_or_channel,
    resolve_client as _resolve_client,
)
from src.monitoring import scheduler as scheduler_mod
from src.storage import audit
from src.storage import clients as clients_storage
from src.utils.logging import get_logger

log = get_logger(__name__)

# Ügyfél nélkül a scan MINDEN mature kampányt végigjár. Nagy fiókpark mellett ez
# perceket vehet igénybe, a Discord interakciós token viszont 15 percig él —
# ezért a hosszú futás végén a válasz a csatornába megy, ha a followup lejárt.
_INTERACTION_TOKEN_SECONDS = 15 * 60


def _format_report(stats: dict[str, int], *, scope: str, elapsed_s: float) -> str:
    """A scan eredménye emberi formában.

    A számok ugyanabból a dictből jönnek, amit a scan visszaad és amit a záró
    log sor is használ — a Discord-válasz és a Railway log nem tud eltérni.
    """
    total = stats.get("total", 0)
    insights = stats.get("insights", 0)
    skipped = stats.get("skipped_no_history", 0)
    failed = stats.get("failed", 0)
    routed = stats.get("routed", 0)
    quiet = stats.get("quiet_hours", 0)

    if total == 0:
        return (
            f"🔍 **Insight scan — {scope}**\n"
            f"Nincs egyetlen `mature` kampány sem, amire futhatna.\n"
            f"*Az insight csak `mature` lifecycle-ú kampányra készül — "
            f"ellenőrizd: `/campaign list`.*"
        )

    sorok = [
        f"🔍 **Insight scan — {scope}** *(‎{elapsed_s:.1f} mp)*",
        "",
        f"**{total}** mature kampány vizsgálva",
        f"💡 **{insights}** insight generálva",
    ]

    if routed or quiet:
        sorok.append(f"📤 ebből kiküldve: **{routed}**")
    if quiet:
        sorok.append(
            f"🔇 csendes idő miatt elnyomva: **{quiet}** — "
            f"*ezek a DB-be bekerültek `suppressed` státusszal, de nem mentek ki*"
        )
    if skipped:
        sorok.append(
            f"⏭️ **{skipped}** kampány kihagyva — kevés historikus adat "
            f"(< {scheduler_mod._INSIGHT_MIN_HISTORY_ROWS} mérés az elmúlt 7 napban)"
        )
    if failed:
        sorok.append(
            f"❌ **{failed}** kampány hibázott — a Railway logban a "
            f"`Insight scan: kampány #…` sorok mutatják a stack trace-t"
        )

    if insights == 0 and not failed:
        sorok.append("")
        if skipped == total:
            sorok.append(
                "ℹ️ Minden kampány kimaradt adathiány miatt — a `campaign_insights` "
                "tábla nem gyűlik (fut-e az óránkénti monitoring ciklus?)."
            )
        else:
            sorok.append(
                "ℹ️ A motor lefutott, de egyetlen szabály sem tüzelt. Ez lehet "
                "helyes eredmény (nincs javasolható változtatás), vagy a 7 napos "
                "insight-dedup miatt már kiment ugyanez a héten."
            )

    return "\n".join(sorok)


class InsightsCog(commands.GroupCog, group_name="insight"):
    """Az `insight` parancscsoport — az AI insight motor kézi vezérlése."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /insight scan-now [client:<>] [limit:<N>]
    # ------------------------------------------------------------------
    @app_commands.command(
        name="scan-now",
        description="Az AI insight scan azonnali futtatása (admin csatorna)",
    )
    @app_commands.describe(
        client="Csak ennek az ügyfélnek a mature kampányai (gyorsabb teszt)",
        limit="Legfeljebb ennyi kampányt vizsgáljon (gyors próbához)",
    )
    async def scan_now(
        self,
        interaction: discord.Interaction,
        client: str | None = None,
        limit: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send(
                "Ez a parancs csak az admin csatornában használható."
            )
            return

        client_id: int | None = None
        scope = "minden ügyfél"
        if client is not None:
            c = await asyncio.to_thread(_resolve_client, client)
            if c is None:
                await interaction.followup.send(
                    f"❌ Nem található ügyfél: **{client}**\nNézd meg: `/client list`"
                )
                return
            client_id = c["id"]
            scope = c["name"]

        if limit is not None and limit < 1:
            await interaction.followup.send("❌ A `limit` legalább 1 legyen.")
            return

        if client_id is None and limit is None:
            # A teljes scan perceket vehet igénybe — mondjuk meg előre, hogy ne
            # tűnjön beragadtnak. Külön üzenet, mert a végeredmény csak később jön.
            await interaction.followup.send(
                "⏳ Teljes insight scan indul minden mature kampányra — "
                "ez több percig is tarthat. Az eredmény itt fog megjelenni."
            )

        started = time.monotonic()
        try:
            stats = await scheduler_mod.daily_insight_scan(
                client_id=client_id, limit=limit,
            )
        except Exception as exc:  # noqa: BLE001 — a parancs ne haljon néma hibával
            log.exception("Manuális insight scan hiba (ügyfél=%s)", scope)
            await self._reply(
                interaction,
                f"❌ Az insight scan elhasalt: `{exc}`\n"
                f"A részletes stack trace a Railway logban van.",
            )
            return

        elapsed = time.monotonic() - started

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "insight_scan_now",
            entity_type="client",
            entity_id=client_id,
            details={"scope": scope, "limit": limit, **stats},
        )

        await self._reply(interaction, _format_report(stats, scope=scope, elapsed_s=elapsed))

    @scan_now.autocomplete("client")
    async def scan_now_client_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        rows = await asyncio.to_thread(clients_storage.search_clients, current, active=None)
        return [
            app_commands.Choice(name=r["name"][:100], value=str(r["id"]))
            for r in rows
        ][:25]

    # ------------------------------------------------------------------

    async def _reply(self, interaction: discord.Interaction, content: str) -> None:
        await reply_or_channel(interaction, content, logger=log, what="Insight scan")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InsightsCog(bot))
