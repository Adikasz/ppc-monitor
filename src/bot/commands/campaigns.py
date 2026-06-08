"""
/campaign slash parancscsoport.

Kampányok létrehozása, lekérdezése, KPI-ok beállítása, lifecycle kezelés
és helyettes OM (supporter) hozzárendelés.

Parancsok:
    /campaign list   client_name:<név>                     — ügyfél kampányainak listázása
    /campaign info   campaign_id:<id>                      — egy kampány részletei + KPI-ok
    /campaign add    client_name:<név> platform:<meta|google> name:<...> [account_id] [type]
                                                           — új kampány létrehozása
    /campaign tag    campaign_id:<id> type:<típus>         — kampány típus beállítása
    /campaign kpi    campaign_id:<id> [KPI mezők...]       — KPI-ok beállítása / frissítése
    /campaign set-state  campaign_id:<id> state:<állapot> [until:<dátum>]
                                                           — lifecycle állapot beállítása
    /campaign add-supporter     campaign_id:<id> supporter:<@user>
                                                           — helyettes OM hozzáadása
    /campaign remove-supporter  campaign_id:<id> supporter:<@user>
                                                           — helyettes OM eltávolítása

Megjegyzések:
  - Az írási parancsok az admin csatornára vannak korlátozva (ha konfigurálva).
  - A /campaign list és info bárhonnan hívható (ephemeral válasz).
  - A supporter hozzárendelés az assignments táblán keresztül kezelt (role='supporter').
  - KPI beállítás verziózott — minden /campaign kpi hívás új sort hoz létre.
  - Minden Supabase hívás asyncio.to_thread()-ben fut, hogy ne blokkolja az event loop-ot.
"""
from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from src.config import get_config
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import campaigns as campaigns_storage
from src.storage import clients as clients_storage
from src.storage import kpis as kpis_storage
from src.storage import users as users_storage
from src.storage import audit
from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

log = get_logger(__name__)

# Érvényes lifecycle állapotok (Discord choice-okhoz)
_LIFECYCLE_STATES = ["new", "learning", "mature", "paused", "ended"]
_PLATFORMS = ["meta", "google"]


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
    """True, ha az interakció az admin csatornában történt (vagy nincs konfigurálva)."""
    admin = _admin_channel_id()
    if admin is None:
        return True
    return interaction.channel_id == admin


def _display_name(member: discord.Member | discord.User) -> str:
    """Emberi olvasható név Discord member/user objektumból."""
    if isinstance(member, discord.Member) and member.nick:
        return member.nick
    return member.display_name or member.name


def _lifecycle_emoji(state: str | None) -> str:
    """Lifecycle állapot → emoji."""
    return {
        "new": "🆕",
        "learning": "📚",
        "mature": "✅",
        "paused": "⏸",
        "ended": "🔴",
    }.get(state or "", "❓")


def _fmt_kpi(label: str, value: object, unit: str = "") -> str:
    """KPI mező formázása megjelenítéshez. None → '—'."""
    if value is None:
        return f"{label}: —"
    return f"{label}: {value}{unit}"


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class CampaignsCog(commands.GroupCog, group_name="campaign"):
    """A `campaign` parancscsoport implementációja."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /campaign list client_name:<név>
    # ------------------------------------------------------------------
    @app_commands.command(name="list", description="Ügyfél kampányainak listázása")
    @app_commands.describe(client_name="Az ügyfél neve (pontosan, ahogy a /clients list mutatja)")
    async def list_(
        self,
        interaction: discord.Interaction,
        client_name: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        client = await asyncio.to_thread(
            clients_storage.get_client_by_name, client_name.strip()
        )
        if client is None:
            await interaction.followup.send(
                f"Nem található ügyfél: **{client_name}**\n"
                f"Ellenőrizd a `/clients list` paranccsal az elérhető ügyfeleket."
            )
            return

        rows = await asyncio.to_thread(campaigns_storage.list_campaigns, client["id"])
        if not rows:
            await interaction.followup.send(
                f"**{client['name']}** ügyfélhez még nincs aktív kampány.\n"
                f"Adj hozzá egyet a `/campaign add` paranccsal."
            )
            return

        embed = discord.Embed(
            title=f"📊 {client['name']} — kampányok",
            color=discord.Color.blue(),
        )
        for c in rows:
            state = c.get("lifecycle_state", "?")
            platform = c.get("platform", "?")
            campaign_type = c.get("campaign_type") or "—"
            embed.add_field(
                name=f"#{c['id']} — {c['name']}",
                value=(
                    f"{_lifecycle_emoji(state)} `{state}` · 🖥 `{platform}`\n"
                    f"típus: `{campaign_type}`"
                ),
                inline=False,
            )
        embed.set_footer(text=f"Összesen: {len(rows)} kampány")
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # /campaign info campaign_id:<id>
    # ------------------------------------------------------------------
    @app_commands.command(name="info", description="Egy kampány részletei és aktuális KPI-ok")
    @app_commands.describe(campaign_id="A kampány numerikus azonosítója")
    async def info(
        self,
        interaction: discord.Interaction,
        campaign_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        c = await asyncio.to_thread(campaigns_storage.get_campaign, campaign_id)
        if c is None:
            await interaction.followup.send(f"Nincs ilyen kampány: #{campaign_id}")
            return

        state = c.get("lifecycle_state", "?")
        embed = discord.Embed(
            title=f"#{c['id']} — {c['name']}",
            color=discord.Color.blue() if state != "ended" else discord.Color.dark_grey(),
        )
        embed.add_field(name="Platform", value=c.get("platform") or "—")
        embed.add_field(name="Lifecycle", value=f"{_lifecycle_emoji(state)} {state}")
        embed.add_field(name="Típus", value=c.get("campaign_type") or "—")
        embed.add_field(name="Fiók ID", value=c.get("account_id") or "—")

        if c.get("lifecycle_until"):
            embed.add_field(name="Lifecycle lejár", value=str(c["lifecycle_until"]), inline=False)
        if c.get("data_valid_from"):
            embed.add_field(name="Adat érvényes ettől", value=str(c["data_valid_from"]), inline=False)

        # Aktív KPI-ok
        kpi = await asyncio.to_thread(kpis_storage.get_active_kpis, campaign_id)
        if kpi:
            kpi_lines = "\n".join(filter(None, [
                _fmt_kpi("ROAS cél", kpi.get("target_roas"), "%"),
                _fmt_kpi("Max CPA", kpi.get("max_cpa"), " Ft"),
                _fmt_kpi("Max CPL", kpi.get("max_cpl"), " Ft"),
                _fmt_kpi("Havi büdzsé", kpi.get("monthly_budget"), " Ft"),
                _fmt_kpi("CTR cél", kpi.get("target_ctr"), "%"),
                _fmt_kpi("Max CPC", kpi.get("max_cpc"), " Ft"),
                _fmt_kpi("Konverzió esemény", kpi.get("primary_conversion_event")),
                _fmt_kpi("Nincs konv. → CRITICAL", kpi.get("no_conversion_critical_days"), " nap"),
                _fmt_kpi("CPA ugrás → CRITICAL", kpi.get("cpa_spike_critical_pct"), "%"),
                _fmt_kpi("Trend warning", kpi.get("trend_warning_days"), " nap"),
            ]))
            embed.add_field(name="📈 Aktív KPI-ok", value=kpi_lines or "—", inline=False)
        else:
            embed.add_field(
                name="📈 Aktív KPI-ok",
                value="Még nincs beállítva — használd a `/campaign kpi` parancsot.",
                inline=False,
            )

        embed.add_field(name="Létrehozva", value=str(c.get("created_at", "—")), inline=False)
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # /campaign add client_name:<név> platform:<meta|google> account_id:<...>
    # Regisztrál egy hirdetési fiókot az ügyfélhez — a discovery ebből indul.
    # ------------------------------------------------------------------
    @app_commands.command(
        name="add",
        description="Hirdetési fiók regisztrálása ügyfélhez (discovery innen indul)",
    )
    @app_commands.describe(
        client_name="Az ügyfél neve",
        platform="Hirdetési platform: meta vagy google",
        account_id="Hirdetési fiók azonosítója (Meta: act_123456789, Google: 1234567890)",
        account_name="Opcionális: fiók emberi neve",
    )
    @app_commands.choices(platform=[
        app_commands.Choice(name="Meta Ads", value="meta"),
        app_commands.Choice(name="Google Ads", value="google"),
    ])
    async def add(
        self,
        interaction: discord.Interaction,
        client_name: str,
        platform: str,
        account_id: str,
        account_name: str | None = None,
    ) -> None:
        # ── DEBUG STRIP ── minden logika ki van kommentelve ──────────────
        # Ha ez a válasz megjelenik, a defer+followup lánc működik.
        # Ha nem jelenik meg → valami a bot betöltésekor már elromlott.
        await interaction.response.defer(ephemeral=True)
        log.info(
            "DEBUG /campaign add hívva: client=%r platform=%r account_id=%r",
            client_name, platform, account_id,
        )
        await interaction.followup.send(
            f"✅ DEBUG: add parancs elért a followup.send-ig\n"
            f"client={client_name!r}  platform={platform!r}  account_id={account_id!r}"
        )

    # ------------------------------------------------------------------
    # /campaign tag campaign_id:<id> type:<típus>
    # ------------------------------------------------------------------
    @app_commands.command(name="tag", description="Kampány típus (tag) beállítása")
    @app_commands.describe(
        campaign_id="A kampány numerikus azonosítója",
        campaign_type="Kampány típusa, pl. meta_conversion, google_brand_search",
    )
    async def tag(
        self,
        interaction: discord.Interaction,
        campaign_id: int,
        campaign_type: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send(
                "Ez a parancs csak az admin csatornában használható."
            )
            return

        c = await asyncio.to_thread(campaigns_storage.get_campaign, campaign_id)
        if c is None:
            await interaction.followup.send(f"Nincs ilyen kampány: #{campaign_id}")
            return

        # Supabase hívás thread-poolban
        _type = campaign_type.strip()
        try:
            def _do_tag_update() -> dict | None:
                res = (
                    get_supabase()
                    .table("campaigns")
                    .update({"campaign_type": _type})
                    .eq("id", campaign_id)
                    .execute()
                )
                return res.data[0] if res.data else None

            await asyncio.to_thread(_do_tag_update)
        except Exception as exc:  # noqa: BLE001
            log.exception("Hiba a kampány tag frissítésekor (#%s)", campaign_id)
            await interaction.followup.send(f"Hiba: `{exc}`")
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "campaign_tag",
            entity_type="campaign",
            entity_id=campaign_id,
            details={"campaign_type": campaign_type, "campaign_name": c["name"]},
        )

        log.info("Kampány típus beállítva: #%s → %s", campaign_id, campaign_type)
        await interaction.followup.send(
            f"✅ **#{campaign_id} — {c['name']}** típusa beállítva: `{campaign_type}`"
        )

    # ------------------------------------------------------------------
    # /campaign kpi campaign_id:<id> [KPI mezők...]
    # ------------------------------------------------------------------
    @app_commands.command(name="kpi", description="KPI-ok beállítása kampányhoz (verziózott)")
    @app_commands.describe(
        campaign_id="A kampány numerikus azonosítója",
        target_roas="Célzott ROAS (%)",
        max_cpa="Max megengedett CPA (Ft)",
        max_cpl="Max megengedett CPL lead kampányhoz (Ft)",
        monthly_budget="Havi büdzsé (Ft)",
        target_ctr="Célzott CTR (%)",
        max_cpc="Max CPC (Ft)",
        primary_conversion_event="Elsődleges konverzió esemény neve (pl. Purchase)",
        no_conversion_critical_days="Ennyi napja nincs konverzió → CRITICAL (default: 3)",
        cpa_spike_critical_pct="Ennyi %-os CPA-ugrás → CRITICAL (default: 50)",
        trend_warning_days="Ennyi napja csökken metrika → WARNING (default: 7)",
    )
    async def kpi(
        self,
        interaction: discord.Interaction,
        campaign_id: int,
        target_roas: float | None = None,
        max_cpa: float | None = None,
        max_cpl: float | None = None,
        monthly_budget: float | None = None,
        target_ctr: float | None = None,
        max_cpc: float | None = None,
        primary_conversion_event: str | None = None,
        no_conversion_critical_days: int | None = None,
        cpa_spike_critical_pct: float | None = None,
        trend_warning_days: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send(
                "Ez a parancs csak az admin csatornában használható."
            )
            return

        # Legalább egy KPI mezőt meg kell adni
        all_values = [
            target_roas, max_cpa, max_cpl, monthly_budget, target_ctr, max_cpc,
            primary_conversion_event, no_conversion_critical_days,
            cpa_spike_critical_pct, trend_warning_days,
        ]
        if all(v is None for v in all_values):
            await interaction.followup.send("Legalább egy KPI mezőt meg kell adni!")
            return

        c = await asyncio.to_thread(campaigns_storage.get_campaign, campaign_id)
        if c is None:
            await interaction.followup.send(f"Nincs ilyen kampány: #{campaign_id}")
            return

        try:
            new_kpi = await asyncio.to_thread(
                kpis_storage.set_kpis,
                campaign_id,
                target_roas=target_roas,
                max_cpa=max_cpa,
                max_cpl=max_cpl,
                monthly_budget=monthly_budget,
                target_ctr=target_ctr,
                max_cpc=max_cpc,
                primary_conversion_event=primary_conversion_event,
                no_conversion_critical_days=no_conversion_critical_days,
                cpa_spike_critical_pct=cpa_spike_critical_pct,
                trend_warning_days=trend_warning_days,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Hiba a KPI beállításakor (#%s)", campaign_id)
            await interaction.followup.send(f"Hiba a KPI mentésekor: `{exc}`")
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "set_kpi",
            entity_type="campaign",
            entity_id=campaign_id,
            details={
                "campaign_name": c["name"],
                "kpi_id": new_kpi["id"],
                "target_roas": target_roas,
                "max_cpa": max_cpa,
                "max_cpl": max_cpl,
                "monthly_budget": monthly_budget,
                "target_ctr": target_ctr,
                "max_cpc": max_cpc,
                "primary_conversion_event": primary_conversion_event,
                "no_conversion_critical_days": no_conversion_critical_days,
                "cpa_spike_critical_pct": cpa_spike_critical_pct,
                "trend_warning_days": trend_warning_days,
            },
        )

        log.info(
            "KPI beállítva: #%s %s → kpi #%s",
            campaign_id, c["name"], new_kpi["id"],
        )

        set_fields = [k for k, v in {
            "ROAS cél": target_roas,
            "Max CPA": max_cpa,
            "Max CPL": max_cpl,
            "Havi büdzsé": monthly_budget,
            "CTR cél": target_ctr,
            "Max CPC": max_cpc,
            "Konverzió esemény": primary_conversion_event,
            "Nincs konv. → CRITICAL": no_conversion_critical_days,
            "CPA ugrás → CRITICAL": cpa_spike_critical_pct,
            "Trend warning": trend_warning_days,
        }.items() if v is not None]

        await interaction.followup.send(
            f"✅ KPI-ok beállítva: **#{campaign_id} — {c['name']}** *(kpi #{new_kpi['id']})*\n"
            f"Frissített mezők: {', '.join(set_fields)}"
        )

    # ------------------------------------------------------------------
    # /campaign set-state campaign_id:<id> state:<állapot> [until:<dátum>]
    # ------------------------------------------------------------------
    @app_commands.command(name="set-state", description="Kampány lifecycle állapot beállítása")
    @app_commands.describe(
        campaign_id="A kampány numerikus azonosítója",
        state="Új lifecycle állapot",
        until="Opcionális lejárati dátum (YYYY-MM-DD), pl. learning → ennyi ideig tart",
    )
    @app_commands.choices(state=[
        app_commands.Choice(name="🆕 new — most indult, adatgyűjtés", value="new"),
        app_commands.Choice(name="📚 learning — tanulási fázis", value="learning"),
        app_commands.Choice(name="✅ mature — normál üzem", value="mature"),
        app_commands.Choice(name="⏸ paused — szüneteltetett", value="paused"),
        app_commands.Choice(name="🔴 ended — lezárva", value="ended"),
    ])
    async def set_state(
        self,
        interaction: discord.Interaction,
        campaign_id: int,
        state: str,
        until: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send(
                "Ez a parancs csak az admin csatornában használható."
            )
            return

        c = await asyncio.to_thread(campaigns_storage.get_campaign, campaign_id)
        if c is None:
            await interaction.followup.send(f"Nincs ilyen kampány: #{campaign_id}")
            return

        old_state = c.get("lifecycle_state", "?")

        try:
            updated = await asyncio.to_thread(
                campaigns_storage.set_lifecycle_state,
                campaign_id,
                state,
                until=until,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Hiba a lifecycle beállításakor (#%s)", campaign_id)
            await interaction.followup.send(f"Hiba: `{exc}`")
            return

        if updated is None:
            await interaction.followup.send(f"Nem sikerült frissíteni: #{campaign_id}")
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "set_lifecycle",
            entity_type="campaign",
            entity_id=campaign_id,
            details={
                "campaign_name": c["name"],
                "old_state": old_state,
                "new_state": state,
                "until": until,
            },
        )

        log.info(
            "Lifecycle beállítva: #%s %s → %s (until=%s)",
            campaign_id, c["name"], state, until,
        )

        until_str = f" · lejár: `{until}`" if until else ""
        await interaction.followup.send(
            f"{_lifecycle_emoji(state)} **#{campaign_id} — {c['name']}** lifecycle beállítva: "
            f"`{old_state}` → `{state}`{until_str}"
        )

    # ------------------------------------------------------------------
    # /campaign add-supporter campaign_id:<id> supporter:<@user>
    # ------------------------------------------------------------------
    @app_commands.command(name="add-supporter", description="Helyettes OM hozzáadása kampányhoz")
    @app_commands.describe(
        campaign_id="A kampány numerikus azonosítója",
        supporter="A hozzáadandó helyettes Discord felhasználó",
    )
    async def add_supporter(
        self,
        interaction: discord.Interaction,
        campaign_id: int,
        supporter: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send(
                "Ez a parancs csak az admin csatornában használható."
            )
            return

        c = await asyncio.to_thread(campaigns_storage.get_campaign, campaign_id)
        if c is None:
            await interaction.followup.send(f"Nincs ilyen kampány: #{campaign_id}")
            return

        # Supporter auto-regisztráció (users tábla)
        user_row, user_created = await asyncio.to_thread(
            users_storage.get_or_create_user,
            str(supporter.id),
            _display_name(supporter),
        )
        if user_created:
            log.info("Új felhasználó regisztrálva: %s (%s)", supporter, supporter.id)

        assignment, created = await asyncio.to_thread(
            campaigns_storage.add_supporter,
            campaign_id,
            user_row["id"],
            created_by_discord_user_id=str(interaction.user.id),
        )

        if not created:
            await interaction.followup.send(
                f"**{_display_name(supporter)}** már helyettes a "
                f"**#{campaign_id} — {c['name']}** kampányon."
            )
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "add_supporter",
            entity_type="campaign",
            entity_id=campaign_id,
            details={
                "campaign_name": c["name"],
                "supporter_discord_id": str(supporter.id),
                "supporter_name": _display_name(supporter),
                "assignment_id": assignment["id"],
            },
        )

        log.info(
            "Supporter hozzáadva: %s → kampány #%s %s (assignment #%s)",
            supporter, campaign_id, c["name"], assignment["id"],
        )
        await interaction.followup.send(
            f"✅ **{_display_name(supporter)}** hozzáadva helyettesként a "
            f"**#{campaign_id} — {c['name']}** kampányhoz.\n"
            f"*(assignment #{assignment['id']})*"
        )

    # ------------------------------------------------------------------
    # /campaign remove-supporter campaign_id:<id> supporter:<@user>
    # ------------------------------------------------------------------
    @app_commands.command(name="remove-supporter", description="Helyettes OM eltávolítása kampányról")
    @app_commands.describe(
        campaign_id="A kampány numerikus azonosítója",
        supporter="Az eltávolítandó helyettes Discord felhasználó",
    )
    async def remove_supporter(
        self,
        interaction: discord.Interaction,
        campaign_id: int,
        supporter: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send(
                "Ez a parancs csak az admin csatornában használható."
            )
            return

        c = await asyncio.to_thread(campaigns_storage.get_campaign, campaign_id)
        if c is None:
            await interaction.followup.send(f"Nincs ilyen kampány: #{campaign_id}")
            return

        user_row = await asyncio.to_thread(
            users_storage.get_user_by_discord_id, str(supporter.id)
        )
        if user_row is None:
            await interaction.followup.send(
                f"**{_display_name(supporter)}** nincs a rendszerben — "
                f"nem volt helyettes ezen a kampányon."
            )
            return

        deleted = await asyncio.to_thread(
            campaigns_storage.remove_supporter,
            campaign_id,
            user_row["id"],
        )

        if not deleted:
            await interaction.followup.send(
                f"**{_display_name(supporter)}** nem volt helyettes a "
                f"**#{campaign_id} — {c['name']}** kampányon."
            )
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "remove_supporter",
            entity_type="campaign",
            entity_id=campaign_id,
            details={
                "campaign_name": c["name"],
                "supporter_discord_id": str(supporter.id),
                "supporter_name": _display_name(supporter),
            },
        )

        log.info(
            "Supporter eltávolítva: %s ← kampány #%s %s",
            supporter, campaign_id, c["name"],
        )
        await interaction.followup.send(
            f"✅ **{_display_name(supporter)}** eltávolítva helyettesként a "
            f"**#{campaign_id} — {c['name']}** kampányról."
        )


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(CampaignsCog(bot))
