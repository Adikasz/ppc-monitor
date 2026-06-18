"""
/account slash parancscsoport — hirdetési fiók kezelés Discordból.

A hozzárendelés és a KPI egysége a FIÓK (ad account) — egy kliensnek lehet külön
Meta- és Google-fiókja, külön OM-mel és külön KPI-val (21. lépés).

Parancsok:
    /account list   [client:<>] [page:<>]     — fiókok #id-vel (OM + kampány + KPI)
    /account add    client:<> platform:<> account_id:<>  — fiók regisztrálása (admin)
    /account remove account_id:<>             — fiók soft-delete (admin)
    /account assign account:<#id> user:<@OM> [role]  — OM a fiók minden kampányára (admin)
    /account unassign account:<#id> user:<@OM>       — OM eltávolítása a fiókról (admin)
    /account kpi    account:<#id> [KPI mezők...]      — fiók KPI + lecsorgatás (admin)

A fiókokat a #szám (ad_accounts.id) azonosítja — ezt a `/account list` írja ki.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict

import discord
from discord import app_commands
from discord.ext import commands

from src.config import get_config
from src.storage import account_assignments as account_assignments_storage
from src.storage import ad_account_kpis as ad_account_kpis_storage
from src.storage import ad_accounts as ad_accounts_storage
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
_ROLE_CHOICES = [
    app_commands.Choice(name="primary — elsődleges felelős", value="primary"),
    app_commands.Choice(name="supporter — helyettes OM", value="supporter"),
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
    admin = _admin_channel_id()
    if admin is None:
        return True
    return interaction.channel_id == admin


def _resolve_client(value: str) -> dict | None:
    val = (value or "").strip()
    if not val:
        return None
    if val.isdigit():
        row = clients_storage.get_client(int(val))
        if row is not None:
            return row
    return clients_storage.get_client_by_name(val)


def _display_name(member: discord.Member | discord.User) -> str:
    if isinstance(member, discord.Member) and member.nick:
        return member.nick
    return member.display_name or member.name


def _short_account(ext_id: object) -> str:
    s = str(ext_id)
    return s if len(s) <= 11 else s[:8] + "…"


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


def _account_campaign_ids(ad_account_id: int, *, active_only: bool = True) -> list[int]:
    """Egy fiók kampány-ID-jai. active_only=True → a nem-'ended' kampányok."""
    rows = campaigns_storage.get_campaigns_by_ad_account(ad_account_id)
    if active_only:
        rows = [r for r in rows if r.get("lifecycle_state") != "ended"]
    return [r["id"] for r in rows]


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class AdAccountsCog(commands.GroupCog, group_name="account"):
    """Az `account` parancscsoport implementációja (fiók-szintű kezelés)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /account list [client:<>] [page:<>]
    # ------------------------------------------------------------------
    @app_commands.command(name="list", description="Hirdetési fiókok #id-vel (OM + kampány + KPI)")
    @app_commands.describe(
        client="Opcionális: csak ennek az ügyfélnek a fiókjai (név vagy id)",
        page="Oldal száma (1-től), alapból 1 — csak az összes fiók nézetnél releváns",
    )
    async def list_(
        self,
        interaction: discord.Interaction,
        client: str | None = None,
        page: int = 1,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if client:
            c = await asyncio.to_thread(_resolve_client, client)
            if c is None:
                await interaction.followup.send(
                    f"❌ Nem található ügyfél: **{client}**\nNézd meg: `/client list`"
                )
                return
            accounts = await asyncio.to_thread(
                ad_accounts_storage.get_ad_accounts_for_client, c["id"], active_only=False
            )
            scope = f" — {c['name']}"
        else:
            accounts = await asyncio.to_thread(ad_accounts_storage.list_ad_accounts, active_only=False)
            scope = ""

        if not accounts:
            await interaction.followup.send(
                "Nincs hirdetési fiók." + (f" ({client})" if client else "")
                + "\nAdj hozzá egyet: `/account add` vagy `/client onboard`"
            )
            return

        accounts.sort(key=lambda a: a["id"])

        # Bulk kiegészítő adatok
        clients_all = await asyncio.to_thread(clients_storage.list_clients, active_only=False)
        client_name = {cc["id"]: cc["name"] for cc in clients_all}
        camp_per_acct = Counter(
            await asyncio.to_thread(campaigns_storage.list_ad_account_ids, active_only=True)
        )
        kpi_ids = await asyncio.to_thread(ad_account_kpis_storage.list_account_ids_with_kpis)
        om_rows = await asyncio.to_thread(account_assignments_storage.list_all_account_assignments)
        om_by_acct: dict[int, list[str]] = defaultdict(list)
        for r in om_rows:
            nm = (r.get("users") or {}).get("display_name")
            if nm:
                om_by_acct[r["ad_account_id"]].append(nm)

        per_page = 25
        total = len(accounts)
        pages = (total + per_page - 1) // per_page
        page = max(1, min(page, pages))
        start = (page - 1) * per_page
        page_accounts = accounts[start:start + per_page]

        lines: list[str] = []
        for a in page_accounts:
            aid = a["id"]
            cname = client_name.get(a["client_id"], f"#{a['client_id']}")
            n_camp = camp_per_acct.get(aid, 0)
            oms = om_by_acct.get(aid, [])
            om_str = ", ".join(f"@{n}" for n in oms) if oms else "—"
            kpi_emoji = "✅" if aid in kpi_ids else "❌"
            inactive = " · ⏸ inaktív" if not a.get("is_active", True) else ""
            lines.append(
                f"**#{aid}**  {cname} · `{a['platform']}` · `{_short_account(a['external_account_id'])}` · "
                f"{n_camp} kampány · OM: {om_str} · KPI {kpi_emoji}{inactive}"
            )

        embed = discord.Embed(
            title=f"🗂 Hirdetési fiókok{scope} ({start + 1}–{start + len(page_accounts)} / {total})",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        footer = f"Lap: {page}/{pages}"
        if page < pages:
            footer += f"  |  /account list page:{page + 1}"
        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # /account add client:<> platform:<> account_id:<>
    # ------------------------------------------------------------------
    @app_commands.command(name="add", description="Hirdetési fiók regisztrálása ügyfélhez (admin)")
    @app_commands.describe(
        client="Az ügyfél neve vagy numerikus azonosítója",
        platform="Hirdetési platform: meta vagy google",
        account_id="Fiók azonosító (Meta: act_123 vagy 123, Google: 123-456-7890)",
        account_name="Opcionális: a fiók emberi neve",
    )
    @app_commands.choices(platform=_PLATFORM_CHOICES)
    async def add(
        self,
        interaction: discord.Interaction,
        client: str,
        platform: str,
        account_id: str,
        account_name: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        account_id = (account_id or "").strip()
        if not account_id:
            await interaction.followup.send("❌ A fiók azonosító nem lehet üres.")
            return

        c = await asyncio.to_thread(_resolve_client, client)
        if c is None:
            await interaction.followup.send(
                f"❌ Nem található ügyfél: **{client}**\nNézd meg: `/client list`"
            )
            return
        if not c.get("is_active", True):
            await interaction.followup.send(f"❌ Az ügyfél **{c['name']}** inaktív.")
            return

        normalized = ad_accounts_storage.normalize_external_account_id(platform, account_id)

        try:
            row, created = await asyncio.to_thread(
                ad_accounts_storage.get_or_create_ad_account,
                c["id"], platform, normalized,
                account_name=account_name.strip() if account_name else None,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Hiba a fiók regisztrálásakor (%s / %s)", c["name"], normalized)
            await interaction.followup.send(f"❌ Hiba: `{exc}`")
            return

        if not created:
            reactivated = False
            if not row.get("is_active", True):
                await asyncio.to_thread(ad_accounts_storage.set_ad_account_active, row["id"], True)
                reactivated = True
            note = " — újra aktiválva ✅" if reactivated else ""
            await interaction.followup.send(
                f"ℹ️ Ez a fiók már regisztrálva van: **{platform}** / `{normalized}` "
                f"→ **{c['name']}** *(#{row['id']})*{note}"
            )
            return

        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "account_add",
            entity_type="ad_account", entity_id=row["id"],
            details={"client_id": c["id"], "client_name": c["name"],
                     "platform": platform, "external_account_id": normalized},
        )
        log.info("Hirdetési fiók regisztrálva: %s / %s → %s (#%s)",
                 platform, normalized, c["name"], row["id"])
        await interaction.followup.send(
            f"✅ Hirdetési fiók regisztrálva: **{platform}** / `{normalized}`\n"
            f"Ügyfél: **{c['name']}** *(#{row['id']})*\n"
            f"Következő: `/discover client:{c['name']}`, majd `/account assign account:{row['id']} user:@OM`"
        )

    # ------------------------------------------------------------------
    # /account remove account_id:<>
    # ------------------------------------------------------------------
    @app_commands.command(name="remove", description="Hirdetési fiók eltávolítása — soft delete (admin)")
    @app_commands.describe(account_id="A fiók külső azonosítója (act_123 / 123 / 123-456-7890)")
    async def remove(self, interaction: discord.Interaction, account_id: str) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        account_id = (account_id or "").strip()
        if not account_id:
            await interaction.followup.send("❌ A fiók azonosító nem lehet üres.")
            return

        candidates = [
            account_id,
            ad_accounts_storage.normalize_external_account_id("meta", account_id),
            ad_accounts_storage.normalize_external_account_id("google", account_id),
        ]
        matches = await asyncio.to_thread(
            ad_accounts_storage.find_ad_accounts_by_external_id, candidates
        )
        if not matches:
            await interaction.followup.send(
                f"❌ Nem található hirdetési fiók ezzel az azonosítóval: `{account_id}`\n"
                f"Tipp: `/account list` mutatja a fiókokat és a #id-ket."
            )
            return

        active_matches = [m for m in matches if m.get("is_active", True)]
        if not active_matches:
            await interaction.followup.send(
                f"ℹ️ A fiók már eltávolítva (inaktív): `{account_id}` *(#{matches[0]['id']})*"
            )
            return

        removed = []
        for m in active_matches:
            await asyncio.to_thread(ad_accounts_storage.set_ad_account_active, m["id"], False)
            removed.append(m)
            await asyncio.to_thread(
                audit.log_action, str(interaction.user.id), "account_remove",
                entity_type="ad_account", entity_id=m["id"],
                details={"platform": m["platform"], "external_account_id": m["external_account_id"],
                         "client_id": m.get("client_id")},
            )
            log.info("Hirdetési fiók soft-deleted: %s / %s (#%s)",
                     m["platform"], m["external_account_id"], m["id"])

        lines = "\n".join(
            f"🖥 `{m['platform']}` / `{m['external_account_id']}` *(#{m['id']})*" for m in removed
        )
        await interaction.followup.send(
            f"✅ Fiók eltávolítva (soft delete — az adat megmarad):\n{lines}"
        )

    # ------------------------------------------------------------------
    # /account assign account:<#id> user:<@OM> [role]
    # ------------------------------------------------------------------
    @app_commands.command(name="assign", description="OM hozzárendelése a fiók MINDEN kampányához (admin)")
    @app_commands.describe(
        account="A fiók #azonosítója (a `/account list` mutatja)",
        user="A hozzárendelendő Discord felhasználó (OM)",
        role="primary (elsődleges) vagy supporter (helyettes) — alap: primary",
    )
    @app_commands.choices(role=_ROLE_CHOICES)
    async def assign(
        self,
        interaction: discord.Interaction,
        account: int,
        user: discord.Member,
        role: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        acct = await asyncio.to_thread(ad_accounts_storage.get_ad_account, account)
        if acct is None:
            await interaction.followup.send(
                f"❌ Nincs ilyen hirdetési fiók: **#{account}**\nNézd meg: `/account list`"
            )
            return

        role_value = role.value if role else "primary"
        cname = (await asyncio.to_thread(clients_storage.get_client, acct["client_id"]) or {}).get("name", "?")

        user_row, user_created = await asyncio.to_thread(
            users_storage.get_or_create_user, str(user.id), _display_name(user)
        )
        if user_created:
            log.info("Új felhasználó regisztrálva: %s (%s)", user, user.id)

        # 1) Fiók-szintű hozzárendelés (öröklés-forrás az új kampányokhoz)
        try:
            await asyncio.to_thread(
                account_assignments_storage.upsert_account_assignment,
                acct["id"], user_row["id"], role=role_value,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Fiók-assign hiba (#%s)", acct["id"])
            await interaction.followup.send(
                f"❌ Nem sikerült a fiók-hozzárendelés: `{exc}`\n"
                f"Lehet, hogy a `0010_account_level.sql` migráció még nem futott le."
            )
            return

        # 2) Kaszkád a fiók jelenlegi (nem-ended) kampányaira
        campaign_ids = await asyncio.to_thread(_account_campaign_ids, acct["id"], active_only=True)
        casc = await asyncio.to_thread(
            assignments_storage.bulk_assign_campaigns,
            user_row["id"], campaign_ids,
            role=role_value, inherited_field="inherited_from_account",
            created_by_discord_user_id=str(interaction.user.id),
        )

        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "account_assign",
            entity_type="ad_account", entity_id=acct["id"],
            details={"client_name": cname, "platform": acct["platform"],
                     "user_discord_id": str(user.id), "user_name": _display_name(user),
                     "role": role_value, "campaigns": casc["total"]},
        )
        log.info("Fiók-assign: %s → %s · %s (%s, %d kampány)",
                 user, cname, acct["platform"], role_value, casc["total"])
        await interaction.followup.send(
            f"✅ {user.mention} hozzárendelve: **{cname} · {acct['platform']}** "
            f"({casc['total']} kampány, {role_value})"
        )

    # ------------------------------------------------------------------
    # /account unassign account:<#id> user:<@OM>
    # ------------------------------------------------------------------
    @app_commands.command(name="unassign", description="OM eltávolítása a fiókról (admin)")
    @app_commands.describe(
        account="A fiók #azonosítója",
        user="Az eltávolítandó Discord felhasználó",
    )
    async def unassign(
        self,
        interaction: discord.Interaction,
        account: int,
        user: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        acct = await asyncio.to_thread(ad_accounts_storage.get_ad_account, account)
        if acct is None:
            await interaction.followup.send(
                f"❌ Nincs ilyen hirdetési fiók: **#{account}**\nNézd meg: `/account list`"
            )
            return

        cname = (await asyncio.to_thread(clients_storage.get_client, acct["client_id"]) or {}).get("name", "?")
        user_row = await asyncio.to_thread(users_storage.get_user_by_discord_id, str(user.id))
        if user_row is None:
            await interaction.followup.send(
                f"**{_display_name(user)}** nincs a rendszerben — nem volt hozzárendelve."
            )
            return

        await asyncio.to_thread(
            account_assignments_storage.delete_account_assignment, acct["id"], user_row["id"]
        )
        campaign_ids = await asyncio.to_thread(_account_campaign_ids, acct["id"], active_only=False)
        deleted = await asyncio.to_thread(
            assignments_storage.bulk_unassign_campaigns, user_row["id"], campaign_ids
        )

        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "account_unassign",
            entity_type="ad_account", entity_id=acct["id"],
            details={"client_name": cname, "platform": acct["platform"],
                     "user_discord_id": str(user.id), "deleted_campaign_assignments": deleted},
        )
        log.info("Fiók-unassign: %s ← %s · %s (%d törölve)", user, cname, acct["platform"], deleted)
        await interaction.followup.send(
            f"✅ {user.mention} eltávolítva: **{cname} · {acct['platform']}** "
            f"({deleted} kampány-hozzárendelés törölve)"
        )

    # ------------------------------------------------------------------
    # /account kpi account:<#id> [KPI mezők...]
    # ------------------------------------------------------------------
    @app_commands.command(name="kpi", description="Fiók-szintű KPI + lecsorgatás a kampányokra (admin)")
    @app_commands.describe(
        account="A fiók #azonosítója",
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
        account: int,
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

        acct = await asyncio.to_thread(ad_accounts_storage.get_ad_account, account)
        if acct is None:
            await interaction.followup.send(
                f"❌ Nincs ilyen hirdetési fiók: **#{account}**\nNézd meg: `/account list`"
            )
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
            log.exception("Fiók-KPI mentés hiba (#%s)", acct["id"])
            await interaction.followup.send(
                f"❌ Nem sikerült a fiók-KPI mentése: `{exc}`\n"
                f"Lehet, hogy a `0010_account_level.sql` migráció (ad_account_kpis) még nem futott le."
            )
            return

        cascade_values = {
            f: row[f] for f in ad_account_kpis_storage.CASCADE_FIELDS if row.get(f) is not None
        }
        campaign_ids = await asyncio.to_thread(_account_campaign_ids, acct["id"], active_only=True)
        try:
            casc = await asyncio.to_thread(
                kpis_storage.cascade_account_kpis_to_campaigns,
                campaign_ids, values=cascade_values,
                set_by_discord_user_id=str(interaction.user.id),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Fiók-KPI kaszkád hiba (#%s)", acct["id"])
            await interaction.followup.send(
                f"⚠️ A fiók-KPI elmentve, de a kaszkád hibázott: `{exc}`\n"
                f"(Lehet, hogy a campaign_kpis `inherited_from_account` oszlop hiányzik — `0010`.)"
            )
            return

        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "account_kpi",
            entity_type="ad_account", entity_id=acct["id"],
            details={"client_name": cname, "platform": acct["platform"],
                     "fields": inputs, "cascade": casc},
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

        msg = (
            f"✅ KPI beállítva: **{cname} · {acct['platform']}** ({written} kampány)\n"
            f"{' | '.join(bits)} | {thr}"
        )
        if casc["skipped_override"]:
            msg += f"\nℹ️ {casc['skipped_override']} kampány kihagyva (kézi `/campaign kpi` override)"
        await interaction.followup.send(msg)


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(AdAccountsCog(bot))
