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

from datetime import datetime, timedelta, timezone

from src.config import get_config
from src.integrations import discord_router
from src.monitoring.discovery import discover_campaigns_for_client
from src.storage import account_assignments as account_assignments_storage
from src.storage import ad_account_kpis as ad_account_kpis_storage
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import alerts as alerts_storage
from src.storage import assignments as assignments_storage
from src.storage import audit
from src.storage import campaigns as campaigns_storage
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


def _short_account(ext_id: object) -> str:
    """Hosszú fiók-azonosító rövidítése a listához (act_165789… )."""
    s = str(ext_id)
    return s if len(s) <= 11 else s[:8] + "…"


def _platform_line(accounts: list[dict], camp_per_acct) -> str:
    """Egy platform fiók-sora a /client list-hez: '✅ act_165… (36 kampány)' vagy '➖ nincs'."""
    if not accounts:
        return "➖ nincs"
    return ", ".join(
        f"✅ {_short_account(a['external_account_id'])} "
        f"({camp_per_acct.get(a['id'], 0)} kampány)"
        for a in accounts
    )


def _fmt_ts(iso: object) -> str:
    """ISO időbélyeg → 'YYYY-MM-DD HH:MM'. Üres ha nincs."""
    if not iso:
        return "—"
    return str(iso)[:16].replace("T", " ")


def _max_last_seen(campaigns: list[dict]) -> str:
    """A kampányok közül a legutóbbi last_seen_at, formázva (≈ utolsó discovery)."""
    values = [c.get("last_seen_at") for c in campaigns if c.get("last_seen_at")]
    return _fmt_ts(max(values)) if values else "—"


def _format_account_kpi(kpi: dict | None) -> str:
    """Fiók-szintű KPI sor a /client status-hoz. '❌ nincs' ha nincs beállítva."""
    if not kpi:
        return "❌ nincs (`/account kpi`)"
    bits: list[str] = []
    if kpi.get("target_roas") is not None:
        bits.append(f"ROAS {_pct_int(kpi['target_roas'])}")
    if kpi.get("max_cpa") is not None:
        bits.append(f"CPA {_money(kpi['max_cpa'])} Ft")
    if kpi.get("monthly_budget") is not None:
        bits.append(f"Büdzsé {_money(kpi['monthly_budget'])} Ft")
    if kpi.get("target_ctr") is not None:
        bits.append(f"CTR {_pct_int(kpi['target_ctr'])}%")
    if kpi.get("max_cpc") is not None:
        bits.append(f"CPC {_money(kpi['max_cpc'])} Ft")
    if kpi.get("max_cpl") is not None:
        bits.append(f"CPL {_money(kpi['max_cpl'])} Ft")
    thr = f"W-{_pct_int(kpi.get('warning_pct'))}% / C-{_pct_int(kpi.get('critical_pct'))}%"
    return (" | ".join(bits) + " | " + thr) if bits else thr


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
    @app_commands.command(name="list", description="Kliensek lapozható listája (25/oldal)")
    @app_commands.describe(page="Oldal száma (1-től), alapból 1")
    async def list_(self, interaction: discord.Interaction, page: int = 1) -> None:
        await interaction.response.defer(ephemeral=True)

        clients = await asyncio.to_thread(clients_storage.list_clients, active_only=False)
        if not clients:
            await interaction.followup.send("Nincs még ügyfél. Hozz létre egyet: `/client add`")
            return

        per_page = 25
        total = len(clients)
        pages = (total + per_page - 1) // per_page
        page = max(1, min(page, pages))
        start = (page - 1) * per_page
        page_clients = clients[start:start + per_page]

        # Bulk adatok — lapfüggetlenül konstans lekérdezésszám (nincs N+1).
        # A KPI és az OM mostantól FIÓK-szintű (21. lépés).
        accounts = await asyncio.to_thread(ad_accounts_storage.list_ad_accounts, active_only=True)
        campaign_acct_ids = await asyncio.to_thread(
            campaigns_storage.list_ad_account_ids, active_only=True
        )
        kpi_acct_ids = await asyncio.to_thread(ad_account_kpis_storage.list_account_ids_with_kpis)
        om_rows = await asyncio.to_thread(account_assignments_storage.list_all_account_assignments)

        accounts_by_client: dict[int, list[dict]] = defaultdict(list)
        for a in accounts:
            accounts_by_client[a["client_id"]].append(a)
        camp_per_acct = Counter(campaign_acct_ids)
        om_by_acct: dict[int, list[str]] = defaultdict(list)
        for r in om_rows:
            name = (r.get("users") or {}).get("display_name")
            if name:
                om_by_acct[r["ad_account_id"]].append(name)

        blocks: list[str] = []
        for c in page_clients:
            cid = c["id"]
            accs = sorted(accounts_by_client.get(cid, []), key=lambda a: (a["platform"], a["id"]))
            inactive = " · ⏸ inaktív" if not c.get("is_active", True) else ""
            lines = [f"**#{cid} {c['name']}**{inactive}"]
            if accs:
                for a in accs:
                    aid = a["id"]
                    oms = om_by_acct.get(aid, [])
                    om_str = ", ".join(f"@{n}" for n in oms) if oms else "—"
                    kpi_emoji = "✅" if aid in kpi_acct_ids else "❌"
                    lines.append(
                        f"　`{a['platform']}` `{_short_account(a['external_account_id'])}` "
                        f"(#{aid}, {camp_per_acct.get(aid, 0)} kampány) · KPI {kpi_emoji} · OM: {om_str}"
                    )
            else:
                lines.append("　➖ nincs fiók")
            blocks.append("\n".join(lines))

        description = "\n\n".join(blocks)
        if len(description) > 4000:
            description = description[:3990] + "\n…"

        first = start + 1
        last = start + len(page_clients)
        embed = discord.Embed(
            title=f"📋 Kliensek ({first}–{last} / {total} total)",
            description=description or "—",
            color=discord.Color.blue(),
        )
        footer = f"Lap: {page}/{pages}"
        if page < pages:
            footer += f"  |  /client list page:{page + 1}"
        embed.set_footer(text=footer)
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
            acc_value = "— (nincs fiók — `/account add` vagy `/client onboard`)"
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

        # Kényelmi parancs: a kliens MINDEN (aktív) fiókjára fiók-szintű
        # hozzárendelés + kaszkád a fiók kampányaira (21. lépés — az egység a fiók).
        accounts = await asyncio.to_thread(
            ad_accounts_storage.get_ad_accounts_for_client, c["id"], active_only=True
        )
        if not accounts:
            await interaction.followup.send(
                f"**{c['name']}**-hez nincs aktív hirdetési fiók — előbb `/account add`."
            )
            return

        total_campaigns = 0
        per_account: list[str] = []
        for a in accounts:
            try:
                await asyncio.to_thread(
                    account_assignments_storage.upsert_account_assignment,
                    a["id"], user_row["id"], role=role_value,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Kliens-assign: fiók-hozzárendelés hiba (#%s)", a["id"])
                per_account.append(f"`{a['platform']}` #{a['id']} — ❌ `{exc}`")
                continue
            campaign_ids = [
                cm["id"]
                for cm in await asyncio.to_thread(campaigns_storage.get_campaigns_by_ad_account, a["id"])
                if cm.get("lifecycle_state") != "ended"
            ]
            casc = await asyncio.to_thread(
                assignments_storage.bulk_assign_campaigns,
                user_row["id"], campaign_ids,
                role=role_value, inherited_field="inherited_from_account",
                created_by_discord_user_id=str(interaction.user.id),
            )
            total_campaigns += casc["total"]
            per_account.append(f"`{a['platform']}` #{a['id']} — {casc['total']} kampány")

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
                "accounts": len(accounts),
                "campaigns": total_campaigns,
            },
        )

        log.info(
            "Kliens-assign (fiókonként): %s → %s (%d fiók, %d kampány)",
            user, c["name"], len(accounts), total_campaigns,
        )
        await interaction.followup.send(
            f"✅ {user.mention} hozzárendelve: **{c['name']}** — "
            f"{len(accounts)} fiók, {total_campaigns} kampány ({role_value})\n"
            + "\n".join(f"　• {line}" for line in per_account)
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

        # Fiók-szintű hozzárendelések törlése a kliens MINDEN fiókján, plusz a
        # régi kliens-szintű sor (visszafelé kompat.), majd a kampány-szintű sorok.
        accounts = await asyncio.to_thread(
            ad_accounts_storage.get_ad_accounts_for_client, c["id"], active_only=False
        )
        for a in accounts:
            await asyncio.to_thread(
                account_assignments_storage.delete_account_assignment, a["id"], user_row["id"]
            )
        await asyncio.to_thread(
            assignments_storage.delete_assignment, user_id=user_row["id"], client_id=c["id"]
        )

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
                "accounts": len(accounts),
                "deleted_campaign_assignments": deleted,
            },
        )

        log.info(
            "Kliens-unassign (fiókonként): %s ← %s (%d fiók, %d kampány törölve)",
            user, c["name"], len(accounts), deleted,
        )
        await interaction.followup.send(
            f"✅ {user.mention} eltávolítva: **{c['name']}** — {len(accounts)} fiók, "
            f"{deleted} kampány-hozzárendelés törölve"
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

        # Kényelmi parancs: ugyanazt a KPI-t állítja a kliens MINDEN (aktív)
        # fiókjára — a tárolás FIÓK-szinten történik (21. lépés).
        accounts = await asyncio.to_thread(
            ad_accounts_storage.get_ad_accounts_for_client, c["id"], active_only=True
        )
        if not accounts:
            await interaction.followup.send(
                f"**{c['name']}**-hez nincs aktív hirdetési fiók — előbb `/account add`."
            )
            return

        total_written = 0
        last_row: dict = {}
        per_account: list[str] = []
        for a in accounts:
            try:
                row = await asyncio.to_thread(
                    ad_account_kpis_storage.upsert_ad_account_kpis, a["id"], fields=inputs
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Kliens-KPI: fiók-KPI mentés hiba (#%s)", a["id"])
                await interaction.followup.send(
                    f"❌ Nem sikerült a fiók-KPI mentése (#{a['id']}): `{exc}`\n"
                    f"Lehet, hogy a `0010_account_level.sql` migráció még nem futott le."
                )
                return
            last_row = row
            cascade_values = {
                f: row[f] for f in ad_account_kpis_storage.CASCADE_FIELDS if row.get(f) is not None
            }
            campaign_ids = [
                cm["id"]
                for cm in await asyncio.to_thread(campaigns_storage.get_campaigns_by_ad_account, a["id"])
                if cm.get("lifecycle_state") != "ended"
            ]
            try:
                casc = await asyncio.to_thread(
                    kpis_storage.cascade_account_kpis_to_campaigns,
                    campaign_ids, values=cascade_values,
                    set_by_discord_user_id=str(interaction.user.id),
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Kliens-KPI: kaszkád hiba (#%s)", a["id"])
                await interaction.followup.send(
                    f"⚠️ A fiók-KPI (#{a['id']}) elmentve, de a kaszkád hibázott: `{exc}`\n"
                    f"(Lehet, hogy a campaign_kpis `inherited_from_account` oszlop hiányzik — `0010`.)"
                )
                return
            written = casc["updated"] + casc["inserted"]
            total_written += written
            per_account.append(f"`{a['platform']}` #{a['id']} — {written} kampány")

        await asyncio.to_thread(
            audit.log_action,
            str(interaction.user.id),
            "client_kpi",
            entity_type="client",
            entity_id=c["id"],
            details={"client_name": c["name"], "fields": inputs,
                     "accounts": len(accounts), "campaigns": total_written},
        )
        log.info("Kliens-KPI (fiókonként): %s → %d fiók, %d kampány",
                 c["name"], len(accounts), total_written)

        bits: list[str] = []
        if last_row.get("target_roas") is not None:
            bits.append(f"ROAS {_pct_int(last_row['target_roas'])}")
        if last_row.get("max_cpa") is not None:
            bits.append(f"Max CPA {_money(last_row['max_cpa'])} Ft")
        if last_row.get("monthly_budget") is not None:
            bits.append(f"Büdzsé {_money(last_row['monthly_budget'])} Ft")
        if last_row.get("target_ctr") is not None:
            bits.append(f"CTR {_pct_int(last_row['target_ctr'])}%")
        if last_row.get("max_cpc") is not None:
            bits.append(f"Max CPC {_money(last_row['max_cpc'])} Ft")
        if last_row.get("max_cpl") is not None:
            bits.append(f"Max CPL {_money(last_row['max_cpl'])} Ft")
        thr_line = f"W-{_pct_int(last_row.get('warning_pct'))}% / C-{_pct_int(last_row.get('critical_pct'))}%"

        await interaction.followup.send(
            f"✅ KPI beállítva: **{c['name']}** — {len(accounts)} fiók, {total_written} kampány\n"
            f"{' | '.join(bits)} | {thr_line}\n"
            + "\n".join(f"　• {line}" for line in per_account)
        )

    # ------------------------------------------------------------------
    # /client status client:<név vagy id>
    # ------------------------------------------------------------------
    @app_commands.command(name="status", description="Részletes státusz egy kliensről")
    @app_commands.describe(client="Az ügyfél neve vagy numerikus azonosítója")
    async def status(self, interaction: discord.Interaction, client: str) -> None:
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
        all_campaigns = await asyncio.to_thread(
            campaigns_storage.list_campaigns, c["id"], active_only=False
        )
        campaign_ids = [cm["id"] for cm in all_campaigns]
        last_alert = (
            await asyncio.to_thread(alerts_storage.get_latest_alert_for_campaigns, campaign_ids)
            if campaign_ids else None
        )

        campaigns_by_acct: dict[int, list[dict]] = defaultdict(list)
        for cm in all_campaigns:
            campaigns_by_acct[cm["ad_account_id"]].append(cm)

        is_active = c.get("is_active", True)
        embed = discord.Embed(
            title=f"📊 {c['name']} (#{c['id']})",
            color=discord.Color.blue() if is_active else discord.Color.dark_grey(),
        )

        # FIÓKONKÉNT: kampányszám + KPI + OM + utolsó discovery (21. lépés)
        if accounts:
            for a in accounts:
                camps = campaigns_by_acct.get(a["id"], [])
                active = sum(1 for cm in camps if cm.get("lifecycle_state") not in ("paused", "ended"))
                paused = sum(1 for cm in camps if cm.get("lifecycle_state") == "paused")
                icon = "📣" if a["platform"] == "meta" else "🔎"
                inactive = "" if a.get("is_active", True) else " ⏸"

                kpi = await asyncio.to_thread(ad_account_kpis_storage.get_ad_account_kpis, a["id"])
                oms = await asyncio.to_thread(
                    account_assignments_storage.get_account_assignments, a["id"]
                )
                om_line = ", ".join(
                    f"@{(r.get('users') or {}).get('display_name') or '?'} (`{r.get('role') or 'primary'}`)"
                    for r in oms
                ) if oms else "➖ nincs"

                embed.add_field(
                    name=f"{icon} {a['platform']}: {a['external_account_id']} (#{a['id']}){inactive}",
                    value=(
                        f"Kampányok: {active} aktív / {paused} pausált\n"
                        f"🎯 KPI: {_format_account_kpi(kpi)}\n"
                        f"👤 OM: {om_line}\n"
                        f"Utolsó discovery: {_max_last_seen(camps)}"
                    ),
                    inline=False,
                )
        else:
            embed.add_field(name="Hirdetési fiókok", value="— nincs", inline=False)

        # Utolsó alert
        if last_alert:
            alert_value = (
                f"{_fmt_ts(last_alert.get('detected_at'))} | "
                f"{last_alert.get('metric')} ({last_alert.get('severity')})"
            )
        else:
            alert_value = "nincs"
        embed.add_field(name="🚨 Utolsó alert", value=alert_value, inline=False)

        await interaction.followup.send(embed=embed)

    # ==================================================================
    # Lifecycle: offboard / pause / resume / reactivate (25. lépés)
    # ==================================================================

    async def _client_autocomplete(
        self, current: str, *, active: bool | None
    ) -> list[app_commands.Choice[str]]:
        rows = await asyncio.to_thread(
            clients_storage.search_clients, current, active=active
        )
        return [
            app_commands.Choice(name=r["name"][:100], value=str(r["id"]))
            for r in rows
        ][:25]

    # ------------------------------------------------------------------
    # /client offboard client:<> [reason:<>] [confirm:yes]
    # ------------------------------------------------------------------
    @app_commands.command(
        name="offboard",
        description="Ügyfél leállítása: kampányok 'ended', OM-ek törölve, monitoring off (admin)",
    )
    @app_commands.describe(
        client="Az ügyfél neve vagy #id",
        reason="Opcionális indok (auditba kerül)",
        confirm="Írd be: yes — a művelet NEM visszavonható (a /client reactivate részben visszaállít)",
    )
    async def offboard(
        self,
        interaction: discord.Interaction,
        client: str,
        reason: str | None = None,
        confirm: str | None = None,
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

        accounts = await asyncio.to_thread(
            ad_accounts_storage.get_ad_accounts_for_client, c["id"], active_only=False
        )
        account_ids = [a["id"] for a in accounts]
        campaigns = await asyncio.to_thread(campaigns_storage.list_campaigns, c["id"], active_only=False)
        all_campaign_ids = [cm["id"] for cm in campaigns]
        active_campaign_ids = [cm["id"] for cm in campaigns if cm.get("lifecycle_state") != "ended"]

        om_total = 0
        for aid in account_ids:
            om_total += len(
                await asyncio.to_thread(account_assignments_storage.get_account_assignments, aid)
            )

        # 1) Megerősítés kérése
        if (confirm or "").strip().lower() != "yes":
            await interaction.followup.send(
                f"⚠️ Biztosan offboardolod: **{c['name']}**?\n"
                f"Ez az alábbiak miatt csak részben visszavonható:\n"
                f"• {len(active_campaign_ids)} kampány → lifecycle: `ended`\n"
                f"• {om_total} OM hozzárendelés törlése\n"
                f"• Monitoring leáll, a kliens + fiókok inaktívvá válnak\n\n"
                f"A kampány-adatok (insights, alertek) MEGMARADNAK.\n"
                f"Megerősítés: `/client offboard client:{c['name']} confirm:yes`"
            )
            return

        # 2) Végrehajtás
        ended = await asyncio.to_thread(
            campaigns_storage.set_campaigns_lifecycle,
            active_campaign_ids, "ended", is_monitored=False,
        )
        deleted_assigns = await asyncio.to_thread(
            assignments_storage.delete_assignments_for_campaigns, all_campaign_ids
        )
        deleted_acct = await asyncio.to_thread(
            account_assignments_storage.delete_account_assignments_for_accounts, account_ids
        )
        await asyncio.to_thread(clients_storage.set_client_active, c["id"], False)
        for aid in account_ids:
            await asyncio.to_thread(ad_accounts_storage.set_ad_account_active, aid, False)

        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "client_offboard",
            entity_type="client", entity_id=c["id"],
            details={"client_name": c["name"], "reason": reason,
                     "campaigns_ended": ended, "deleted_assignments": deleted_assigns,
                     "deleted_account_assignments": deleted_acct, "accounts": len(account_ids)},
        )
        log.info("Offboard: %s — %d kampány ended, %d+%d hozzárendelés törölve",
                 c["name"], ended, deleted_assigns, deleted_acct)

        reason_line = f"\nReason: {reason}" if reason else ""
        await _notify_admin(
            f"📤 **{c['name']}** offboardolva\n"
            f"{ended} kampány leállítva · {deleted_acct} OM fiók-hozzárendelés törölve"
            f"{reason_line}"
        )
        await interaction.followup.send(
            f"✅ **{c['name']}** offboardolva.\n"
            f"• {ended} kampány → `ended` (monitoring off)\n"
            f"• {deleted_assigns} kampány- + {deleted_acct} fiók-hozzárendelés törölve\n"
            f"• kliens + {len(account_ids)} fiók inaktív\n"
            f"Visszahozható: `/client reactivate client:{c['name']}`"
        )

    @offboard.autocomplete("client")
    async def offboard_client_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._client_autocomplete(current, active=True)

    # ------------------------------------------------------------------
    # /client pause client:<> [days:<30>]
    # ------------------------------------------------------------------
    @app_commands.command(
        name="pause",
        description="Ügyfél kampányainak szüneteltetése X napra (auto-resume) (admin)",
    )
    @app_commands.describe(
        client="Az ügyfél neve vagy #id",
        days="Hány nap múlva álljon vissza automatikusan (alap: 30)",
    )
    async def pause(
        self,
        interaction: discord.Interaction,
        client: str,
        days: int = 30,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return
        if days <= 0 or days > 365:
            await interaction.followup.send("❌ A napok száma 1 és 365 között legyen.")
            return

        c = await asyncio.to_thread(_resolve_client, client)
        if c is None:
            await interaction.followup.send(
                f"❌ Nem található ügyfél: **{client}**\nNézd meg: `/client list`"
            )
            return

        campaigns = await asyncio.to_thread(campaigns_storage.list_campaigns, c["id"], active_only=False)
        ids = [cm["id"] for cm in campaigns if cm.get("lifecycle_state") != "ended"]
        until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

        paused = await asyncio.to_thread(
            campaigns_storage.set_campaigns_lifecycle, ids, "paused", lifecycle_until=until,
        )
        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "client_pause",
            entity_type="client", entity_id=c["id"],
            details={"client_name": c["name"], "days": days, "until": until, "campaigns": paused},
        )
        log.info("Pause: %s — %d kampány szüneteltetve %d napra", c["name"], paused, days)

        until_label = until[:10]
        await _notify_admin(
            f"⏸ **{c['name']}** szüneteltetve — {paused} kampány, auto-resume: {until_label}"
        )
        await interaction.followup.send(
            f"⏸ **{c['name']}** szüneteltetve: {paused} kampány `paused`.\n"
            f"Automatikus visszaállás: **{until_label}** (vagy korábban: `/client resume client:{c['name']}`).\n"
            f"Az OM-hozzárendelések megmaradnak."
        )

    @pause.autocomplete("client")
    async def pause_client_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._client_autocomplete(current, active=True)

    # ------------------------------------------------------------------
    # /client resume client:<>
    # ------------------------------------------------------------------
    @app_commands.command(
        name="resume",
        description="Szüneteltetett ügyfél kampányainak azonnali visszaállítása (admin)",
    )
    @app_commands.describe(client="Az ügyfél neve vagy #id")
    async def resume(self, interaction: discord.Interaction, client: str) -> None:
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

        campaigns = await asyncio.to_thread(campaigns_storage.list_campaigns, c["id"], active_only=False)
        paused_ids = [cm["id"] for cm in campaigns if cm.get("lifecycle_state") == "paused"]
        if not paused_ids:
            await interaction.followup.send(f"**{c['name']}**-nek nincs szüneteltetett kampánya.")
            return

        resumed = await asyncio.to_thread(
            campaigns_storage.set_campaigns_lifecycle,
            paused_ids, "mature", is_monitored=True, lifecycle_until=None,
        )
        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "client_resume",
            entity_type="client", entity_id=c["id"],
            details={"client_name": c["name"], "campaigns": resumed},
        )
        log.info("Resume: %s — %d kampány visszaállítva (mature)", c["name"], resumed)

        await _notify_admin(f"▶️ **{c['name']}** visszaállítva — {resumed} kampány `mature`")
        await interaction.followup.send(
            f"▶️ **{c['name']}** visszaállítva: {resumed} kampány → `mature`, monitoring újra él."
        )

    @resume.autocomplete("client")
    async def resume_client_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._client_autocomplete(current, active=True)

    # ------------------------------------------------------------------
    # /client reactivate client:<>
    # ------------------------------------------------------------------
    @app_commands.command(
        name="reactivate",
        description="Offboardolt ügyfél visszahozása: kliens + fiókok aktív, ended kampányok 'new' (admin)",
    )
    @app_commands.describe(client="Az (inaktív) ügyfél neve vagy #id")
    async def reactivate(self, interaction: discord.Interaction, client: str) -> None:
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

        await asyncio.to_thread(clients_storage.set_client_active, c["id"], True)
        accounts = await asyncio.to_thread(
            ad_accounts_storage.get_ad_accounts_for_client, c["id"], active_only=False
        )
        for a in accounts:
            await asyncio.to_thread(ad_accounts_storage.set_ad_account_active, a["id"], True)

        campaigns = await asyncio.to_thread(campaigns_storage.list_campaigns, c["id"], active_only=False)
        ended_ids = [cm["id"] for cm in campaigns if cm.get("lifecycle_state") == "ended"]
        revived = await asyncio.to_thread(
            campaigns_storage.set_campaigns_lifecycle,
            ended_ids, "new", is_monitored=True, lifecycle_until=None,
        )

        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "client_reactivate",
            entity_type="client", entity_id=c["id"],
            details={"client_name": c["name"], "accounts": len(accounts), "campaigns_revived": revived},
        )
        log.info("Reactivate: %s — %d fiók aktív, %d kampány → new", c["name"], len(accounts), revived)

        await _notify_admin(
            f"✅ **{c['name']}** reaktiválva — {len(accounts)} fiók aktív, {revived} kampány `new`"
        )
        await interaction.followup.send(
            f"✅ **{c['name']}** reaktiválva — {len(accounts)} fiók aktív, {revived} kampány → `new`.\n"
            f"Futtass `/discover client:{c['name']}`-t az új/aktuális kampányok felfedezéséhez."
        )

    @reactivate.autocomplete("client")
    async def reactivate_client_autocomplete(self, interaction: discord.Interaction, current: str):
        # Csak INAKTÍV kliensek (őket lehet reaktiválni).
        return await self._client_autocomplete(current, active=False)


async def _notify_admin(content: str) -> None:
    """Best-effort, NEM-ephemeral értesítés az admin csatornára (lifecycle audit-nyom).

    Élő bot kliens nélkül (pl. teszt) vagy hiányzó admin csatornánál csendben kimarad.
    """
    admin = get_config().discord_admin_channel_id
    if not admin:
        return
    try:
        await discord_router.send_text_message(admin, content)
    except Exception:  # noqa: BLE001 — az értesítés sosem buktathatja meg a parancsot
        log.exception("Admin lifecycle-értesítés kiküldése sikertelen")


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(ClientCog(bot))
