"""
/my slash parancscsoport — csatorna-szkópolt OM parancsok (22. lépés).

Az OM a SAJÁT #alerts csatornájából kezelheti a hozzá rendelt fiókokat: KPI,
némítás, lifecycle, insight kapcsoló, összefoglaló. A csatorna azonosítja az
"aktuális OM"-et: az a user, akinek az `alerts_channel_id`-ja az adott csatorna.

Parancsok:
    /my accounts                                   — a saját fiókjaim
    /my kpi       account:<#id> [KPI mezők...]      — fiók-KPI (csak saját fiók)
    /my mute      account:<#id> [campaign_id:] hours:<>  — némítás
    /my unmute    account:<#id> [campaign_id:]      — némítás feloldása
    /my lifecycle account:<#id> campaign:<név|#id> state:<>  — lifecycle állítás
    /my insights  account:<#id> enabled:<bool>      — insight kapcsoló
    /my summary   [type:daily|weekly]               — saját összefoglaló

Jogosultság: minden parancs CSAK az OM-hez rendelt fiók(ok)ra hat. Ha a
csatornához nincs OM rendelve (pl. admin-config csatorna), a parancs barátságos
üzenettel elutasít — az admin műveletek az `/account …` / `/client …` alatt
maradnak.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from src.integrations import discord_router
from src.monitoring import summary as summary_gen
from src.storage import account_assignments as account_assignments_storage
from src.storage import ad_account_kpis as ad_account_kpis_storage
from src.storage import audit
from src.storage import campaigns as campaigns_storage
from src.storage import clients as clients_storage
from src.storage import kpis as kpis_storage
from src.storage import mutes as mutes_storage
from src.storage import users as users_storage
from src.utils.logging import get_logger

log = get_logger(__name__)

_LIFECYCLE_CHOICES = [
    app_commands.Choice(name="🆕 new", value="new"),
    app_commands.Choice(name="📚 learning", value="learning"),
    app_commands.Choice(name="✅ mature", value="mature"),
    app_commands.Choice(name="⏸ paused", value="paused"),
    app_commands.Choice(name="🔴 ended", value="ended"),
]


# ---------------------------------------------------------------------------
# Segédfüggvények
# ---------------------------------------------------------------------------

def _money(v: object) -> str:
    try:
        return f"{int(float(v)):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def _pct_int(v: object) -> str:
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else f"{f:g}"
    except (TypeError, ValueError):
        return str(v)


def _short_account(ext_id: object) -> str:
    s = str(ext_id)
    return s if len(s) <= 11 else s[:8] + "…"


def _channel_owner(channel_id: object) -> dict | None:
    """Az aktuális csatorna OM-je (users.alerts_channel_id alapján). None ha nincs."""
    return users_storage.get_user_by_alerts_channel(str(channel_id))


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class MyCommandsCog(commands.GroupCog, group_name="my"):
    """A `my` parancscsoport — csatorna-szkópolt OM műveletek."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _owner_or_reject(self, interaction: discord.Interaction) -> dict | None:
        """Az aktuális csatorna OM-je, vagy None + barátságos elutasítás."""
        owner = await asyncio.to_thread(_channel_owner, interaction.channel_id)
        if owner is None:
            await interaction.followup.send(
                "❌ Ehhez a csatornához nincs OM rendelve, ezért a `/my` parancsok itt "
                "nem használhatók.\nFuttasd a saját #alerts csatornádból (állítsd be: "
                "`/user set-channel`), vagy használd az admin parancsokat (`/account …`)."
            )
        return owner

    async def _owned_account_or_reject(
        self, interaction: discord.Interaction, owner: dict, account: str
    ) -> dict | None:
        """Feloldja az `account` paramétert (#id / külső azonosító / kliensnév) EGY
        fiókra, amely az OM-hez van rendelve.

        not_found / nem-saját / többértelmű esetén üzen és None-t ad vissza (a hívó
        ekkor return-öljön).
        """
        res = await asyncio.to_thread(account_assignments_storage.resolve_accounts, account)
        if res["status"] == "not_found":
            await interaction.followup.send(
                f"❌ Nem található fiók vagy kliens: **{account}** — nézd meg: `/my accounts`"
            )
            return None
        owned = await asyncio.to_thread(
            account_assignments_storage.get_account_ids_for_user, owner["id"]
        )
        mine = [a for a in res["accounts"] if a["id"] in owned]
        if not mine:
            await interaction.followup.send(
                "❌ Ez a fiók nincs hozzád rendelve — nézd meg: `/my accounts`"
            )
            return None
        if len(mine) > 1:
            lines = [
                f"**#{a['id']}** `{a['platform']}` `{_short_account(a['external_account_id'])}`"
                for a in mine
            ]
            await interaction.followup.send(
                "Több hozzád rendelt fiók illeszkedik:\n" + "\n".join(lines)
                + "\nAdd meg a konkrét **#id**-t (vagy válassz az autocomplete-ből)."
            )
            return None
        return mine[0]

    async def _owned_account_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete a `/my` parancsok `account` mezőjéhez: CSAK az aktuális
        csatorna OM-jéhez rendelt (aktív) fiókok, a beírt szövegre szűrve.

        Az OM SAJÁT fiókjaiból építkezünk (nem a globális `search_account_choices`-
        ból), így a lista akkor is teljes, ha a kliens ABC-rendben hátul van. A
        szűrés kliensnévre, külső azonosítóra és #id-re is illeszt; üres input az
        összes saját fiókot mutatja.
        """
        owner = await asyncio.to_thread(_channel_owner, interaction.channel_id)
        if owner is None:
            return []
        accounts = await asyncio.to_thread(
            account_assignments_storage.get_accounts_for_user, owner["id"]
        )
        if not accounts:
            return []
        clients = await asyncio.to_thread(clients_storage.list_clients)
        names = {c["id"]: c["name"] for c in clients}

        q = (current or "").strip().lower()
        choices: list[app_commands.Choice[str]] = []
        for a in sorted(accounts, key=lambda x: x["id"]):
            if not a.get("is_active", True):
                continue
            cname = names.get(a["client_id"], f"#{a['client_id']}")
            if q and q not in f"{cname} {a['external_account_id']} {a['id']}".lower():
                continue
            label = f"{cname} ({a['platform']} · {_short_account(a['external_account_id'])})"
            choices.append(app_commands.Choice(name=label[:100], value=str(a["id"])))
            if len(choices) >= 25:
                break
        return choices

    # ------------------------------------------------------------------
    # /my accounts
    # ------------------------------------------------------------------
    @app_commands.command(name="accounts", description="A hozzád rendelt hirdetési fiókok")
    async def accounts(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        owner = await self._owner_or_reject(interaction)
        if owner is None:
            return

        accounts = await asyncio.to_thread(
            account_assignments_storage.get_accounts_for_user, owner["id"]
        )
        if not accounts:
            await interaction.followup.send(
                "Nincs hozzád rendelt fiók. Szólj az adminnak: `/account assign`."
            )
            return

        kpi_ids = await asyncio.to_thread(ad_account_kpis_storage.list_account_ids_with_kpis)
        client_name_cache: dict[int, str] = {}

        lines: list[str] = []
        for a in sorted(accounts, key=lambda x: x["id"]):
            cid = a["client_id"]
            if cid not in client_name_cache:
                client = await asyncio.to_thread(clients_storage.get_client, cid)
                client_name_cache[cid] = (client or {}).get("name", f"#{cid}")
            n_camp = sum(
                1 for cm in await asyncio.to_thread(
                    campaigns_storage.get_campaigns_by_ad_account, a["id"]
                ) if cm.get("lifecycle_state") != "ended"
            )
            kpi_emoji = "✅" if a["id"] in kpi_ids else "❌"
            inactive = " · ⏸ inaktív" if not a.get("is_active", True) else ""
            lines.append(
                f"**#{a['id']}**  {client_name_cache[cid]} · `{a['platform']}` · "
                f"`{_short_account(a['external_account_id'])}` · {n_camp} kampány · "
                f"KPI {kpi_emoji}{inactive}"
            )

        embed = discord.Embed(
            title="📋 Fiókjaid",
            description="\n".join(lines),
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"OM: {owner.get('display_name') or owner['id']} · {len(accounts)} fiók")
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # /my kpi account:<#id> [KPI mezők...]
    # ------------------------------------------------------------------
    @app_commands.command(name="kpi", description="A saját fiókod KPI-jának beállítása")
    @app_commands.describe(
        account="Saját fiók: #id, külső azonosító VAGY kliensnév (a `/my accounts` mutatja)",
        target_roas="Cél ROAS szorzó (pl. 3.0 = 3×)",
        max_cpa="Max CPA (Ft)",
        monthly_budget="Havi büdzsé (Ft)",
        warning_pct="Warning küszöb százalék (alap: 20)",
        critical_pct="Critical küszöb százalék (alap: 40)",
        target_roi="Cél ROI",
        max_cpl="Max CPL (Ft)",
        target_ctr="Cél CTR (%)",
        max_cpc="Max CPC (Ft)",
        primary_conversion_event="Elsődleges konverzió esemény (pl. Purchase)",
    )
    async def kpi(
        self,
        interaction: discord.Interaction,
        account: str,
        target_roas: float | None = None,
        max_cpa: float | None = None,
        monthly_budget: float | None = None,
        warning_pct: float | None = None,
        critical_pct: float | None = None,
        target_roi: float | None = None,
        max_cpl: float | None = None,
        target_ctr: float | None = None,
        max_cpc: float | None = None,
        primary_conversion_event: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        owner = await self._owner_or_reject(interaction)
        if owner is None:
            return
        acct = await self._owned_account_or_reject(interaction, owner, account)
        if acct is None:
            return

        inputs = {k: v for k, v in {
            "target_roas": target_roas, "max_cpa": max_cpa, "monthly_budget": monthly_budget,
            "warning_pct": warning_pct, "critical_pct": critical_pct, "target_roi": target_roi,
            "max_cpl": max_cpl, "target_ctr": target_ctr, "max_cpc": max_cpc,
            "primary_conversion_event": primary_conversion_event,
        }.items() if v is not None}
        if not inputs:
            await interaction.followup.send("❌ Adj meg legalább egy KPI mezőt.")
            return

        cname = (await asyncio.to_thread(clients_storage.get_client, acct["client_id"]) or {}).get("name", "?")

        try:
            row = await asyncio.to_thread(
                ad_account_kpis_storage.upsert_ad_account_kpis, acct["id"], fields=inputs
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("/my kpi mentés hiba (#%s)", acct["id"])
            await interaction.followup.send(
                f"❌ Nem sikerült a KPI mentése: `{exc}`\n"
                f"Lehet, hogy a `0010_account_level.sql` migráció még nem futott le."
            )
            return

        cascade_values = {
            f: row[f] for f in ad_account_kpis_storage.CASCADE_FIELDS if row.get(f) is not None
        }
        campaign_ids = [
            cm["id"]
            for cm in await asyncio.to_thread(campaigns_storage.get_campaigns_by_ad_account, acct["id"])
            if cm.get("lifecycle_state") != "ended"
        ]
        try:
            casc = await asyncio.to_thread(
                kpis_storage.cascade_account_kpis_to_campaigns,
                campaign_ids, values=cascade_values,
                set_by_discord_user_id=str(interaction.user.id),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("/my kpi kaszkád hiba (#%s)", acct["id"])
            await interaction.followup.send(f"⚠️ A KPI elmentve, de a kaszkád hibázott: `{exc}`")
            return

        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "my_kpi",
            entity_type="ad_account", entity_id=acct["id"],
            details={"owner_id": owner["id"], "client_name": cname,
                     "platform": acct["platform"], "fields": inputs, "cascade": casc},
        )

        written = casc["updated"] + casc["inserted"]
        bits: list[str] = []
        if row.get("target_roas") is not None:
            bits.append(f"ROAS {_pct_int(row['target_roas'])}")
        if row.get("max_cpa") is not None:
            bits.append(f"Max CPA {_money(row['max_cpa'])} Ft")
        if row.get("monthly_budget") is not None:
            bits.append(f"Büdzsé {_money(row['monthly_budget'])} Ft")
        if row.get("target_ctr") is not None:
            bits.append(f"CTR {_pct_int(row['target_ctr'])}%")
        if row.get("max_cpc") is not None:
            bits.append(f"Max CPC {_money(row['max_cpc'])} Ft")
        if row.get("max_cpl") is not None:
            bits.append(f"Max CPL {_money(row['max_cpl'])} Ft")
        thr = f"W-{_pct_int(row.get('warning_pct'))}% / C-{_pct_int(row.get('critical_pct'))}%"
        await interaction.followup.send(
            f"✅ KPI beállítva: **{cname} · {acct['platform']}** ({written} kampány)\n"
            f"{' | '.join(bits)} | {thr}"
        )

    @kpi.autocomplete("account")
    async def kpi_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._owned_account_choices(interaction, current)

    # ------------------------------------------------------------------
    # /my mute account:<#id> [campaign_id:<>] hours:<>
    # ------------------------------------------------------------------
    @app_commands.command(name="mute", description="Fiók vagy kampány némítása X órára")
    @app_commands.describe(
        account="Saját fiók: #id, külső azonosító VAGY kliensnév (a `/my accounts` mutatja)",
        hours="Hány órára némítsd",
        campaign_id="Opcionális: csak ez a kampány (egyébként a fiók összes kampánya)",
    )
    async def mute(
        self,
        interaction: discord.Interaction,
        account: str,
        hours: int,
        campaign_id: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        owner = await self._owner_or_reject(interaction)
        if owner is None:
            return
        acct = await self._owned_account_or_reject(interaction, owner, account)
        if acct is None:
            return
        if hours <= 0:
            await interaction.followup.send("❌ Az órák száma legyen pozitív.")
            return

        targets = await self._campaign_targets(interaction, acct, campaign_id)
        if targets is None:
            return

        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        for cm in targets:
            await asyncio.to_thread(
                mutes_storage.mute_campaign, cm["id"], until,
                reason="OM némítás (/my mute)",
                created_by_discord_user_id=str(interaction.user.id),
            )
        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "my_mute",
            entity_type="ad_account", entity_id=acct["id"],
            details={"owner_id": owner["id"], "campaigns": [cm["id"] for cm in targets], "hours": hours},
        )
        scope = f"#{campaign_id} kampány" if campaign_id else f"a fiók {len(targets)} kampánya"
        await interaction.followup.send(
            f"🔇 Némítva {hours} órára ({scope}) — eddig: {until.strftime('%Y-%m-%d %H:%M')} UTC"
        )

    @mute.autocomplete("account")
    async def mute_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._owned_account_choices(interaction, current)

    # ------------------------------------------------------------------
    # /my unmute account:<#id> [campaign_id:<>]
    # ------------------------------------------------------------------
    @app_commands.command(name="unmute", description="Némítás korai feloldása")
    @app_commands.describe(
        account="Saját fiók: #id, külső azonosító VAGY kliensnév (a `/my accounts` mutatja)",
        campaign_id="Opcionális: csak ez a kampány (egyébként a fiók összes kampánya)",
    )
    async def unmute(
        self,
        interaction: discord.Interaction,
        account: str,
        campaign_id: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        owner = await self._owner_or_reject(interaction)
        if owner is None:
            return
        acct = await self._owned_account_or_reject(interaction, owner, account)
        if acct is None:
            return

        targets = await self._campaign_targets(interaction, acct, campaign_id)
        if targets is None:
            return

        unmuted = 0
        for cm in targets:
            if await asyncio.to_thread(mutes_storage.unmute_campaign, cm["id"]):
                unmuted += 1
        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "my_unmute",
            entity_type="ad_account", entity_id=acct["id"],
            details={"owner_id": owner["id"], "unmuted": unmuted},
        )
        await interaction.followup.send(f"🔔 Feloldva {unmuted} kampány némítása.")

    @unmute.autocomplete("account")
    async def unmute_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._owned_account_choices(interaction, current)

    # ------------------------------------------------------------------
    # /my lifecycle account:<#id> campaign_id:<> state:<>
    # ------------------------------------------------------------------
    @app_commands.command(name="lifecycle", description="Kampány lifecycle állapotának állítása")
    @app_commands.describe(
        account="Saját fiók: #id, külső azonosító VAGY kliensnév (a `/my accounts` mutatja)",
        campaign="A kampány neve (autocomplete) VAGY #id",
        state="Új lifecycle állapot",
    )
    @app_commands.choices(state=_LIFECYCLE_CHOICES)
    async def lifecycle(
        self,
        interaction: discord.Interaction,
        account: str,
        campaign: str,
        state: app_commands.Choice[str],
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        owner = await self._owner_or_reject(interaction)
        if owner is None:
            return
        acct = await self._owned_account_or_reject(interaction, owner, account)
        if acct is None:
            return

        resolved = await asyncio.to_thread(
            campaigns_storage.resolve_campaign, campaign, acct["id"]
        )
        if resolved is None:
            await interaction.followup.send(
                f"❌ Nem található kampány: **{campaign}** — válassz az autocomplete-ből."
            )
            return
        if resolved.get("ambiguous"):
            lines = [f"**#{m['id']}** {m['name']}" for m in resolved["matches"]]
            await interaction.followup.send(
                "Több kampány illeszkedik — pontosíts vagy válassz az autocomplete-ből:\n"
                + "\n".join(lines)
            )
            return

        camp = resolved
        campaign_id = camp["id"]
        if camp.get("ad_account_id") != acct["id"]:
            await interaction.followup.send(
                f"❌ A(z) #{campaign_id} — {camp['name']} kampány nem ehhez a fiókhoz "
                f"(#{acct['id']}) tartozik."
            )
            return

        old_state = camp.get("lifecycle_state", "?")
        updated = await asyncio.to_thread(
            campaigns_storage.set_lifecycle_state, campaign_id, state.value
        )
        if updated is None:
            await interaction.followup.send(f"❌ Nem sikerült frissíteni: #{campaign_id}")
            return

        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "my_lifecycle",
            entity_type="campaign", entity_id=campaign_id,
            details={"owner_id": owner["id"], "old_state": old_state, "new_state": state.value},
        )
        await interaction.followup.send(
            f"✅ **#{campaign_id} — {camp['name']}** lifecycle: `{old_state}` → `{state.value}`"
        )

    @lifecycle.autocomplete("account")
    async def lifecycle_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._owned_account_choices(interaction, current)

    @lifecycle.autocomplete("campaign")
    async def lifecycle_campaign_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Kampány-autocomplete: a már kiválasztott (saját) fiók nem-'ended' kampányai.

        A fiókot a `account` mezőből olvassuk (namespace); ha még nincs megadva,
        vagy nem az OM-hez tartozik, üres listát adunk (előbb fiókot kell választani).
        """
        owner = await asyncio.to_thread(_channel_owner, interaction.channel_id)
        if owner is None:
            return []
        account_val = interaction.namespace.account
        if not account_val:
            return []

        res = await asyncio.to_thread(
            account_assignments_storage.resolve_accounts, str(account_val)
        )
        if res["status"] != "ok" or not res.get("accounts"):
            return []
        owned = await asyncio.to_thread(
            account_assignments_storage.get_account_ids_for_user, owner["id"]
        )
        mine = [a for a in res["accounts"] if a["id"] in owned]
        if len(mine) != 1:
            return []
        account_id = mine[0]["id"]

        rows = await asyncio.to_thread(
            campaigns_storage.search_campaign_choices, account_id, current or ""
        )
        return [
            app_commands.Choice(name=c["name"][:100], value=str(c["id"]))
            for c in rows
        ][:25]

    # ------------------------------------------------------------------
    # /my insights account:<#id> enabled:<bool>
    # ------------------------------------------------------------------
    @app_commands.command(name="insights", description="Insight (AI-elemzés) kapcsoló")
    @app_commands.describe(
        account="Saját fiók: #id, külső azonosító VAGY kliensnév (a `/my accounts` mutatja)",
        enabled="true = bekapcsolva, false = kikapcsolva",
    )
    async def insights(
        self,
        interaction: discord.Interaction,
        account: str,
        enabled: bool,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        owner = await self._owner_or_reject(interaction)
        if owner is None:
            return
        acct = await self._owned_account_or_reject(interaction, owner, account)
        if acct is None:
            return

        # Megjegyzés: az insight-kapcsoló jelenleg KLIENS-szintű (clients.insights_enabled);
        # nincs külön fiók-szintű oszlop, ezért a fiók ügyfelén állítjuk.
        client = await asyncio.to_thread(clients_storage.get_client, acct["client_id"])
        if client is None:
            await interaction.followup.send("❌ A fiók ügyfele nem található.")
            return
        try:
            await asyncio.to_thread(clients_storage.set_insights_enabled, client["id"], enabled)
        except Exception as exc:  # noqa: BLE001
            log.exception("/my insights hiba (client #%s)", client["id"])
            await interaction.followup.send(f"❌ Nem sikerült a kapcsoló állítása: `{exc}`")
            return

        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "my_insights",
            entity_type="client", entity_id=client["id"],
            details={"owner_id": owner["id"], "ad_account_id": acct["id"], "insights_enabled": enabled},
        )
        state = "🟢 bekapcsolva" if enabled else "🔴 kikapcsolva"
        await interaction.followup.send(
            f"✅ Insights {state}: **{client['name']}** "
            f"*(kliens-szintű kapcsoló — a kliens minden fiókjára érvényes)*"
        )

    @insights.autocomplete("account")
    async def insights_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._owned_account_choices(interaction, current)

    # ------------------------------------------------------------------
    # /my summary [type:daily|weekly]
    # ------------------------------------------------------------------
    @app_commands.command(name="summary", description="A saját összefoglalód (csak a te fiókjaid)")
    @app_commands.describe(type="daily (tegnapi) vagy weekly (hétvégi) — alap: daily")
    @app_commands.choices(type=[
        app_commands.Choice(name="daily — napi (tegnap)", value="daily"),
        app_commands.Choice(name="weekly — hétvégi", value="weekly"),
    ])
    async def summary(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        owner = await self._owner_or_reject(interaction)
        if owner is None:
            return

        is_weekly = bool(type and type.value == "weekly")
        kind = "Hétvégi" if is_weekly else "Napi"
        if is_weekly:
            summary = await summary_gen.generate_weekly_summary(owner["id"])
        else:
            summary = await summary_gen.generate_daily_summary(owner["id"])

        res = await discord_router.send_summary_to_user(owner, summary, is_weekly=is_weekly)
        if res:
            await interaction.followup.send(f"✅ {kind} összefoglaló elküldve a csatornádba.")
        else:
            await interaction.followup.send(
                f"⚠️ A {kind.lower()} összefoglaló NEM ment ki — nincs feloldható csatorna."
            )

    # ------------------------------------------------------------------
    # Belső: a célzott kampányok (egy kampány vagy a fiók összes kampánya)
    # ------------------------------------------------------------------
    async def _campaign_targets(
        self,
        interaction: discord.Interaction,
        acct: dict,
        campaign_id: int | None,
    ) -> list[dict] | None:
        """A művelet célkampányai. Egy konkrét kampány (a fiókhoz tartoznia kell),
        vagy a fiók összes nem-ended kampánya. None + elutasítás, ha hibás."""
        if campaign_id is not None:
            camp = await asyncio.to_thread(campaigns_storage.get_campaign, campaign_id)
            if camp is None or camp.get("ad_account_id") != acct["id"]:
                await interaction.followup.send(
                    f"❌ A(z) #{campaign_id} kampány nem ehhez a fiókhoz (#{acct['id']}) tartozik."
                )
                return None
            return [camp]
        camps = await asyncio.to_thread(campaigns_storage.get_campaigns_by_ad_account, acct["id"])
        targets = [cm for cm in camps if cm.get("lifecycle_state") != "ended"]
        if not targets:
            await interaction.followup.send("ℹ️ Ennek a fióknak nincs aktív kampánya.")
            return None
        return targets


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(MyCommandsCog(bot))
