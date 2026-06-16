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
from src.storage import client_kpis as client_kpis_storage
from src.storage import clients as clients_storage
from src.storage import kpis as kpis_storage
from src.storage import users as users_storage
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


def _display_name(member: discord.Member | discord.User) -> str:
    """Emberi olvasható név Discord member/user objektumból."""
    if isinstance(member, discord.Member) and member.nick:
        return member.nick
    return member.display_name or member.name


def _money(v: object) -> str:
    """Forint-összeg formázása ezres szóközzel (150000 → '150 000')."""
    try:
        return f"{int(float(v)):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def _pct_int(v: object) -> str:
    """Százalék rövid formázása (20.0 → '20')."""
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else f"{f:g}"
    except (TypeError, ValueError):
        return str(v)


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

    # ------------------------------------------------------------------
    # /client assign client:<> user:<@OM> [role]
    # ------------------------------------------------------------------
    @app_commands.command(
        name="assign",
        description="OM hozzárendelése a kliens MINDEN kampányához (kaszkád, admin)",
    )
    @app_commands.describe(
        client="Az ügyfél neve vagy numerikus azonosítója",
        user="A hozzárendelendő Discord felhasználó (OM)",
        role="primary (elsődleges) vagy supporter (helyettes) — alap: primary",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name="primary — elsődleges felelős", value="primary"),
        app_commands.Choice(name="supporter — helyettes OM", value="supporter"),
    ])
    async def assign(
        self,
        interaction: discord.Interaction,
        client: str,
        user: discord.Member,
        role: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        role_value = role.value if role else "primary"

        c = await asyncio.to_thread(_resolve_client, client)
        if c is None:
            await interaction.followup.send(
                f"❌ Nem található ügyfél: **{client}**\nNézd meg: `/client list`"
            )
            return

        # OM auto-regisztráció
        user_row, user_created = await asyncio.to_thread(
            users_storage.get_or_create_user, str(user.id), _display_name(user)
        )
        if user_created:
            log.info("Új felhasználó regisztrálva: %s (%s)", user, user.id)

        # 1) Kliens-szintű hozzárendelés (öröklés forrása — az új kampányok ebből
        #    öröklik a hozzárendelést a discovery során).
        await asyncio.to_thread(
            assignments_storage.create_assignment,
            user_id=user_row["id"],
            client_id=c["id"],
            role=role_value,
            created_by_discord_user_id=str(interaction.user.id),
        )

        # 2) Kaszkád a kliens jelenlegi (nem-ended) kampányaira.
        campaigns = await asyncio.to_thread(campaigns_storage.list_campaigns, c["id"])
        campaign_ids = [row["id"] for row in campaigns]
        casc = await asyncio.to_thread(
            assignments_storage.bulk_assign_campaigns,
            user_row["id"],
            campaign_ids,
            role=role_value,
            created_by_discord_user_id=str(interaction.user.id),
        )

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "client_assign",
            entity_type="client",
            entity_id=c["id"],
            details={
                "client_name": c["name"],
                "user_discord_id": str(user.id),
                "user_name": _display_name(user),
                "role": role_value,
                "campaigns": casc["total"],
                "created": casc["created"],
                "updated": casc["updated"],
            },
        )

        log.info(
            "Kliens-assign: %s → %s (%s, %d kampány)",
            user, c["name"], role_value, casc["total"],
        )
        await interaction.followup.send(
            f"✅ {user.mention} hozzárendelve: **{c['name']}** "
            f"({casc['total']} kampány, {role_value})"
        )

    # ------------------------------------------------------------------
    # /client unassign client:<> user:<@OM>
    # ------------------------------------------------------------------
    @app_commands.command(
        name="unassign",
        description="OM eltávolítása a kliens összes kampányáról (admin)",
    )
    @app_commands.describe(
        client="Az ügyfél neve vagy numerikus azonosítója",
        user="Az eltávolítandó Discord felhasználó",
    )
    async def unassign(
        self,
        interaction: discord.Interaction,
        client: str,
        user: discord.Member,
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

        user_row = await asyncio.to_thread(
            users_storage.get_user_by_discord_id, str(user.id)
        )
        if user_row is None:
            await interaction.followup.send(
                f"**{_display_name(user)}** nincs a rendszerben — nem volt hozzárendelve."
            )
            return

        # Kliens-szintű hozzárendelés törlése (öröklés-forrás megszüntetése).
        await asyncio.to_thread(
            assignments_storage.delete_assignment,
            user_id=user_row["id"],
            client_id=c["id"],
        )

        # Kampány-szintű hozzárendelések törlése a kliens ÖSSZES kampányáról
        # (ended-eket is beleértve, hogy ne maradjon árva sor).
        campaigns = await asyncio.to_thread(
            campaigns_storage.list_campaigns, c["id"], active_only=False
        )
        campaign_ids = [row["id"] for row in campaigns]
        deleted = await asyncio.to_thread(
            assignments_storage.bulk_unassign_campaigns, user_row["id"], campaign_ids
        )

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "client_unassign",
            entity_type="client",
            entity_id=c["id"],
            details={
                "client_name": c["name"],
                "user_discord_id": str(user.id),
                "user_name": _display_name(user),
                "deleted_campaign_assignments": deleted,
            },
        )

        log.info("Kliens-unassign: %s ← %s (%d törölve)", user, c["name"], deleted)
        await interaction.followup.send(
            f"✅ {user.mention} eltávolítva: **{c['name']}** "
            f"({deleted} kampány-hozzárendelés törölve)"
        )

    # ------------------------------------------------------------------
    # /client kpi client:<> [KPI mezők...]
    # ------------------------------------------------------------------
    @app_commands.command(
        name="kpi",
        description="Kliens-szintű KPI + lecsorgatás minden kampányra (admin)",
    )
    @app_commands.describe(
        client="Az ügyfél neve vagy numerikus azonosítója",
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
        client: str,
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

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        c = await asyncio.to_thread(_resolve_client, client)
        if c is None:
            await interaction.followup.send(
                f"❌ Nem található ügyfél: **{client}**\nNézd meg: `/client list`"
            )
            return

        inputs = {
            k: v for k, v in {
                "target_roas": target_roas,
                "max_cpa": max_cpa,
                "monthly_budget": monthly_budget,
                "warning_pct": warning_pct,
                "critical_pct": critical_pct,
                "target_roi": target_roi,
                "max_cpl": max_cpl,
                "target_ctr": target_ctr,
                "max_cpc": max_cpc,
                "primary_conversion_event": primary_conversion_event,
            }.items() if v is not None
        }
        if not inputs:
            await interaction.followup.send("❌ Adj meg legalább egy KPI mezőt.")
            return

        # 1) Kliens-szintű KPI upsert
        try:
            client_row = await asyncio.to_thread(
                client_kpis_storage.upsert_client_kpis, c["id"], fields=inputs
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Hiba a kliens-KPI mentésekor (#%s)", c["id"])
            await interaction.followup.send(
                f"❌ Nem sikerült a kliens-KPI mentése: `{exc}`\n"
                f"Lehet, hogy a `0009_client_cascade.sql` migráció (client_kpis tábla) "
                f"még nem futott le."
            )
            return

        # 2) Lecsorgatás a kliens kampányaira (a tárolt — defaultokkal kiegészített
        #    — kliens-értékekből; a kézi /campaign kpi override-okat megőrzi).
        cascade_values = {
            f: client_row[f]
            for f in client_kpis_storage.CASCADE_FIELDS
            if client_row.get(f) is not None
        }
        campaigns = await asyncio.to_thread(campaigns_storage.list_campaigns, c["id"])
        campaign_ids = [row["id"] for row in campaigns]
        try:
            casc = await asyncio.to_thread(
                kpis_storage.cascade_client_kpis_to_campaigns,
                campaign_ids,
                values=cascade_values,
                set_by_discord_user_id=str(interaction.user.id),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Hiba a KPI kaszkádban (#%s)", c["id"])
            await interaction.followup.send(
                f"⚠️ A kliens-KPI elmentve, de a kaszkád hibázott: `{exc}`\n"
                f"(Lehet, hogy a campaign_kpis `warning_pct`/`critical_pct` oszlopok "
                f"hiányoznak — `0009` migráció.)"
            )
            return

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "client_kpi",
            entity_type="client",
            entity_id=c["id"],
            details={"client_name": c["name"], "fields": inputs, "cascade": casc},
        )

        written = casc["updated"] + casc["inserted"]
        log.info("Kliens-KPI: %s → %d kampány frissítve", c["name"], written)

        # Összefoglaló sorok
        summary_bits: list[str] = []
        if client_row.get("target_roas") is not None:
            summary_bits.append(f"ROAS target: {_pct_int(client_row['target_roas'])}")
        if client_row.get("target_roi") is not None:
            summary_bits.append(f"ROI: {_pct_int(client_row['target_roi'])}")
        if client_row.get("max_cpa") is not None:
            summary_bits.append(f"Max CPA: {_money(client_row['max_cpa'])} Ft")
        if client_row.get("max_cpl") is not None:
            summary_bits.append(f"Max CPL: {_money(client_row['max_cpl'])} Ft")
        if client_row.get("target_ctr") is not None:
            summary_bits.append(f"Cél CTR: {_pct_int(client_row['target_ctr'])}%")
        if client_row.get("max_cpc") is not None:
            summary_bits.append(f"Max CPC: {_money(client_row['max_cpc'])} Ft")
        if client_row.get("monthly_budget") is not None:
            summary_bits.append(f"Büdzsé: {_money(client_row['monthly_budget'])} Ft")
        if client_row.get("primary_conversion_event"):
            summary_bits.append(f"Konverzió: {client_row['primary_conversion_event']}")

        w = client_row.get("warning_pct")
        cr = client_row.get("critical_pct")
        thr_line = f"Warning: -{_pct_int(w)}% | Critical: -{_pct_int(cr)}%"

        msg = (
            f"✅ KPI beállítva: **{c['name']}** ({written} kampány frissítve)\n"
            f"{' | '.join(summary_bits)}\n{thr_line}"
        )
        if casc["skipped_override"]:
            msg += (
                f"\nℹ️ {casc['skipped_override']} kampány kihagyva "
                f"(kézi `/campaign kpi` override megmaradt)"
            )
        await interaction.followup.send(msg)


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(ClientCog(bot))
