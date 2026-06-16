"""
/client slash parancscsoport — ügyfélkezelés Discordból.

Parancsok:
    /client list                              — összes ügyfél (fiók-/kampányszám, insights)
    /client info     client:<név vagy id>     — egy ügyfél részletei
    /client add      name:<> [contact_email:] — új ügyfél létrehozása (admin)
    /client insights client:<> enabled:<bool> — insights kapcsoló (admin)
    /client onboard  name:<> platform:<> account_id:<> [contact_email:]
                                              — ügyfél + fiók + discovery egy lépésben (admin)

Megjegyzések:
  - Az írási parancsok (add, insights, onboard) az admin csatornára vannak
    korlátozva, ha a DISCORD_ADMIN_CHANNEL_ID be van állítva.
  - A /client list és info bárhonnan hívható (ephemeral válasz).
  - Minden Supabase / API hívás asyncio.to_thread()-ben fut, hogy ne blokkolja
    az event loop-ot.
  - A globális app-command error handler (main.py) elkapja a nem kezelt
    kivételeket — itt csak a felhasználóbarát, várt hibákat kezeljük.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict

import discord
from discord import app_commands
from discord.ext import commands

from src.config import get_config
from src.monitoring.discovery import discover_campaigns_for_client
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import assignments as assignments_storage
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
    """True, ha az interakció az admin csatornában történt (vagy ha nincs konfigurálva)."""
    admin = _admin_channel_id()
    if admin is None:
        return True
    return interaction.channel_id == admin


def _resolve_client(value: str) -> dict | None:
    """Ügyfél feloldása név VAGY numerikus ID alapján. None, ha nincs ilyen.

    Ha a bemenet csak számjegy, előbb ID-ként próbáljuk; ha nincs ilyen ID,
    névként is megkíséreljük (egy ügyfél elvileg lehet "123" nevű is).
    """
    val = (value or "").strip()
    if not val:
        return None
    if val.isdigit():
        row = clients_storage.get_client(int(val))
        if row is not None:
            return row
    return clients_storage.get_client_by_name(val)


def _insights_emoji(enabled: object) -> str:
    return "🟢" if enabled else "🔴"


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class ClientCog(commands.GroupCog, group_name="client"):
    """A `client` parancscsoport implementációja (ügyfélkezelés)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /client list
    # ------------------------------------------------------------------
    @app_commands.command(name="list", description="Összes ügyfél: fiók-/kampányszám + insights")
    async def list_(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        clients = await asyncio.to_thread(clients_storage.list_clients, active_only=False)
        if not clients:
            await interaction.followup.send("Nincs még ügyfél. Hozz létre egyet: `/client add`")
            return

        # Egy lekérdezés az összes aktív fiókra és kampányra → ügyfelenkénti
        # aggregálás Pythonban (nincs N+1 query).
        accounts = await asyncio.to_thread(ad_accounts_storage.list_ad_accounts, active_only=True)
        campaign_acct_ids = await asyncio.to_thread(
            campaigns_storage.list_ad_account_ids, active_only=True
        )

        acct_ids_by_client: dict[int, list[int]] = defaultdict(list)
        for a in accounts:
            acct_ids_by_client[a["client_id"]].append(a["id"])
        camp_per_acct = Counter(campaign_acct_ids)

        lines: list[str] = []
        for c in clients:
            cid = c["id"]
            aids = acct_ids_by_client.get(cid, [])
            n_accounts = len(aids)
            n_campaigns = sum(camp_per_acct.get(aid, 0) for aid in aids)
            # insights_enabled hiányozhat, ha a 0008 migráció még nem futott → default true
            insights = c.get("insights_enabled", True)
            inactive = "" if c.get("is_active", True) else " · ⏸ inaktív"
            lines.append(
                f"**#{cid} — {c['name']}**{inactive}\n"
                f"　🗂 {n_accounts} fiók · 📊 {n_campaigns} kampány · "
                f"insights {_insights_emoji(insights)}"
            )

        # Description-alapú lista (NEM mezőnként) — a 25-mezős embed limit miatt.
        description = ""
        shown = 0
        for line in lines:
            if len(description) + len(line) + 1 > 3900:
                break
            description += line + "\n"
            shown += 1

        embed = discord.Embed(
            title="👥 Ügyfelek",
            description=description or "—",
            color=discord.Color.blue(),
        )
        if shown < len(lines):
            embed.set_footer(text=f"{shown}/{len(clients)} ügyfél megjelenítve (a többi nem fért ki)")
        else:
            embed.set_footer(text=f"Összesen: {len(clients)} ügyfél")
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # /client info client:<név vagy id>
    # ------------------------------------------------------------------
    @app_commands.command(name="info", description="Egy ügyfél részletei (fiókok, kampányszám, OM-ek)")
    @app_commands.describe(client="Az ügyfél neve vagy numerikus azonosítója")
    async def info(self, interaction: discord.Interaction, client: str) -> None:
        await interaction.response.defer(ephemeral=True)

        c = await asyncio.to_thread(_resolve_client, client)
        if c is None:
            await interaction.followup.send(
                f"❌ Nem található ügyfél: **{client}**\n"
                f"Nézd meg az elérhető ügyfeleket: `/client list`"
            )
            return

        accounts = await asyncio.to_thread(
            ad_accounts_storage.get_ad_accounts_for_client, c["id"], active_only=False
        )
        campaigns = await asyncio.to_thread(campaigns_storage.list_campaigns, c["id"])
        assigns = await asyncio.to_thread(
            assignments_storage.get_assignments_for_client, c["id"]
        )

        is_active = c.get("is_active", True)
        embed = discord.Embed(
            title=f"#{c['id']} — {c['name']}",
            color=discord.Color.blue() if is_active else discord.Color.dark_grey(),
        )
        embed.add_field(name="Aktív", value="✅ igen" if is_active else "⏸ nem")
        embed.add_field(
            name="Insights",
            value=f"{_insights_emoji(c.get('insights_enabled', True))} "
                  f"{'be' if c.get('insights_enabled', True) else 'ki'}",
        )
        embed.add_field(name="Kontakt email", value=c.get("contact_email") or "—")
        embed.add_field(
            name="Discord csatorna",
            value=f"<#{c['discord_channel_id']}>" if c.get("discord_channel_id") else "—",
        )

        # Hirdetési fiókok
        if accounts:
            acc_lines = []
            for a in accounts:
                name_part = f" ({a['account_name']})" if a.get("account_name") else ""
                inactive = "" if a.get("is_active", True) else " · ⏸ inaktív"
                acc_lines.append(
                    f"🖥 `{a['platform']}` · `{a['external_account_id']}`{name_part}{inactive}"
                )
            acc_value = "\n".join(acc_lines)
        else:
            acc_value = "— (nincs fiók — `/adaccount add` vagy `/client onboard`)"
        embed.add_field(
            name=f"Hirdetési fiókok ({len(accounts)})", value=acc_value, inline=False
        )

        embed.add_field(name="Aktív kampányok száma", value=str(len(campaigns)), inline=False)

        # Hozzárendelt OM-ek (ügyfél-szintű assignments)
        if assigns:
            om_lines = []
            for row in assigns:
                u = row.get("users") or {}
                name = u.get("display_name") or f"user #{row.get('user_id', '?')}"
                role = row.get("role") or "primary"
                om_lines.append(f"👤 {name} (`{role}`)")
            om_value = "\n".join(om_lines)
        else:
            om_value = "— (nincs ügyfél-szintű hozzárendelés)"
        embed.add_field(name="Hozzárendelt OM-ek", value=om_value, inline=False)

        embed.add_field(name="Létrehozva", value=str(c.get("created_at", "—")), inline=False)
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # /client add name:<> [contact_email:<>]
    # ------------------------------------------------------------------
    @app_commands.command(name="add", description="Új ügyfél létrehozása (admin csatorna)")
    @app_commands.describe(
        name="Az ügyfél neve (egyedi)",
        contact_email="Opcionális kontakt email (ide megy a CRITICAL ügyfél-email)",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        name: str,
        contact_email: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        name = name.strip()
        if not name:
            await interaction.followup.send("❌ A név nem lehet üres.")
            return

        email = contact_email.strip() if contact_email else None

        existing = await asyncio.to_thread(clients_storage.get_client_by_name, name)
        if existing:
            await interaction.followup.send(
                f"❌ Már létezik ügyfél ezzel a névvel: **{name}** (id: {existing['id']})"
            )
            return

        try:
            row = await asyncio.to_thread(
                clients_storage.create_client, name, contact_email=email
            )
        except Exception as exc:  # noqa: BLE001 — felhasználói visszajelzéshez kell
            log.exception("Hiba az ügyfél létrehozásakor (%s)", name)
            await interaction.followup.send(f"❌ Hiba a létrehozáskor: `{exc}`")
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "client_add",
            entity_type="client",
            entity_id=row["id"],
            details={"name": name, "contact_email": email},
        )

        log.info("Ügyfél létrehozva: #%s — %s", row["id"], row["name"])
        email_line = f"\n📧 Kontakt email: `{email}`" if email else ""
        await interaction.followup.send(
            f"✅ Kliens létrehozva: **{row['name']}** (id: {row['id']}){email_line}"
        )

    # ------------------------------------------------------------------
    # /client insights client:<> enabled:<true|false>
    # ------------------------------------------------------------------
    @app_commands.command(name="insights", description="Ügyfél insights kapcsoló be/ki (admin csatorna)")
    @app_commands.describe(
        client="Az ügyfél neve vagy numerikus azonosítója",
        enabled="true = insights bekapcsolva, false = kikapcsolva",
    )
    async def insights(
        self,
        interaction: discord.Interaction,
        client: str,
        enabled: bool,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        c = await asyncio.to_thread(_resolve_client, client)
        if c is None:
            await interaction.followup.send(
                f"❌ Nem található ügyfél: **{client}**\nNézd meg: `/client list`"
            )
            return

        try:
            updated = await asyncio.to_thread(
                clients_storage.set_insights_enabled, c["id"], enabled
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Hiba az insights kapcsoló frissítésekor (#%s)", c["id"])
            await interaction.followup.send(
                f"❌ Nem sikerült frissíteni az insights kapcsolót: `{exc}`\n"
                f"Lehet, hogy a `0008_management.sql` migráció még nem futott le "
                f"(insights_enabled oszlop)."
            )
            return

        if updated is None:
            await interaction.followup.send(f"❌ Nem sikerült frissíteni: #{c['id']}")
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "client_insights",
            entity_type="client",
            entity_id=c["id"],
            details={"name": c["name"], "insights_enabled": enabled},
        )

        state = "🟢 bekapcsolva" if enabled else "🔴 kikapcsolva"
        await interaction.followup.send(
            f"✅ **{c['name']}** (id: {c['id']}) insights: {state}"
        )

    # ------------------------------------------------------------------
    # /client onboard name:<> platform:<> account_id:<> [contact_email:<>]
    # ------------------------------------------------------------------
    @app_commands.command(
        name="onboard",
        description="Új ügyfél teljes onboardingja: kliens + fiók + discovery (admin)",
    )
    @app_commands.describe(
        name="Az ügyfél neve",
        platform="Hirdetési platform: meta vagy google",
        account_id="Hirdetési fiók azonosítója (Meta: act_123, Google: 123-456-7890)",
        contact_email="Opcionális kontakt email",
    )
    @app_commands.choices(platform=_PLATFORM_CHOICES)
    async def onboard(
        self,
        interaction: discord.Interaction,
        name: str,
        platform: str,
        account_id: str,
        contact_email: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        name = name.strip()
        account_id = (account_id or "").strip()
        if not name:
            await interaction.followup.send("❌ A név nem lehet üres.")
            return
        if not account_id:
            await interaction.followup.send("❌ A fiók azonosító nem lehet üres.")
            return

        email = contact_email.strip() if contact_email else None
        normalized = ad_accounts_storage.normalize_external_account_id(platform, account_id)

        # 1) Ügyfél: ha már létezik (név alapján), újrahasznosítjuk — így az
        #    onboard idempotens (újrafuttatható), nem hasal el "már létezik"-kel.
        client = await asyncio.to_thread(clients_storage.get_client_by_name, name)
        client_created = False
        if client is None:
            try:
                client = await asyncio.to_thread(
                    clients_storage.create_client, name, contact_email=email
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Onboard: hiba az ügyfél létrehozásakor (%s)", name)
                await interaction.followup.send(f"❌ Hiba az ügyfél létrehozásakor: `{exc}`")
                return
            client_created = True

        # 2) Hirdetési fiók regisztrálása (normalizált external id-vel)
        try:
            account, account_created = await asyncio.to_thread(
                ad_accounts_storage.get_or_create_ad_account,
                client["id"],
                platform,
                normalized,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Onboard: hiba a fiók regisztrálásakor (%s / %s)", name, normalized)
            await interaction.followup.send(
                f"⚠️ Ügyfél kész (**{name}**, id: {client['id']}), de a fiók "
                f"regisztráció hibázott: `{exc}`"
            )
            return

        # 3) Discovery az ügyfélre
        try:
            result = await asyncio.to_thread(discover_campaigns_for_client, client["id"])
        except Exception as exc:  # noqa: BLE001
            log.exception("Onboard: discovery hiba (client_id=%s)", client["id"])
            await interaction.followup.send(
                f"⚠️ **{name}** ügyfél és fiók (`{normalized}`) kész, de a discovery "
                f"hibázott: `{exc}`\nKésőbb újrapróbálható: `/discover client:{name}`"
            )
            return

        # 4) Audit + összefoglaló
        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "client_onboard",
            entity_type="client",
            entity_id=client["id"],
            details={
                "name": name,
                "platform": platform,
                "external_account_id": normalized,
                "contact_email": email,
                "client_created": client_created,
                "account_created": account_created,
                "discovery": {k: v for k, v in result.items() if k != "errors"},
                "errors": len(result["errors"]),
            },
        )

        log.info(
            "Onboard kész: %s (client_created=%s, account_created=%s, inserted=%s, errors=%s)",
            name, client_created, account_created, result["inserted"], len(result["errors"]),
        )

        detail_lines = [
            f"{'🆕 létrehozva' if client_created else 'ℹ️ már létezett'} — ügyfél #{client['id']}",
            f"{'🆕 regisztrálva' if account_created else 'ℹ️ már létezett'} — "
            f"fiók `{platform}` / `{normalized}`",
            f"📊 discovery: {result['inserted']} új · {result['updated']} frissítve · "
            f"{result['deactivated']} deaktiválva",
        ]
        if result["errors"]:
            detail_lines.append(f"⚠️ {len(result['errors'])} hiba a discovery során (lásd logok)")

        await interaction.followup.send(
            f"✅ **{name}** onboardolva: {result['inserted']} kampány felfedezve.\n"
            + "\n".join(detail_lines)
        )


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(ClientCog(bot))
