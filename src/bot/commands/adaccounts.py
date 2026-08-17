"""
/account slash parancscsoport — hirdetési fiók kezelés Discordból.

A hozzárendelés és a KPI egysége a FIÓK (ad account) — egy kliensnek lehet külön
Meta- és Google-fiókja, külön OM-mel és külön KPI-val (21. lépés).

Parancsok:
    /account list   [client:<>] [page:<>]     — fiókok #id-vel (OM + kampány + KPI)
    /account add    platform:<> account:<> [client_name:<>] [account_name:<>]
                                          — fiók regisztrálása (admin); a kliens
                                           NINCS külön mezőként — automatikusan
                                           létrejön/összekötődik a fiók API neve
                                           alapján (kézi azonosító esetén a
                                           client_name kötelező, lásd korlátok)
    /account remove account_id:<>             — fiók soft-delete (admin)
    /account assign account:<#id> user:<@OM> [role]  — OM a fiók minden kampányára (admin)
    /account unassign account:<#id> user:<@OM>       — OM eltávolítása a fiókról (admin)
    /account kpi    account:<#id> [KPI mezők...]      — fiók KPI + lecsorgatás (admin)
    /account summary-now account:<#id>       — azonnali összefoglaló ÉLŐ mai adatból

A fiókokat a #szám (ad_accounts.id) azonosítja — ezt a `/account list` írja ki.

A `/account summary-now` a `/summary type:daily`-vel szemben nem a tárolt,
tegnapi alertekből dolgozik, hanem élőben lekéri a mai adatokat a platform
API-ból (1 batch hívás / fiók), és read-only pillanatképet ad — nem rögzít
alertet és nem küld riasztást.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from src.config import get_config
from src.integrations import account_catalog
from src.integrations.discord_router import split_message
from src.monitoring.instant_summary import (
    format_instant_summary,
    generate_instant_account_summary,
)
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


def _kpi_summary_bits(inputs: dict) -> list[str]:
    """Rövid, ember-olvasható összefoglaló a beállított KPI mezőkből (multi-mód)."""
    bits: list[str] = []
    if inputs.get("target_roas") is not None:
        bits.append(f"ROAS {_pct_int(inputs['target_roas'])}")
    if inputs.get("max_cpa") is not None:
        bits.append(f"Max CPA {_money(inputs['max_cpa'])} Ft")
    if inputs.get("monthly_budget") is not None:
        bits.append(f"Büdzsé {_money(inputs['monthly_budget'])} Ft")
    if inputs.get("target_ctr") is not None:
        bits.append(f"CTR {_pct_int(inputs['target_ctr'])}%")
    if inputs.get("max_cpc") is not None:
        bits.append(f"Max CPC {_money(inputs['max_cpc'])} Ft")
    if inputs.get("max_cpl") is not None:
        bits.append(f"Max CPL {_money(inputs['max_cpl'])} Ft")
    if inputs.get("min_ctr") is not None:
        bits.append(f"Min CTR {_pct_int(inputs['min_ctr'])}%")
    if inputs.get("max_cpm") is not None:
        bits.append(f"Max CPM {_money(inputs['max_cpm'])} Ft")
    if inputs.get("max_frequency") is not None:
        bits.append(f"Max Freq {_pct_int(inputs['max_frequency'])}")
    if inputs.get("min_impressions") is not None:
        bits.append(f"Min Impr {_money(inputs['min_impressions'])}")
    return bits


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

    @list_.autocomplete("client")
    async def list_client_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        rows = await asyncio.to_thread(clients_storage.search_clients, current, active=None)
        return [app_commands.Choice(name=r["name"][:100], value=str(r["id"])) for r in rows][:25]

    # ------------------------------------------------------------------
    # /account add platform:<> account:<> [client_name:<>] [account_name:<>]
    # ------------------------------------------------------------------
    @app_commands.command(
        name="add",
        description="Hirdetési fiók regisztrálása — a kliens automatikusan létrejön a fiók API-neve alapján (admin)",
    )
    @app_commands.describe(
        platform="Hirdetési platform: meta vagy google — válaszd ki ELŐSZÖR (az account: ez alapján szűr)",
        account="Válaszd ki a fiókot NÉV alapján a listából, vagy írd be kézzel az azonosítót",
        client_name="CSAK kézi azonosító esetén kötelező (ügynökségi fiók, nincs API-név) — ilyenkor az ügyfél neve",
        account_name="Opcionális: a fiók megjelenítési nevének felülírása (alapból az API-ból kapott nevet menti el)",
    )
    @app_commands.choices(platform=_PLATFORM_CHOICES)
    async def add(
        self,
        interaction: discord.Interaction,
        platform: str,
        account: str,
        client_name: str | None = None,
        account_name: str | None = None,
    ) -> None:
        """A kliens NEM külön mező — automatikusan feloldódik/létrejön a fiók
        API-neve alapján (lásd modul-docstring). Ha a választott `account`
        nem szerepel a platform API-katalógusában (kézi azonosító, pl.
        ügynökségi hozzáférésű Meta fiók), a `client_name` megadása kötelező.
        """
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        account_id = (account or "").strip()
        if not account_id:
            await interaction.followup.send("❌ A fiók azonosító nem lehet üres.")
            return

        normalized = ad_accounts_storage.normalize_external_account_id(platform, account_id)

        # 1) A fiók NEVE: elsődlegesen a platform API-katalógusából (ha a
        #    választott azonosító ott van), egyébként a kézzel megadott
        #    client_name — ez adja a kliens nevét is.
        api_name = await asyncio.to_thread(
            account_catalog.find_account_name, platform, normalized
        )
        if api_name:
            resolved_name = api_name
            name_source = "api"
        else:
            resolved_name = (client_name or "").strip()
            if not resolved_name:
                await interaction.followup.send(
                    "❌ Ez a fiók nem szerepel a platform API-listájában (kézzel "
                    "megadott azonosító — pl. ügynökségi hozzáférésű fiók, amit a "
                    "`/me/adaccounts` nem lát).\n"
                    "Ilyenkor add meg az ügyfél nevét is: `client_name:<...>`"
                )
                return
            name_source = "manual"

        # 2) Kliens feloldása (kis/nagybetű-független egyezés — ne duplikáljon),
        #    vagy létrehozása, ha még nincs ilyen nevű ügyfél.
        client = await asyncio.to_thread(clients_storage.find_client_by_name_ci, resolved_name)
        client_created = False
        if client is None:
            try:
                client = await asyncio.to_thread(clients_storage.create_client, resolved_name)
                client_created = True
            except Exception as exc:  # noqa: BLE001 — pl. race a unique constrainten
                log.warning("Ügyfél automatikus létrehozása ütközött (%s): %s", resolved_name, exc)
                client = await asyncio.to_thread(clients_storage.find_client_by_name_ci, resolved_name)
                if client is None:
                    await interaction.followup.send(f"❌ Nem sikerült az ügyfél létrehozása: `{exc}`")
                    return
        elif not client.get("is_active", True):
            await interaction.followup.send(
                f"❌ **{client['name']}** ügyfélnévvel már létezik egy INAKTÍV ügyfél — "
                f"aktiváld előbb (`/client list` → reactivate), majd próbáld újra."
            )
            return

        # A fiók saját megjelenítési neve (ad_accounts.account_name) — az
        # opcionális felülírás VAGY az imént feloldott név (API vagy kézi).
        stored_account_name = (account_name or "").strip() or resolved_name

        try:
            row, created = await asyncio.to_thread(
                ad_accounts_storage.get_or_create_ad_account,
                client["id"], platform, normalized,
                account_name=stored_account_name,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Hiba a fiók regisztrálásakor (%s / %s)", client["name"], normalized)
            await interaction.followup.send(f"❌ Hiba: `{exc}`")
            return

        if not created:
            reactivated = False
            if not row.get("is_active", True):
                await asyncio.to_thread(ad_accounts_storage.set_ad_account_active, row["id"], True)
                reactivated = True
            note = " — újra aktiválva ✅" if reactivated else ""
            # A fiók a MÁR LÉTEZŐ sorhoz tartozó (esetleg más) klienshez van
            # kötve — pl. ha a Meta account neve közben megváltozott, és most
            # egy másik névre oldódott fel. A TÉNYLEGES tulajdonost mutatjuk,
            # ne a most (esetleg félrevezetően) feloldott klienst.
            owner_client = client
            if row.get("client_id") != client["id"]:
                owner_client = (
                    await asyncio.to_thread(clients_storage.get_client, row["client_id"])
                    or client
                )
            await interaction.followup.send(
                f"ℹ️ Ez a fiók már regisztrálva van: **{platform}** / `{normalized}` "
                f"→ **{owner_client['name']}** *(#{row['id']})*{note}"
            )
            return

        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "account_add",
            entity_type="ad_account", entity_id=row["id"],
            details={
                "client_id": client["id"], "client_name": client["name"],
                "client_created": client_created, "name_source": name_source,
                "platform": platform, "external_account_id": normalized,
            },
        )
        log.info(
            "Hirdetési fiók regisztrálva: %s / %s → %s (#%s, kliens_uj=%s, nev_forras=%s)",
            platform, normalized, client["name"], row["id"], client_created, name_source,
        )

        client_note = (
            f"🆕 ÚJ ügyfél létrehozva a fiók neve alapján: **{client['name']}**"
            if client_created else
            f"🔗 meglévő ügyfélhez kötve: **{client['name']}**"
        )
        manual_note = (
            "\nℹ️ Ez a fiók nem volt az API-listában — a kliensnév kézzel megadott."
            if name_source == "manual" else ""
        )
        await interaction.followup.send(
            f"✅ Hirdetési fiók regisztrálva: **{platform}** / `{normalized}`\n"
            f"{client_note} *(#{client['id']})*{manual_note}\n"
            f"Ellenőrizd, hogy ez a helyes ügyfélnév — ha nem, jelezd az adminnak az átnevezéshez.\n"
            f"Következő: `/discover client:{client['name']}`, majd `/account assign account:{row['id']} user:@OM`"
        )

    @add.autocomplete("account")
    async def add_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Platform-szűrt fiók-javaslatok NÉVVEL a platform API-jából (Meta/Google).

        A kiválasztott elem ÉRTÉKE a technikai azonosító, a felhasználó viszont
        a fiók nevét látja — így nem kell `act_...` ID-t begépelni.

        A platformot a namespace-ből olvassuk (ugyanaz a minta, mint a
        `/my lifecycle` / `/my mute` campaign-autocomplete-jénél a `MyCommandsCog.
        _owned_campaign_choices`-ban) — amíg a `platform` mező nincs kitöltve,
        üres listát adunk, hogy Meta és Google fiók sose keveredjen egy listában.

        A már regisztrált fiókokat NEM szűrjük ki, csak megjelöljük (`✓`) és
        hátrasoroljuk: jelenleg mind a 66 API-fiók regisztrálva van, így a
        kiszűrésük néma, üres autocomplete-et adna, ami elrontott funkciónak
        látszik. A `/account add` amúgy is kezeli a duplikátumot (jelzi, hogy
        már létezik, és szükség esetén újraaktiválja).

        Ha az API nem elérhető, vagy a keresett fiók nincs a listában (pl.
        ügynökségi hozzáférésű Meta fiók), a mező KÉZZEL is kitölthető marad —
        ezért a beírt szöveget mindig felajánljuk választható elemként.
        """
        platform = (interaction.namespace.platform or "").strip().lower()
        if platform not in ("meta", "google"):
            return []

        try:
            existing = {
                a["external_account_id"]
                for a in await asyncio.to_thread(ad_accounts_storage.list_ad_accounts)
            }
        except Exception:  # noqa: BLE001
            existing = set()

        matches = await asyncio.to_thread(
            account_catalog.search_accounts, platform, current, limit=24,
        )
        # Még nem regisztrált fiókok előre — azok az érdekesek egy `add`-nál.
        matches.sort(key=lambda a: str(a.get("id")) in existing)

        choices = [
            app_commands.Choice(
                name=(
                    f"{'✓ ' if str(a['id']) in existing else ''}{a['name']} ({a['id']})"
                )[:100],
                value=str(a["id"])[:100],
            )
            for a in matches
        ]

        # Kézi beírás mindig maradjon lehetséges (lásd account_catalog korlátok).
        typed = (current or "").strip()
        if typed and not any(c.value == typed for c in choices):
            choices.insert(
                0,
                app_commands.Choice(name=f"↵ Kézi azonosító: {typed}"[:100], value=typed[:100]),
            )
        return choices[:25]

    # ------------------------------------------------------------------
    # /account remove account_id:<>
    # ------------------------------------------------------------------
    @app_commands.command(name="remove", description="Hirdetési fiók eltávolítása — soft delete (admin)")
    @app_commands.describe(
        account="Fiók: #id, külső azonosító VAGY kliensnév (pl. 2 / act_165… / Stopvill)"
    )
    async def remove(self, interaction: discord.Interaction, account: str) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        acct = await self._resolve_single_account(interaction, account)
        if acct is None:
            return

        if not acct.get("is_active", True):
            await interaction.followup.send(
                f"ℹ️ A fiók már eltávolítva (inaktív): "
                f"`{acct['platform']}` / `{acct['external_account_id']}` *(#{acct['id']})*"
            )
            return

        await asyncio.to_thread(ad_accounts_storage.set_ad_account_active, acct["id"], False)
        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "account_remove",
            entity_type="ad_account", entity_id=acct["id"],
            details={"platform": acct["platform"], "external_account_id": acct["external_account_id"],
                     "client_id": acct.get("client_id")},
        )
        log.info("Hirdetési fiók soft-deleted: %s / %s (#%s)",
                 acct["platform"], acct["external_account_id"], acct["id"])
        await interaction.followup.send(
            f"✅ Fiók eltávolítva (soft delete — az adat megmarad):\n"
            f"🖥 `{acct['platform']}` / `{acct['external_account_id']}` *(#{acct['id']})*"
        )

    @remove.autocomplete("account")
    async def remove_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._account_choices(current)

    # ------------------------------------------------------------------
    # /account assign — #id VAGY név VAGY több fiók (accounts:...) (admin)
    # ------------------------------------------------------------------
    async def _assign_one(
        self,
        interaction: discord.Interaction,
        acct: dict,
        user_row: dict,
        role_value: str,
        user: discord.Member,
    ) -> tuple[int, str]:
        """Egy fiókhoz rendeli a usert + lecsorgatja a kampányokra + auditál.

        Visszatérés: (kampányszám, kliensnév). Hibát DOB (a hívó kezeli — pl.
        hiányzó 0010 migráció).
        """
        cname = (await asyncio.to_thread(clients_storage.get_client, acct["client_id"]) or {}).get("name", "?")

        # 1) Fiók-szintű hozzárendelés (öröklés-forrás az új kampányokhoz)
        await asyncio.to_thread(
            account_assignments_storage.upsert_account_assignment,
            acct["id"], user_row["id"], role=role_value,
        )
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
        return casc["total"], cname

    @app_commands.command(
        name="assign",
        description="OM hozzárendelése fiók(ok) MINDEN kampányához — #id vagy név, több is (admin)",
    )
    @app_commands.describe(
        user="A hozzárendelendő Discord felhasználó (OM)",
        account="Egy fiók: #id, külső azonosító VAGY kliensnév (pl. 2 / act_165… / Stopvill)",
        accounts="Több fiók vesszővel: pl. Stopvill,MyMins,Brands",
        role="primary (elsődleges) vagy supporter (helyettes) — alap: primary",
    )
    @app_commands.choices(role=_ROLE_CHOICES)
    async def assign(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        account: str | None = None,
        accounts: str | None = None,
        role: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        if user.bot:
            await interaction.followup.send(
                f"❌ {user.mention} egy bot — nem rendelhető OM-ként fiókhoz. "
                f"Válassz valódi felhasználót a `user:` mezőben."
            )
            return

        if not account and not accounts:
            await interaction.followup.send(
                "❌ Adj meg egy fiókot (`account:`) vagy többet (`accounts:Stopvill,MyMins`)."
            )
            return

        role_value = role.value if role else "primary"
        user_row, user_created = await asyncio.to_thread(
            users_storage.get_or_create_user, str(user.id), _display_name(user)
        )
        if user_created:
            log.info("Új felhasználó regisztrálva: %s (%s)", user, user.id)

        # ---- MULTI mód: accounts:Stopvill,MyMins,... → minden feloldott fiók ----
        if accounts:
            tokens = [t.strip() for t in accounts.split(",") if t.strip()]
            assigned: list[str] = []
            total_campaigns = 0
            not_found: list[str] = []
            failed: list[str] = []
            seen: set[int] = set()

            for tok in tokens:
                res = await asyncio.to_thread(account_assignments_storage.resolve_accounts, tok)
                if res["status"] == "not_found":
                    not_found.append(tok)
                    continue
                for acct in res["accounts"]:
                    if acct["id"] in seen:
                        continue
                    seen.add(acct["id"])
                    try:
                        n, cname = await self._assign_one(interaction, acct, user_row, role_value, user)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("Fiók-assign hiba (#%s)", acct["id"])
                        failed.append(f"#{acct['id']} (`{exc}`)")
                        continue
                    total_campaigns += n
                    assigned.append(f"• {cname} · `{acct['platform']}` ({n} kampány, {role_value})")

            if not assigned:
                msg = "❌ Egyetlen fiókot sem sikerült hozzárendelni."
                if not_found:
                    msg += f"\nNem található: {', '.join(not_found)}"
                if failed:
                    msg += f"\nHiba: {', '.join(failed)} — lehet, hogy a `0010` migráció hiányzik."
                await interaction.followup.send(msg)
                return

            msg = f"✅ {user.mention} hozzárendelve:\n" + "\n".join(assigned)
            msg += f"\n**Összesen: {total_campaigns} kampány**"
            if not_found:
                msg += f"\n⚠️ Nem található: {', '.join(not_found)}"
            if failed:
                msg += f"\n⚠️ Hiba: {', '.join(failed)}"
            await interaction.followup.send(msg)
            return

        # ---- EGYES mód: account:<#id | external | kliensnév> ----
        res = await asyncio.to_thread(account_assignments_storage.resolve_accounts, account)

        if res["status"] == "not_found":
            await interaction.followup.send(
                f"❌ Nem található fiók vagy kliens: **{account}**\nNézd meg: `/account list`"
            )
            return

        if res["status"] == "ambiguous":
            who = res.get("client_name") or account
            lines = []
            for a in res["accounts"]:
                n = len(await asyncio.to_thread(_account_campaign_ids, a["id"], active_only=True))
                lines.append(
                    f"**#{a['id']}** `{a['platform']}` `{_short_account(a['external_account_id'])}` ({n} kampány)"
                )
            await interaction.followup.send(
                f"**{who}** több fiókkal rendelkezik ({len(res['accounts'])}):\n"
                + "\n".join(lines)
                + f"\nMelyiket? `/account assign account:<#id> user:@{_display_name(user)}`"
                + f"\n…vagy mindet egyszerre: `/account assign accounts:{who} user:@{_display_name(user)}`"
            )
            return

        acct = res["accounts"][0]
        try:
            n, cname = await self._assign_one(interaction, acct, user_row, role_value, user)
        except Exception as exc:  # noqa: BLE001
            log.exception("Fiók-assign hiba (#%s)", acct["id"])
            await interaction.followup.send(
                f"❌ Nem sikerült a fiók-hozzárendelés: `{exc}`\n"
                f"Lehet, hogy a `0010_account_level.sql` migráció még nem futott le."
            )
            return

        await interaction.followup.send(
            f"✅ {user.mention} hozzárendelve: **{cname} · {acct['platform']}** "
            f"({n} kampány, {role_value})"
        )

    # --- Autocomplete: kliensnév gépelése közben felugró fiók-lista ---
    @staticmethod
    def _ac_label(row: dict) -> str:
        return f"{row['client_name']} ({row['platform']} · {_short_account(row['external_account_id'])})"[:100]

    async def _account_choices(self, current: str) -> list[app_commands.Choice[str]]:
        """Közös autocomplete: a beírt szövegre illeszkedő (aktív) fiókok, #id az érték."""
        rows = await asyncio.to_thread(
            account_assignments_storage.search_account_choices, current
        )
        return [
            app_commands.Choice(name=self._ac_label(r), value=str(r["id"]))
            for r in rows
        ]

    async def _resolve_single_account(
        self, interaction: discord.Interaction, account: str
    ) -> dict | None:
        """Az `account` paramétert EGY fiókra oldja fel (#id / external / kliensnév).

        not_found / ambiguous esetén üzen a usernek és None-t ad vissza — ekkor a
        hívó parancs egyszerűen return-öljön.
        """
        res = await asyncio.to_thread(account_assignments_storage.resolve_accounts, account)
        if res["status"] == "not_found":
            await interaction.followup.send(
                f"❌ Nem található fiók vagy kliens: **{account}**\nNézd meg: `/account list`"
            )
            return None
        if res["status"] == "ambiguous":
            who = res.get("client_name") or account
            lines = [
                f"**#{a['id']}** `{a['platform']}` `{_short_account(a['external_account_id'])}`"
                for a in res["accounts"]
            ]
            await interaction.followup.send(
                f"**{who}** több fiókkal rendelkezik ({len(res['accounts'])}):\n"
                + "\n".join(lines)
                + "\nAdd meg a konkrét **#id**-t (vagy az autocomplete-ből válassz)."
            )
            return None
        return res["accounts"][0]

    async def _owned_account_choices(
        self, owner: dict, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete-jelöltek CSAK az adott OM saját (aktív) fiókjaiból.

        Az OM saját fiókjaiból építkezünk (nem a globális keresésből), így a lista
        akkor is teljes, ha a kliens ABC-rendben hátul van. Szűr kliensnévre, külső
        azonosítóra és #id-re; üres input az összes saját fiókot mutatja.
        """
        accounts = await asyncio.to_thread(
            account_assignments_storage.get_accounts_for_user, owner["id"]
        )
        if not accounts:
            return []
        clients = await asyncio.to_thread(clients_storage.list_clients)
        names = {c["id"]: c["name"] for c in clients}
        q = (current or "").strip().lower()
        choices: list[app_commands.Choice[str]] = []
        # Alfabetikus (kliensnév) rendezés — üres inputnál is stabil, ABC-sorrend.
        for a in sorted(
            accounts,
            key=lambda x: (names.get(x["client_id"], f"#{x['client_id']}").lower(), x["id"]),
        ):
            if not a.get("is_active", True):
                continue
            cname = names.get(a["client_id"], f"#{a['client_id']}")
            if q and q not in f"{cname} {a['external_account_id']} {a['id']}".lower():
                continue
            choices.append(app_commands.Choice(
                name=self._ac_label({
                    "client_name": cname, "platform": a["platform"],
                    "external_account_id": a["external_account_id"],
                }),
                value=str(a["id"]),
            ))
            if len(choices) >= 25:
                break
        return choices

    @assign.autocomplete("account")
    async def assign_account_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Az `account` mezőhöz: a beírt szövegre illeszkedő fiókok (#id az érték)."""
        return await self._account_choices(current)

    @assign.autocomplete("accounts")
    async def assign_accounts_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Az `accounts` (vesszős lista) mezőhöz: az UTOLSÓ token alapján keres,
        és a választott #id-t a már beírt tokenek MÖGÉ fűzi."""
        parts = (current or "").split(",")
        active = parts[-1].strip()
        prefix = ",".join(p.strip() for p in parts[:-1])
        if prefix:
            prefix += ","
        if len(active) < 1:
            return []
        rows = await asyncio.to_thread(
            account_assignments_storage.search_account_choices, active
        )
        return [
            app_commands.Choice(name=self._ac_label(r), value=f"{prefix}{r['id']}"[:100])
            for r in rows
        ]

    # ------------------------------------------------------------------
    # /account unassign account:<#id> user:<@OM>
    # ------------------------------------------------------------------
    @app_commands.command(name="unassign", description="OM eltávolítása a fiókról (admin)")
    @app_commands.describe(
        account="Fiók: #id, külső azonosító VAGY kliensnév (pl. 2 / act_165… / Stopvill)",
        user="Az eltávolítandó Discord felhasználó",
    )
    async def unassign(
        self,
        interaction: discord.Interaction,
        account: str,
        user: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        acct = await self._resolve_single_account(interaction, account)
        if acct is None:
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
    @unassign.autocomplete("account")
    async def unassign_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._account_choices(current)

    # ------------------------------------------------------------------
    # /account handover from_user:<@OM> to_user:<@OM> [days:<N>] [confirm:yes]
    # ------------------------------------------------------------------
    @app_commands.command(
        name="handover",
        description="Egy OM ÖSSZES fiókjának átadása másik OM-nek — szabadság kezelés (admin)",
    )
    @app_commands.describe(
        from_user="Az OM, akitől átvesszük a fiókokat (pl. szabadságra megy)",
        to_user="Az OM, aki átveszi a fiókokat",
        days="Opcionális: ideiglenes átadás ennyi napra (from_user is megmarad). Üres = TARTÓS átvitel.",
        confirm="Tartós átvitelnél kötelező: yes — from_user lekerül minden fiókról",
    )
    async def handover(
        self,
        interaction: discord.Interaction,
        from_user: discord.Member,
        to_user: discord.Member,
        days: Optional[int] = None,
        confirm: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        if from_user.bot or to_user.bot:
            await interaction.followup.send("❌ Bot nem lehet OM — válassz valódi felhasználókat.")
            return
        if from_user.id == to_user.id:
            await interaction.followup.send("❌ A `from_user` és `to_user` nem lehet ugyanaz.")
            return
        if days is not None and days <= 0:
            await interaction.followup.send(
                "❌ A `days` pozitív egész legyen (vagy hagyd üresen a tartós átvitelhez)."
            )
            return

        from_row = await asyncio.to_thread(users_storage.get_user_by_discord_id, str(from_user.id))
        if from_row is None:
            await interaction.followup.send(
                f"**{_display_name(from_user)}** nincs a rendszerben — nincs mit átadni."
            )
            return

        accounts = await asyncio.to_thread(
            account_assignments_storage.get_accounts_for_user, from_row["id"]
        )
        if not accounts:
            await interaction.followup.send(
                f"**{_display_name(from_user)}** egyetlen fiókhoz sincs rendelve — nincs mit átadni."
            )
            return

        permanent = days is None

        # Tartós átvitel → megerősítés kötelező (from_user véglegesen lekerül).
        if permanent and (confirm or "").strip().lower() != "yes":
            await interaction.followup.send(
                f"⚠️ **Tartós átvitel**: {_display_name(from_user)} **{len(accounts)}** fiókja "
                f"átkerül {_display_name(to_user)}-hoz, és {_display_name(from_user)} lekerül róluk.\n"
                f"Ha biztos vagy, futtasd újra `confirm:yes`-szel.\n"
                f"ℹ️ Ideiglenes (szabadság) átadáshoz add meg a `days:` paramétert — akkor "
                f"{_display_name(from_user)} megmarad, és a lejáratkor manuálisan visszaállítható."
            )
            return

        to_row, created = await asyncio.to_thread(
            users_storage.get_or_create_user, str(to_user.id), _display_name(to_user)
        )
        if created:
            log.info("Új felhasználó regisztrálva: %s (%s)", to_user, to_user.id)

        handover_until = None
        if not permanent:
            handover_until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

        moved: list[str] = []
        failed: list[str] = []
        meta_recorded = True
        for acct in accounts:
            role_value = acct.get("_role") or "primary"
            try:
                _n, cname = await self._assign_one(interaction, acct, to_row, role_value, to_user)
                if permanent:
                    await asyncio.to_thread(
                        account_assignments_storage.delete_account_assignment,
                        acct["id"], from_row["id"],
                    )
                    campaign_ids = await asyncio.to_thread(
                        _account_campaign_ids, acct["id"], active_only=False
                    )
                    await asyncio.to_thread(
                        assignments_storage.bulk_unassign_campaigns, from_row["id"], campaign_ids
                    )
                else:
                    # Ideiglenes: jegyezzük fel, ki adta át és meddig — ebből tud a
                    # /account handover-return pontosan visszavenni. Graceful, ha a
                    # 0012 migráció még nem futott le (ekkor a return fallback-re esik).
                    ok = await asyncio.to_thread(
                        account_assignments_storage.mark_handover,
                        acct["id"], to_row["id"],
                        from_user_id=from_row["id"], until=handover_until,
                    )
                    if not ok:
                        meta_recorded = False
            except Exception as exc:  # noqa: BLE001
                log.exception("Handover hiba (#%s)", acct["id"])
                failed.append(f"#{acct['id']} (`{exc}`)")
                continue
            moved.append(cname)

        if not moved:
            msg = "❌ Egyetlen fiókot sem sikerült átadni."
            if failed:
                msg += f"\nHiba: {', '.join(failed)} — lehet, hogy a `0010` migráció hiányzik."
            await interaction.followup.send(msg)
            return

        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "account_handover",
            entity_type="user", entity_id=from_row["id"],
            details={
                "from_discord_id": str(from_user.id), "from_name": _display_name(from_user),
                "to_discord_id": str(to_user.id), "to_name": _display_name(to_user),
                "permanent": permanent, "days": days, "handover_until": handover_until,
                "count": len(moved), "accounts": moved,
            },
        )
        log.info(
            "Handover: %s → %s (%s, %d fiók)",
            from_user, to_user, "tartós" if permanent else f"{days} nap", len(moved),
        )

        when = "tartósan" if permanent else f"{days} napra"
        msg = (
            f"✅ **{len(moved)} fiók átvíve** {_display_name(from_user)} → "
            f"{_display_name(to_user)} ({when}):\n" + ", ".join(moved)
        )
        if not permanent:
            msg += (
                f"\nℹ️ Ideiglenes: {_display_name(from_user)} megmaradt a fiókokon "
                f"(lejárat: {handover_until[:10]}). Visszaálláskor EGY paranccsal add vissza:\n"
                f"`/account handover-return from_user:@{_display_name(to_user)} "
                f"to_user:@{_display_name(from_user)}`"
            )
            if not meta_recorded:
                msg += (
                    "\n⚠️ A handover-metaadat nem íródott el (a `0012_handover.sql` migráció "
                    "még nem futott le) — a return a „mindkét OM rajta van” heurisztikára esik vissza."
                )
        if failed:
            msg += f"\n⚠️ Hiba: {', '.join(failed)}"
        await interaction.followup.send(msg)

    # ------------------------------------------------------------------
    # /account handover-return from_user:<@OM> to_user:<@OM>
    # ------------------------------------------------------------------
    @app_commands.command(
        name="handover-return",
        description="Ideiglenes handover visszaadása EGY paranccsal (szabadságról visszatéréskor) (admin)",
    )
    @app_commands.describe(
        from_user="Aki MOST bírja a fiókokat (handover-rel kapta, pl. a helyettes)",
        to_user="Az eredeti OM, akinek visszaadjuk (aki átadta)",
    )
    async def handover_return(
        self,
        interaction: discord.Interaction,
        from_user: discord.Member,
        to_user: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not _is_admin_channel(interaction):
            await interaction.followup.send("Ez a parancs csak az admin csatornában használható.")
            return

        if from_user.bot or to_user.bot:
            await interaction.followup.send("❌ Bot nem lehet OM — válassz valódi felhasználókat.")
            return
        if from_user.id == to_user.id:
            await interaction.followup.send("❌ A `from_user` és `to_user` nem lehet ugyanaz.")
            return

        from_row = await asyncio.to_thread(users_storage.get_user_by_discord_id, str(from_user.id))
        to_row = await asyncio.to_thread(users_storage.get_user_by_discord_id, str(to_user.id))
        if from_row is None or to_row is None:
            missing = _display_name(from_user if from_row is None else to_user)
            await interaction.followup.send(
                f"❌ **{missing}** nincs a rendszerben — nincs mit visszaadni."
            )
            return

        # Pontos: a from_user azon sorai, amiket kifejezetten to_user-től kapott
        # handover-rel. None → a 0012 oszlop hiányzik → fallback heurisztika.
        rows = await asyncio.to_thread(
            account_assignments_storage.get_handover_assignments, from_row["id"], to_row["id"]
        )
        used_fallback = False
        if rows is None:
            used_fallback = True
            rows = await asyncio.to_thread(
                account_assignments_storage.get_shared_account_assignments,
                from_row["id"], to_row["id"],
            )

        if not rows:
            hint = (
                "" if not used_fallback
                else "\n(A `0012_handover.sql` még nem futott le — a heurisztika sem talált "
                     "közös fiókot.)"
            )
            await interaction.followup.send(
                f"❌ Nincs aktív handover {from_user.mention} és {to_user.mention} között.{hint}"
            )
            return

        returned: list[str] = []
        failed: list[str] = []
        for r in rows:
            acct = r.get("ad_accounts") or {}
            acct_id = acct.get("id") or r.get("ad_account_id")
            if acct_id is None:
                continue
            cname = ((acct.get("clients") or {}).get("name")) or f"#{acct_id}"
            try:
                await asyncio.to_thread(
                    account_assignments_storage.delete_account_assignment, acct_id, from_row["id"]
                )
                campaign_ids = await asyncio.to_thread(
                    _account_campaign_ids, acct_id, active_only=False
                )
                await asyncio.to_thread(
                    assignments_storage.bulk_unassign_campaigns, from_row["id"], campaign_ids
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Handover-return hiba (#%s)", acct_id)
                failed.append(f"#{acct_id} (`{exc}`)")
                continue
            returned.append(cname)

        if not returned:
            msg = "❌ Egyetlen fiókot sem sikerült visszaadni."
            if failed:
                msg += f"\nHiba: {', '.join(failed)}"
            await interaction.followup.send(msg)
            return

        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "account_handover_return",
            entity_type="user", entity_id=from_row["id"],
            details={
                "from_discord_id": str(from_user.id), "from_name": _display_name(from_user),
                "to_discord_id": str(to_user.id), "to_name": _display_name(to_user),
                "fallback": used_fallback, "count": len(returned), "accounts": returned,
            },
        )
        log.info(
            "Handover-return: %s → %s (%d fiók%s)",
            from_user, to_user, len(returned), ", fallback" if used_fallback else "",
        )

        msg = (
            f"✅ **{len(returned)} fiók visszaadva** {from_user.mention} → "
            f"{to_user.mention}:\n" + ", ".join(returned)
        )
        if used_fallback:
            msg += (
                "\nℹ️ Heurisztikus mód (a `0012_handover.sql` még nem futott le): minden közös "
                f"fiókról levettem {_display_name(from_user)}-t. Ellenőrizd: `/account list`."
            )
        if failed:
            msg += f"\n⚠️ Hiba: {', '.join(failed)}"
        await interaction.followup.send(msg)

    async def _apply_account_kpi(
        self, interaction: discord.Interaction, acct: dict, inputs: dict
    ) -> dict:
        """Egy fiókra alkalmazza a KPI-t: upsert + kaszkád + audit. Hibát DOB (hívó kezeli).

        Visszatérés: {"cname", "row", "casc", "written"}.
        """
        cname = (await asyncio.to_thread(clients_storage.get_client, acct["client_id"]) or {}).get("name", "?")
        row = await asyncio.to_thread(
            ad_account_kpis_storage.upsert_ad_account_kpis, acct["id"], fields=inputs
        )
        cascade_values = {
            f: row[f] for f in ad_account_kpis_storage.CASCADE_FIELDS if row.get(f) is not None
        }
        campaign_ids = await asyncio.to_thread(_account_campaign_ids, acct["id"], active_only=True)
        casc = await asyncio.to_thread(
            kpis_storage.cascade_account_kpis_to_campaigns,
            campaign_ids, values=cascade_values,
            set_by_discord_user_id=str(interaction.user.id),
        )
        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "account_kpi",
            entity_type="ad_account", entity_id=acct["id"],
            details={"client_name": cname, "platform": acct["platform"],
                     "fields": inputs, "cascade": casc},
        )
        return {"cname": cname, "row": row, "casc": casc,
                "written": casc["updated"] + casc["inserted"]}

    @app_commands.command(name="kpi", description="Fiók-szintű KPI + lecsorgatás a kampányokra (csak saját #alerts csatorna)")
    @app_commands.describe(
        account="Egy fiók: #id, külső azonosító VAGY kliensnév (pl. 2 / act_165… / Stopvill)",
        accounts="Több fiók vesszővel — ugyanaz a KPI mindre: pl. Stopvill,MyMins",
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
        min_ctr="Minimum CTR % (alatta riasztás, pl. 1.5)",
        max_cpm="Maximum CPM Ft (felette riasztás, pl. 500)",
        max_frequency="Maximum frequency (felette riasztás, pl. 3.5)",
        min_impressions="Minimum impressions (alatta riasztás, pl. 1000)",
    )
    async def kpi(
        self,
        interaction: discord.Interaction,
        account: str | None = None,
        accounts: str | None = None,
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
        min_ctr: float | None = None,
        max_cpm: float | None = None,
        max_frequency: float | None = None,
        min_impressions: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # CSAK a saját #alerts csatornából futtatható (az admin csatornából NEM).
        channel_owner = await asyncio.to_thread(
            users_storage.get_user_by_alerts_channel, str(interaction.channel_id)
        )
        if channel_owner is None:
            await interaction.followup.send(
                "❌ Ez a parancs csak a saját #alerts csatornádból futtatható. "
                "Nyisd meg a saját csatornádat és futtasd ott."
            )
            return

        if not account and not accounts:
            await interaction.followup.send(
                "❌ Adj meg egy fiókot (`account:`) vagy többet (`accounts:Stopvill,MyMins`)."
            )
            return

        inputs = {k: v for k, v in {
            "target_roas": target_roas, "max_cpa": max_cpa, "monthly_budget": monthly_budget,
            "warning_pct": warning_pct, "critical_pct": critical_pct, "target_roi": target_roi,
            "max_cpl": max_cpl, "target_ctr": target_ctr, "max_cpc": max_cpc,
            "primary_conversion_event": primary_conversion_event,
            "min_ctr": min_ctr, "max_cpm": max_cpm, "max_frequency": max_frequency,
            "min_impressions": min_impressions,
        }.items() if v is not None}
        if not inputs:
            await interaction.followup.send("❌ Adj meg legalább egy KPI mezőt.")
            return

        # Csak a saját (hozzárendelt) fiókok módosíthatók.
        owned = await asyncio.to_thread(
            account_assignments_storage.get_account_ids_for_user, channel_owner["id"]
        )

        # ---- MULTI mód: accounts:Stopvill,MyMins,... → ugyanaz a KPI mindre ----
        if accounts:
            tokens = [t.strip() for t in accounts.split(",") if t.strip()]
            applied: list[str] = []
            not_found: list[str] = []
            not_owned: list[str] = []
            failed: list[str] = []
            seen: set[int] = set()

            for tok in tokens:
                res = await asyncio.to_thread(account_assignments_storage.resolve_accounts, tok)
                if res["status"] == "not_found":
                    not_found.append(tok)
                    continue
                for acct in res["accounts"]:
                    if acct["id"] in seen:
                        continue
                    seen.add(acct["id"])
                    if acct["id"] not in owned:
                        cn = (await asyncio.to_thread(clients_storage.get_client, acct["client_id"]) or {}).get("name", f"#{acct['id']}")
                        not_owned.append(f"{cn} · {acct['platform']}")
                        continue
                    try:
                        summ = await self._apply_account_kpi(interaction, acct, inputs)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("Fiók-KPI hiba (#%s)", acct["id"])
                        failed.append(f"#{acct['id']} (`{exc}`)")
                        continue
                    applied.append(f"• {summ['cname']} · `{acct['platform']}` ({summ['written']} kampány)")

            if not applied:
                msg = "❌ Egyetlen fiókra sem sikerült a KPI beállítása."
                if not_found:
                    msg += f"\nNem található: {', '.join(not_found)}"
                if not_owned:
                    msg += f"\nNincs hozzád rendelve: {', '.join(not_owned)}"
                if failed:
                    msg += f"\nHiba: {', '.join(failed)}"
                await interaction.followup.send(msg)
                return

            set_bits = _kpi_summary_bits(inputs)
            msg = f"✅ KPI beállítva {len(applied)} fiókra:\n" + "\n".join(applied)
            if set_bits:
                msg += f"\n**{' | '.join(set_bits)}**"
            if not_found:
                msg += f"\n⚠️ Nem található: {', '.join(not_found)}"
            if not_owned:
                msg += f"\n⚠️ Nincs hozzád rendelve: {', '.join(not_owned)}"
            if failed:
                msg += f"\n⚠️ Hiba: {', '.join(failed)}"
            await interaction.followup.send(msg)
            return

        # ---- EGYES mód: account:<#id | external | kliensnév> ----
        acct = await self._resolve_single_account(interaction, account)
        if acct is None:
            return

        if acct["id"] not in owned:
            await interaction.followup.send("❌ Ez a fiók nincs hozzád rendelve.")
            return

        try:
            summ = await self._apply_account_kpi(interaction, acct, inputs)
        except Exception as exc:  # noqa: BLE001
            log.exception("Fiók-KPI hiba (#%s)", acct["id"])
            await interaction.followup.send(
                f"❌ Nem sikerült a fiók-KPI beállítása: `{exc}`\n"
                f"Lehet, hogy a `0010_account_level.sql` / `0011` migráció még nem futott le."
            )
            return

        cname = summ["cname"]
        row = summ["row"]
        casc = summ["casc"]
        written = summ["written"]
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

        # Küszöb-alapú metrikák (0011) — külön sor, csak a beállítottak.
        bits2: list[str] = []
        if row.get("min_ctr") is not None:
            bits2.append(f"Min CTR {_pct_int(row['min_ctr'])}%")
        if row.get("max_cpm") is not None:
            bits2.append(f"Max CPM {_money(row['max_cpm'])} Ft")
        if row.get("max_frequency") is not None:
            bits2.append(f"Max Frequency {_pct_int(row['max_frequency'])}")
        if row.get("min_impressions") is not None:
            bits2.append(f"Min Impressions {_money(row['min_impressions'])}")

        msg = (
            f"✅ KPI beállítva: **{cname} · {acct['platform']}** ({written} kampány)\n"
            f"{' | '.join(bits)} | {thr}"
        )
        if bits2:
            msg += f"\n{' | '.join(bits2)}"
        if casc["skipped_override"]:
            msg += f"\nℹ️ {casc['skipped_override']} kampány kihagyva (kézi `/campaign kpi` override)"
        await interaction.followup.send(msg)

    @kpi.autocomplete("account")
    async def kpi_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """CSAK alerts csatornából ajánl fiókokat (az OM sajátjait); máshol üres."""
        owner = await asyncio.to_thread(
            users_storage.get_user_by_alerts_channel, str(interaction.channel_id)
        )
        if owner is None:
            return []
        return await self._owned_account_choices(owner, current)

    @kpi.autocomplete("accounts")
    async def kpi_accounts_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Az `accounts` (vesszős lista) mezőhöz: az UTOLSÓ token alapján, a saját
        fiókok közül; a választott #id-t a már beírt tokenek MÖGÉ fűzi."""
        owner = await asyncio.to_thread(
            users_storage.get_user_by_alerts_channel, str(interaction.channel_id)
        )
        if owner is None:
            return []
        parts = (current or "").split(",")
        active = parts[-1].strip()
        prefix = ",".join(p.strip() for p in parts[:-1])
        if prefix:
            prefix += ","
        base = await self._owned_account_choices(owner, active)
        return [
            app_commands.Choice(name=c.name, value=f"{prefix}{c.value}"[:100])
            for c in base
        ]

    # ------------------------------------------------------------------
    # /account summary-now account:<> — azonnali (élő) health check
    # ------------------------------------------------------------------
    @app_commands.command(
        name="summary-now",
        description="Azonnali összefoglaló ÉLŐ mai adatból egy fiókra (nem a tegnapiból)",
    )
    @app_commands.describe(
        account="Egy fiók: #id, külső azonosító VAGY kliensnév (pl. 2 / act_165… / Stopvill)",
    )
    async def summary_now(
        self,
        interaction: discord.Interaction,
        account: str,
    ) -> None:
        """Élő adatot húz a platform API-ból, majd lefuttatja az anomália-detektort.

        Hatókör — egy fiók: a teljes flotta ~100 ad_account, fiókonként egy
        batch API hívással az 3-5 perc lenne egyetlen parancsra. Egy fiókra
        1 hívás és néhány másodperc.

        Jogosultság: a saját #alerts csatornából a hozzád rendelt fiókokra,
        VAGY az admin csatornából bármelyik fiókra.

        Ez read-only pillanatkép: nem ír insightot, nem rögzít alertet és nem
        küld riasztást (különben duplázná az órás ciklus alertjeit).
        """
        await interaction.response.defer(ephemeral=True)

        channel_owner = await asyncio.to_thread(
            users_storage.get_user_by_alerts_channel, str(interaction.channel_id)
        )
        is_admin_ch = _is_admin_channel(interaction)

        if channel_owner is None and not is_admin_ch:
            await interaction.followup.send(
                "❌ Ez a parancs a saját #alerts csatornádból (saját fiókjaidra) "
                "vagy az admin csatornából (bármely fiókra) futtatható."
            )
            return

        acct = await self._resolve_single_account(interaction, account)
        if acct is None:
            return

        # Az #alerts csatornában csak a saját fiókok kérdezhetők le.
        if not is_admin_ch:
            owned = await asyncio.to_thread(
                account_assignments_storage.get_account_ids_for_user,
                channel_owner["id"],
            )
            if acct["id"] not in owned:
                await interaction.followup.send("❌ Ez a fiók nincs hozzád rendelve.")
                return

        client = await asyncio.to_thread(clients_storage.get_client, acct["client_id"])
        cname = (client or {}).get("name") or f"#{acct['client_id']}"

        await interaction.followup.send(
            f"⏳ Élő adatlekérés indul: **{cname}** · `{acct['platform']}` "
            f"`{_short_account(acct['external_account_id'])}` — ez néhány másodperc…"
        )

        try:
            summary = await generate_instant_account_summary(acct, client_name=cname)
        except Exception as exc:  # noqa: BLE001
            log.exception("Azonnali összefoglaló hiba (fiók #%s)", acct["id"])
            await interaction.followup.send(
                f"❌ Nem sikerült az élő adatlekérés: `{exc}`\n"
                f"Fiók: **{cname}** · `{acct['platform']}` "
                f"`{_short_account(acct['external_account_id'])}`"
            )
            return

        await asyncio.to_thread(
            audit.log_action, str(interaction.user.id), "account_summary_now",
            entity_type="ad_account", entity_id=acct["id"],
            details={
                "client_name": cname,
                "platform": acct["platform"],
                "campaigns": summary["total_campaigns"],
                "alerts": summary["alert_count"],
            },
        )
        # A teljes problémalista (nem top 5) átlépheti a Discord 2000
        # karakteres limitjét — darabolás nélkül a followup.send HTTP 400-zal
        # elszállna, azaz az EGÉSZ összefoglaló elveszne.
        for part in split_message(format_instant_summary(summary)):
            await interaction.followup.send(part)

    @summary_now.autocomplete("account")
    async def summary_now_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Alerts csatornában a saját fiókok, admin csatornában az összes."""
        owner = await asyncio.to_thread(
            users_storage.get_user_by_alerts_channel, str(interaction.channel_id)
        )
        if owner is not None:
            return await self._owned_account_choices(owner, current)
        if _is_admin_channel(interaction):
            return await self._account_choices(current)
        return []


async def setup(bot: commands.Bot) -> None:
    """Discord.py extension entry point — `bot.load_extension(...)` hívja."""
    await bot.add_cog(AdAccountsCog(bot))
