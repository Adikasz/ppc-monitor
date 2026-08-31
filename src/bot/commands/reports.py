"""
/report slash parancscsoport — a heti riport manuális futtatása.

Parancsok:
    /report weekly-now [client:<név|#id>]   — heti riport MOST (admin)

Miért kell: a heti riport HÉTFŐN 08:00-kor fut (scheduler cron), így egy
javítás hatása normál esetben csak a következő hétfőn derülne ki. Ez a parancs
UGYANAZT a függvényt hívja, amit a cron job (`weekly_action_report_job` →
`generate_weekly_action_reports`) — nem egy párhuzamos másolatot.

Az ablak IS ugyanaz: a generátor mindig az ELŐZŐ lezárult naptári hétre számol,
függetlenül attól, hogy mikor indítod (lásd `previous_week_range`). Amit tehát
itt szerdán látsz, az bitre az, amit hétfő reggel kaptál volna.

A válasz megmondja, mi történt ÜGYFELENKÉNT: hány riport készült el, hány
hibázott és MIÉRT, illetve ki maradt ki és milyen okból. A néma „0 Doc"
ugyanaz a hibaosztály, ami az insight scan-nél hetekig rejtve maradt.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

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

# Ennyi sort listázunk ki tételesen a Doc-linkekből / hibákból. A Discord üzenet
# 2000 karakteres — 20+ ügyfélnél a teljes lista túlcsordulna, ezért vágunk, de
# NEM némán: a maradékot számmal jelezzük.
_MAX_LISTED = 10


def _format_report(stats: dict[str, Any], *, scope: str, elapsed_s: float) -> str:
    """A riport-futás eredménye emberi formában.

    A számok ugyanabból a dictből jönnek, amit a generátor visszaad és amiből a
    záró log sor is épül — a Discord-válasz és a Railway log nem tud eltérni.
    """
    total = stats.get("total", 0)
    success = stats.get("success", 0)
    failed = stats.get("failed", 0)
    no_mature = stats.get("skipped_no_mature", 0)
    no_data = stats.get("skipped_no_data", 0)
    skipped_config = stats.get("skipped_config", 0)
    config_error = stats.get("config_error")
    errors = stats.get("errors") or []
    docs = stats.get("docs") or []
    label = stats.get("week_label") or "?"

    sorok = [
        f"📊 **Heti riport — {scope}** *(‎{elapsed_s:.1f} mp)*",
        f"*Vizsgált hét: {label}*",
        "",
    ]

    if total == 0:
        sorok.append("**0** ügyfélre futott le a riport.")
        if no_mature:
            sorok.append(
                f"⏭️ **{no_mature}** ügyfél kihagyva — nincs `mature` kampánya "
                f"*(a heti riport csak felnőtt kampányokra készül)*"
            )
        if no_data:
            sorok.append(
                f"⏭️ **{no_data}** ügyfél kihagyva — sem az elmúlt, sem az azt "
                f"megelőző héten nem volt költése"
            )
        if not no_mature and not no_data:
            sorok.append(
                "ℹ️ Egyetlen aktív ügyfél sem jött vissza a `clients` táblából — "
                "ellenőrizd: `/client list`."
            )
        if config_error:
            sorok.append("")
            sorok.append(f"⚠️ **Hiányzó beállítás:** {config_error}")
        return "\n".join(sorok)

    sorok.append(f"**{total}** ügyfél feldolgozva")
    sorok.append(f"✅ **{success}** ClickUp Doc elkészült")

    if skipped_config:
        sorok.append(
            f"⚠️ **{skipped_config}** ügyfélnél NEM készült Doc — "
            f"{config_error or 'hiányzó beállítás'}"
        )
        sorok.append(
            "*A heti számokat így is elmentettük, tehát a jövő heti "
            "összehasonlítás nem vész el.*"
        )

    if failed:
        sorok.append(f"❌ **{failed}** ügyfél hibázott:")
        for item in errors[:_MAX_LISTED]:
            sorok.append(f"• **{item.get('client', '?')}** — {item.get('error', '?')}")
        if len(errors) > _MAX_LISTED:
            sorok.append(f"• *…és még {len(errors) - _MAX_LISTED} további (lásd a Railway logot)*")

    if no_mature:
        sorok.append(f"⏭️ **{no_mature}** ügyfél kihagyva — nincs `mature` kampánya")
    if no_data:
        sorok.append(f"⏭️ **{no_data}** ügyfél kihagyva — nem volt költése egyik héten sem")

    if docs:
        sorok.append("")
        sorok.append("**Elkészült riportok:**")
        for doc in docs[:_MAX_LISTED]:
            # `<url>` és nem `[cím](url)`: a maszkolt link megjelenítése
            # kontextusfüggő, a szögletes zárójel viszont mindenhol kattintható
            # linket ad, és elnyomja a link-előnézetet (10 riportnál 10 kártya
            # tolná szét az üzenetet).
            sorok.append(f"• **{doc.get('client', '?')}** — <{doc.get('url', '')}>")
        if len(docs) > _MAX_LISTED:
            sorok.append(f"• *…és még {len(docs) - _MAX_LISTED} további*")

    if success == 0 and not failed and not skipped_config:
        sorok.append("")
        sorok.append(
            "ℹ️ A generátor lefutott, de egyetlen Doc sem készült el — ez a "
            "kombináció nem várt, nézd meg a Railway log `Heti riport` sorait."
        )

    return "\n".join(sorok)


class ReportsCog(commands.GroupCog, group_name="report"):
    """A `report` parancscsoport — a heti riport kézi futtatása."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /report weekly-now [client:<>]
    # ------------------------------------------------------------------
    @app_commands.command(
        name="weekly-now",
        description="A heti riport azonnali futtatása (admin csatorna)",
    )
    @app_commands.describe(
        client="Csak ennek az ügyfélnek a heti riportja (gyorsabb teszt)",
    )
    async def weekly_now(
        self,
        interaction: discord.Interaction,
        client: str | None = None,
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

        if client_id is None:
            # A teljes kör ügyfelenként egy Claude hívás + egy ClickUp Doc, plusz
            # egy hétnyi insight-sor beolvasása — mondjuk meg előre, hogy ne
            # tűnjön beragadtnak.
            await interaction.followup.send(
                "⏳ Heti riport indul minden jogosult ügyfélre — ez több percig "
                "is tarthat. Az eredmény itt fog megjelenni."
            )

        started = time.monotonic()
        try:
            stats = await scheduler_mod.generate_weekly_action_reports(
                client_id=client_id,
            )
        except Exception as exc:  # noqa: BLE001 — a parancs ne haljon néma hibával
            log.exception("Manuális heti riport hiba (ügyfél=%s)", scope)
            await self._reply(
                interaction,
                f"❌ A heti riport elhasalt: `{exc}`\n"
                f"A részletes stack trace a Railway logban van.",
            )
            return

        elapsed = time.monotonic() - started

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "weekly_report_now",
            entity_type="client",
            entity_id=client_id,
            details={
                "scope": scope,
                "week_label": stats.get("week_label"),
                "total": stats.get("total"),
                "success": stats.get("success"),
                "failed": stats.get("failed"),
            },
        )

        await self._reply(
            interaction, _format_report(stats, scope=scope, elapsed_s=elapsed)
        )

    @weekly_now.autocomplete("client")
    async def weekly_now_client_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        rows = await asyncio.to_thread(clients_storage.search_clients, current, active=None)
        return [
            app_commands.Choice(name=r["name"][:100], value=str(r["id"]))
            for r in rows
        ][:25]

    # ------------------------------------------------------------------

    async def _reply(self, interaction: discord.Interaction, content: str) -> None:
        await reply_or_channel(interaction, content, logger=log, what="Heti riport")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReportsCog(bot))
