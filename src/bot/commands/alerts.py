"""
/alert slash parancscsoport — kampány-némítás + routing-teszt.

Parancsok:
    /alert mute    campaign:<név|#id> [hours:<2>]           — kampány némítása X órára
    /alert unmute  campaign:<név|#id>                        — némítás korai feloldása
    /alert test    campaign:<név|#id> severity:<…>          — fake riasztás → routing teszt

A `campaign` mező névvel (autocomplete, `Kliens / Kampány`) és #id-vel is működik
(backward compat): a `campaigns_storage.resolve_campaign` oldja fel.

A némítás a `mutes` táblán keresztül történik (src.storage.mutes). Muted
kampányra a detektor nem generál, az alert-router nem küld riasztást.
Minden Supabase hívás asyncio.to_thread()-ben fut (event loop védelme).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from src.config import get_config
from src.integrations import discord_router
from src.monitoring import router as alert_router
from src.monitoring import summary as summary_gen
from src.storage import audit
from src.storage import campaigns as campaigns_storage
from src.storage import mutes as mutes_storage
from src.storage import users as users_storage
from src.utils.logging import get_logger

log = get_logger(__name__)

_DEFAULT_MUTE_HOURS = 2
_MAX_MUTE_HOURS = 24 * 30  # 30 nap felső korlát (elgépelés-védelem)


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


class AlertsCog(commands.GroupCog, group_name="alert"):
    """Az `alert` parancscsoport (némítás)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Kampány-feloldás + autocomplete (admin: bármelyik fiók)
    # ------------------------------------------------------------------
    async def _resolve_campaign_or_reject(
        self, interaction: discord.Interaction, campaign: str
    ) -> dict | None:
        """A `campaign` mező feloldása kampány-sorra (#id VAGY név).

        Egyértelmű találatnál a kampány sort adja vissza; nem-talált / többértelmű
        esetben üzen és None-t ad vissza (a hívó ekkor return-öljön).
        """
        resolved = await asyncio.to_thread(campaigns_storage.resolve_campaign, campaign)
        if resolved is None:
            await interaction.followup.send(
                f"Nincs ilyen kampány: **{campaign}** — válassz az autocomplete-ből."
            )
            return None
        if resolved.get("ambiguous"):
            lines = [f"**#{m['id']}** — {m['name']}" for m in resolved["matches"]]
            await interaction.followup.send(
                "Több kampány illeszkedik — pontosíts vagy válassz az autocomplete-ből:\n"
                + "\n".join(lines)
            )
            return None
        return resolved

    async def _campaign_choices_global(self, current: str) -> list[app_commands.Choice[str]]:
        """Autocomplete az `/alert …` kampány-mezőihez: minden fiók, `Kliens / Kampány`
        címkével. Legalább 2 karakter (vagy id) kell, hogy ne listázzunk mindent."""
        q = (current or "").strip()
        if len(q) < 2 and not q.isdigit():
            return []
        rows = await asyncio.to_thread(campaigns_storage.search_campaign_choices_global, q)
        out: list[app_commands.Choice[str]] = []
        for r in rows:
            client = ((r.get("ad_accounts") or {}).get("clients")) or {}
            cname = client.get("name") or "?"
            out.append(
                app_commands.Choice(
                    name=f"{cname} / {r['name']}"[:100], value=str(r["id"])
                )
            )
        return out[:25]

    # ------------------------------------------------------------------
    # /alert mute campaign:<név|#id> hours:<2>
    # ------------------------------------------------------------------
    @app_commands.command(name="mute", description="Kampány némítása X órára (nem jelez riasztást)")
    @app_commands.describe(
        campaign="A kampány neve (autocomplete) VAGY #id",
        hours="Hány órára némítsd (alapértelmezés: 2)",
    )
    async def mute(
        self,
        interaction: discord.Interaction,
        campaign: str,
        hours: int = _DEFAULT_MUTE_HOURS,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if hours <= 0 or hours > _MAX_MUTE_HOURS:
            await interaction.followup.send(
                f"Az órák számának 1 és {_MAX_MUTE_HOURS} között kell lennie."
            )
            return

        c = await self._resolve_campaign_or_reject(interaction, campaign)
        if c is None:
            return
        campaign_id = c["id"]

        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        try:
            mute_row = await asyncio.to_thread(
                mutes_storage.mute_campaign,
                campaign_id,
                until,
                reason=None,
                created_by_discord_user_id=str(interaction.user.id),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Hiba a némításkor (#%s)", campaign_id)
            await interaction.followup.send(f"Hiba: `{exc}`")
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "mute",
            entity_type="campaign",
            entity_id=campaign_id,
            details={
                "campaign_name": c["name"],
                "hours": hours,
                "muted_until": until.isoformat(),
                "mute_id": mute_row["id"],
            },
        )

        log.info("Kampány némítva: #%s %s (%d óra)", campaign_id, c["name"], hours)
        await interaction.followup.send(
            f"🔇 **#{campaign_id} — {c['name']}** némítva **{hours} órára**.\n"
            f"Eddig: `{until.strftime('%Y-%m-%d %H:%M')} UTC` — addig nem megy riasztás.\n"
            f"Korai feloldás: `/alert unmute campaign:{campaign_id}`"
        )

    @mute.autocomplete("campaign")
    async def mute_campaign_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._campaign_choices_global(current)

    # ------------------------------------------------------------------
    # /alert unmute campaign:<név|#id>
    # ------------------------------------------------------------------
    @app_commands.command(name="unmute", description="Kampány némításának feloldása")
    @app_commands.describe(campaign="A kampány neve (autocomplete) VAGY #id")
    async def unmute(
        self,
        interaction: discord.Interaction,
        campaign: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        c = await self._resolve_campaign_or_reject(interaction, campaign)
        if c is None:
            return
        campaign_id = c["id"]

        unmuted = await asyncio.to_thread(mutes_storage.unmute_campaign, campaign_id)
        if not unmuted:
            await interaction.followup.send(
                f"**#{campaign_id} — {c['name']}** nem volt némítva."
            )
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "unmute",
            entity_type="campaign",
            entity_id=campaign_id,
            details={"campaign_name": c["name"]},
        )

        log.info("Kampány némítása feloldva: #%s %s", campaign_id, c["name"])
        await interaction.followup.send(
            f"🔔 **#{campaign_id} — {c['name']}** némítása feloldva — újra figyeljük."
        )

    @unmute.autocomplete("campaign")
    async def unmute_campaign_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._campaign_choices_global(current)

    # ------------------------------------------------------------------
    # /alert test campaign:<név|#id> severity:<critical|warning|insight>
    # ------------------------------------------------------------------
    @app_commands.command(
        name="test",
        description="Fake riasztás kiküldése a routing ellenőrzéséhez (admin csatorna)",
    )
    @app_commands.describe(
        campaign="A kampány neve (autocomplete) VAGY #id",
        severity="A teszt-riasztás súlyossága",
        force="paused/ended kampányra is küldjön (admin override) — alap: nem",
    )
    @app_commands.choices(
        severity=[
            app_commands.Choice(name="🔴 critical", value="critical"),
            app_commands.Choice(name="🟡 warning", value="warning"),
            app_commands.Choice(name="🔵 insight", value="insight"),
        ]
    )
    async def test(
        self,
        interaction: discord.Interaction,
        campaign: str,
        severity: app_commands.Choice[str],
        force: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send(
                "Ez a parancs csak az admin csatornában használható."
            )
            return

        c = await self._resolve_campaign_or_reject(interaction, campaign)
        if c is None:
            return
        campaign_id = c["id"]

        # Lifecycle: a leállított kampányra az éles monitoring sem küld — a teszt is
        # ezt tükrözze, hacsak az admin nem kéri kifejezetten (force:true).
        lifecycle = (c.get("lifecycle_state") or "new").lower()
        if lifecycle in ("paused", "ended") and not force:
            await interaction.followup.send(
                f"⚠️ **#{campaign_id} — {c['name']}** `{lifecycle}` állapotban van — "
                f"nem küldünk alertet (ahogy az éles monitoring sem).\n"
                f"Ha mégis tesztelni akarod a routingot: `/alert test campaign:{campaign_id} "
                f"severity:{severity.value} force:true`."
            )
            return

        fake_anomaly = {
            "campaign_id": campaign_id,
            "severity": severity.value,
            "metric": "test_alert",
            "observed_value": 9999,
            "threshold_value": 1000,
            "message": "🧪 TEST ALERT — routing ellenőrzés",
        }

        # bypass_quiet_hours: a teszt bármikor (csendes időben is) lefuthasson.
        # bypass_lifecycle=force: csak kifejezett admin-override küldjön paused/ended-re.
        result = await alert_router.route_alert(
            fake_anomaly, bypass_quiet_hours=True, bypass_lifecycle=force,
        )

        log.info(
            "Test alert kiküldve: kampány #%s severity=%s → channels=%s",
            campaign_id, severity.value, result.get("channels"),
        )

        if result.get("routed"):
            recipients = result.get("recipients") or []
            who = f"{len(recipients)} címzett" if recipients else "admin fallback"
            await interaction.followup.send(
                f"✅ Test alert elküldve — **#{campaign_id} — {c['name']}** "
                f"({severity.value}, {who})."
            )
        else:
            reason = result.get("reason") or "ismeretlen ok"
            await interaction.followup.send(
                f"⚠️ Test alert NEM ment ki (ok: `{reason}`).\n"
                f"Ellenőrizd: van-e beállítva admin/alert csatorna, "
                f"vagy némítva van-e a kampány."
            )

    @test.autocomplete("campaign")
    async def test_campaign_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._campaign_choices_global(current)


# A `/summary type:` értéke → (generátor, formázó-`kind`, emberi címke).
#
# A `type` értéke és a formázó `kind`-ja SZÁNDÉKOSAN nem mindenhol azonos: a
# "weekly" felhasználói érték megmarad (meglévő szokás), de a formázó felé
# "weekend" megy — a "workweek" a hétfő–pénteki változat.
#
# Ugyanazokat a generátorokat hívja, mint az ütemezett jobok
# (scheduler._SUMMARY_KINDS), így a manuálisan kért összefoglaló bitre azonos
# azzal, amit a scheduler küldene — a workweek péntekig való várakozás nélkül
# is tesztelhető.
_SUMMARY_TYPES: dict[str, tuple] = {
    "daily": (summary_gen.generate_daily_summary, "daily", "Napi"),
    "weekly": (summary_gen.generate_weekly_summary, "weekend", "Hétvégi"),
    "workweek": (summary_gen.generate_workweek_summary, "workweek", "Heti munkanapi"),
}


class SummaryCog(commands.Cog):
    """A `/summary` parancs — manuális napi/hétvégi/heti munkanapi összefoglaló.

    NEM az /alert csoport alatt van (top-level /summary), ezért külön cog.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="summary",
        description="Napi, hétvégi vagy heti munkanapi összefoglaló (saját, vagy admin: mindenkinek)",
    )
    @app_commands.describe(
        type="daily (tegnapi), weekly (hétvégi) vagy workweek (hétfő–péntek) — alap: daily",
        scope="me (csak nekem) vagy all (minden user — csak admin csatornában)",
    )
    @app_commands.choices(
        type=[
            app_commands.Choice(name="daily — napi (tegnap)", value="daily"),
            app_commands.Choice(name="weekly — hétvégi", value="weekly"),
            app_commands.Choice(
                name="workweek — heti munkanapi (hétfő–péntek)", value="workweek"
            ),
        ],
        scope=[
            app_commands.Choice(name="me — csak nekem", value="me"),
            app_commands.Choice(name="all — minden user (admin)", value="all"),
        ],
    )
    async def summary(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str] | None = None,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        generate, kind, kind_label = _SUMMARY_TYPES[type.value if type else "daily"]
        scope_value = scope.value if scope else "me"

        # --- scope: all (minden aktív user) — csak admin csatornában ---
        if scope_value == "all":
            if not _is_admin_channel(interaction):
                await interaction.followup.send(
                    "A `/summary scope:all` csak az admin csatornában használható."
                )
                return

            users = await asyncio.to_thread(users_storage.list_users, active_only=True)
            sent = 0
            for user in users:
                try:
                    summary = await generate(user["id"])
                    res = await discord_router.send_summary_to_user(
                        user, summary, kind=kind
                    )
                    if res:
                        sent += 1
                except Exception as exc:  # noqa: BLE001
                    log.error("Summary (all) hiba user #%s: %s", user.get("id"), exc)
            await interaction.followup.send(
                f"✅ {kind_label} összefoglaló elküldve **{sent}/{len(users)}** usernek."
            )
            return

        # --- scope: me (saját) ---
        user_row, _ = await asyncio.to_thread(
            users_storage.get_or_create_user,
            str(interaction.user.id),
            interaction.user.display_name or interaction.user.name,
        )
        summary = await generate(user_row["id"])
        res = await discord_router.send_summary_to_user(
            user_row, summary, kind=kind
        )
        if res:
            await interaction.followup.send(
                f"✅ {kind_label} összefoglaló elküldve a csatornádba."
            )
        else:
            await interaction.followup.send(
                f"⚠️ {kind_label} összefoglaló NEM ment ki — nincs feloldható alert "
                f"csatornád (állítsd be: `/user set-channel`), és admin fallback sem elérhető."
            )


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point."""
    await bot.add_cog(AlertsCog(bot))
    await bot.add_cog(SummaryCog(bot))
