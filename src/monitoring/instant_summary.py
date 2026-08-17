"""Azonnali (real-time) összefoglaló — `/account summary-now` és `/my summary-now`.

Különbség a `/summary type:daily`-hoz képest:

    /summary daily      — NEM indít adatlekérést; a MÁR TÁROLT alertekből
                          épít összefoglalót az utolsó lezárt napra (tegnap).
    /account summary-now — ÉLŐBEN lehúzza a MAI adatokat a platform API-ból
    /my summary-now       (1 batch hívás / fiók), lefuttatja rajtuk ugyanazt
                          az anomália-detektort, mint az óránkénti ciklus, és
                          azonnal visszaadja az eredményt.

Hatókör:
    `/account summary-now` — SZÁNDÉKOSAN egy fiók. A teljes flotta ~100
    ad_account, fiókonként egy batch API hívással ez 3-5 percet és ~100
    hívást jelentene egyetlen parancsra. Egy fiókra viszont 1 hívás és
    néhány másodperc.

    `/my summary-now` — egy OM ÖSSZES hozzárendelt fiókja (élő mérésben
    átlag ~10 fiók/OM). Ez a `generate_instant_summary_for_accounts`
    KORLÁTOZOTT PÁRHUZAMOSSÁGGAL (lásd `_MAX_CONCURRENT_ACCOUNTS`) futtatja
    a fiókonkénti lekéréseket — soros végrehajtással 10 fiókra 10-30
    másodperc jönne össze (fiókonként 1-3 mp), ami rossz UX, bár a Discord
    followup token 15 percig érvényes (nem a 3 mp-es kezdeti ACK a szűk
    keresztmetszet, azt a parancs `defer()`-je már lekezeli).

MELLÉKHATÁS-MENTES (fontos):
    Ez egy read-only health check. NEM ír a campaign_insights táblába, NEM
    szúr be alerteket és NEM küld riasztást senkinek — különben minden
    lefuttatás megduplázná az órás ciklus alertjeit és spamelné az OM-eket.
    A `detect_anomalies_for_campaign` maga is tiszta függvény: kiszámolja az
    anomáliákat, de nem perzisztál (az insert/route a scheduler dolga).

    Egy kivétel a lifecycle auto-promóció: azt NEM futtatjuk itt, mert az
    állapotot írna.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.config import get_config
from src.integrations.discord_router import issue_section_lines
from src.integrations.google_ads import GoogleAdsClient
from src.integrations.meta_ads import MetaAdsClient
from src.monitoring.detector import detect_anomalies_for_campaign
from src.storage import campaigns as campaigns_storage
from src.utils.logging import get_logger

log = get_logger(__name__)

# Ugyanaz a rangsor, mint a napi összefoglalóban.
_SEVERITY_RANK = {"critical": 0, "warning": 1, "insight": 2}

# NINCS "top N" vágás — ugyanaz az elvárás, mint a napi összefoglalónál:
# MINDEN CRITICAL és WARNING legyen látható. Korábban fix top-5 volt, ami a
# több fiókos `/my summary-now`-nál KÉTSZER is vágott: előbb fiókonként 5-re,
# majd az összesítést megint 5-re — 10 fiók × 12 anomáliából 5 sor maradt.
#
# Ez csak végső biztonsági plafon (mint summary._MAX_ISSUE_LINES). Ha életbe
# lép, NEM néma: az `issues_truncated` mezőt a formázó kiírja.
_MAX_ISSUE_LINES = 200

# /my summary-now: ennyi fiók lekérése futhat egyszerre. Egy OM-nek átlag
# ~10 fiókja van — 5-ös konkurenciával ez ~2 hullámban fut le (kb. a
# leglassabb fiók 2×-e, nem a fiókok számának összege).
_MAX_CONCURRENT_ACCOUNTS = 5

# Ezekben az állapotokban lévő kampányokat a detektor amúgy is kihagyná —
# az élő lekérés előtt szűrjük ki őket, hogy a "figyelt kampány" szám valós legyen.
_SKIP_LIFECYCLE = {"paused", "ended"}


async def _batch_pull(platform: str, ext_account_id: str, day: str) -> list[dict[str, Any]]:
    """Egy fiók összes kampányának MAI metrikái EGY API hívással."""
    if platform == "meta":
        client = MetaAdsClient.get_instance()
        return await asyncio.to_thread(
            client.get_all_campaigns_insights, ext_account_id, day, day
        )
    if platform == "google":
        client = GoogleAdsClient.get_instance()
        return await asyncio.to_thread(
            client.get_all_campaigns_metrics, ext_account_id, day, day
        )
    raise ValueError(f"Ismeretlen platform: {platform!r}")


async def generate_instant_account_summary(
    ad_account: dict[str, Any],
    *,
    client_name: str | None = None,
) -> dict[str, Any]:
    """Élő health check egy hirdetési fiókra.

    Paraméterek:
        ad_account  — ad_accounts sor (id, external_account_id, platform, ...)
        client_name — az ügyfél neve a top-problémák címkézéséhez

    Visszatérés (a napi összefoglalóval egyező kulcsok + real-time extrák):
        {
            "total_campaigns":   int,  — figyelt (nem paused/ended) kampányok
            "campaigns_with_data": int,— ezekre volt MA adat az API-ban
            "critical_count":    int,
            "warning_count":     int,
            "alert_count":       int,
            "healthy_campaigns": int,
            "top_issues":        [...],— MINDEN anomália (nem top N), a
                                       biztonsági plafonig
            "issues_truncated":  int,  — a plafon miatt kimaradt sorok száma
            "spend_today":       float,— a fiók mai összköltése
            "fetched_at":        str,  — a lekérés időpontja (ISO)
            "day":               str,  — melyik napra kértük (ma)
            "account_label":     str,
            "platform":          str,
            "is_live":           True, — élő adat (nem tárolt)
        }

    Raises:
        Exception — az API hívás hibáját TOVÁBBDOBJA, hogy a parancs
                    egyértelmű hibaüzenetet adhasson (szemben az órás
                    ciklussal, ami csendben átlép a következő fiókra).
    """
    platform = ad_account["platform"]
    ext_account_id = ad_account["external_account_id"]
    db_account_id = ad_account["id"]

    tz = ZoneInfo(get_config().timezone or "UTC")
    now = datetime.now(tz)
    day = date.today().isoformat()

    # 1) A fiók figyelt kampányai (a paused/ended nem számít bele)
    all_campaigns = await asyncio.to_thread(
        campaigns_storage.get_campaigns_by_ad_account, db_account_id
    )
    campaigns = [
        c for c in all_campaigns
        if c.get("is_monitored")
        and (c.get("lifecycle_state") or "new").lower() not in _SKIP_LIFECYCLE
    ]

    # 2) ÉLŐ adatlekérés — 1 batch hívás a fiókra
    insights = await _batch_pull(platform, ext_account_id, day)
    insights_map = {str(i["external_campaign_id"]): i for i in insights}

    log.info(
        "Azonnali összefoglaló: fiók=%s (%s) — %d figyelt kampány, "
        "%d kampányra jött MAI adat",
        ext_account_id, platform, len(campaigns), len(insights_map),
    )

    # 3) Detektor futtatása — ugyanaz, mint az órás ciklusban, de MENTÉS NÉLKÜL
    critical_count = 0
    warning_count = 0
    all_anomalies: list[dict[str, Any]] = []
    campaigns_with_data = 0
    campaigns_with_alerts: set[int] = set()
    spend_today = 0.0

    for campaign in campaigns:
        insight = insights_map.get(str(campaign.get("external_campaign_id")))
        if not insight:
            continue
        campaigns_with_data += 1
        try:
            spend_today += float(insight.get("spend") or 0.0)
        except (TypeError, ValueError):
            pass

        try:
            anomalies = await detect_anomalies_for_campaign(campaign["id"], insight)
        except Exception as exc:  # noqa: BLE001 — egy kampány hibája ne bukjon meg mindent
            log.error(
                "Azonnali összefoglaló: detektor hiba (kampány #%s): %s",
                campaign.get("id"), exc,
            )
            continue

        for a in anomalies:
            severity = (a.get("severity") or "").lower()
            if severity == "critical":
                critical_count += 1
            elif severity == "warning":
                warning_count += 1
            campaigns_with_alerts.add(campaign["id"])
            all_anomalies.append({**a, "_campaign_name": campaign.get("name")})

    # 4) Problémalista — a napi összefoglalóval azonos szerkezetben, teljes
    #    listával (csak a biztonsági plafon vág).
    ordered = sorted(
        all_anomalies,
        key=lambda a: _SEVERITY_RANK.get((a.get("severity") or "").lower(), 99),
    )
    account_label = (
        ad_account.get("account_name") or ext_account_id
    )
    top_issues = [
        {
            "client": client_name,
            "campaign": a.get("_campaign_name") or f"#{a.get('campaign_id')}",
            "platform": platform,
            "account_label": account_label,
            "severity": (a.get("severity") or "").lower(),
            "message": a.get("message") or a.get("metric") or "",
            # ÉLŐ pillanatkép: a detektor most, ebben a pillanatban találta az
            # anomáliát (a tárolt alerteknél a DB tölti a detected_at-et — itt
            # nincs DB-írás, lásd modul-docstring). A lekérés ideje = az
            # észlelés ideje, így a sorok ugyanúgy kapnak időbélyeget, mint a
            # napi összefoglalóban.
            "detected_at": now.isoformat(),
        }
        for a in ordered[:_MAX_ISSUE_LINES]
    ]

    issues_truncated = max(0, len(ordered) - len(top_issues))
    if issues_truncated:
        log.warning(
            "Azonnali összefoglaló (%s): %d anomáliából csak %d fér a listába "
            "(biztonsági plafon: %d)",
            ext_account_id, len(ordered), len(top_issues), _MAX_ISSUE_LINES,
        )

    return {
        "total_campaigns": len(campaigns),
        "campaigns_with_data": campaigns_with_data,
        # A critical_count / warning_count MINDIG a teljes összesítés — nem a
        # `top_issues` megjelenített sorainak száma.
        "critical_count": critical_count,
        "warning_count": warning_count,
        "alert_count": len(all_anomalies),
        "healthy_campaigns": max(0, len(campaigns) - len(campaigns_with_alerts)),
        "top_issues": top_issues,
        "issues_truncated": issues_truncated,
        "spend_today": round(spend_today, 2),
        "fetched_at": now.isoformat(),
        "day": day,
        "account_label": account_label,
        "platform": platform,
        "is_live": True,
    }


def _single_issue_line(issue: dict[str, Any]) -> str:
    """Problémasor EGY fiók összefoglalójában: `• Ügyfél — kampány — üzenet`.

    Fiók/platform címke nélkül: az egyfiókos nézet fejléce már tartalmazza.
    """
    client = issue.get("client") or "?"
    return f"• {client} — {issue.get('campaign', '?')} — {issue.get('message', '')}"


def _multi_issue_line(issue: dict[str, Any]) -> str:
    """Problémasor a több fiókos nézetben: `• Ügyfél [fiók] — kampány — üzenet`."""
    client = issue.get("client") or "?"
    account_label = issue.get("account_label")
    tag = f" [{account_label}]" if account_label else ""
    return f"• {client}{tag} — {issue.get('campaign', '?')} — {issue.get('message', '')}"


def format_instant_summary(summary: dict[str, Any]) -> str:
    """Az azonnali összefoglaló Discord-szövege.

    A napi összefoglaló formátumát követi (kritikus/warning szám, problémalista,
    figyelt kampányok), de egyértelműen jelzi, hogy ÉLŐ, mai adatról van szó, és
    hogy a nap még nem zárult le.

    A problémalista NINCS top-N-re vágva, és a napi összefoglalóval AZONOS
    logikával jelenik meg (`issue_section_lines`): rövid listánál lapos
    felsorolás, 15 fölött súlyosság szerinti csoportosítás darabszámmal.
    A 2000 karakteres Discord-limitet a hívó parancs kezeli `split_message`-dzsel.
    """
    total = summary.get("total_campaigns", 0)
    with_data = summary.get("campaigns_with_data", 0)
    crit = summary.get("critical_count", 0)
    warn = summary.get("warning_count", 0)
    healthy = summary.get("healthy_campaigns", 0)
    alert_count = summary.get("alert_count", 0)
    top_issues = summary.get("top_issues") or []
    label = summary.get("account_label", "?")
    platform = (summary.get("platform") or "").upper()
    spend = summary.get("spend_today", 0.0)

    fetched = str(summary.get("fetched_at") or "")[:16].replace("T", " ")
    # Ezres tagolás szóközzel — CSAK a számon (a sor többi vesszőjét nem bántjuk).
    spend_str = f"{spend:,.0f}".replace(",", " ")

    header = f"⚡ **Azonnali összefoglaló** — {label} [{platform}]"
    meta_line = f"🔄 *Élő adat, lekérve: {fetched}* · mai költés: **{spend_str}**"

    if with_data == 0:
        return (
            f"{header}\n{meta_line}\n\n"
            f"ℹ️ Ma még egyik figyelt kampányra sincs adat a platform API-ban "
            f"(**{total}** figyelt kampány).\n"
            f"Ez a nap elején normális — a Meta/Google insights késve frissül."
        )

    if alert_count == 0:
        return (
            f"✅ {header}\n{meta_line}\n\n"
            f"Jelenleg nincs anomália. Mind a **{with_data}** ma adatot adó "
            f"kampány rendben. 🎉\n"
            f"**Kampányok figyelve:** {total}"
        )

    lines = [
        header,
        meta_line,
        "",
        f"🔴 Kritikus: {crit}  |  🟡 Figyelmeztetés: {warn}  |  ✅ Egészséges: {healthy}",
    ]

    if top_issues:
        lines.append("")
        lines.append("**Problémák MOST:**")
        # A fiók és a platform már a fejlécben van — a soroknak nem kell újra.
        lines.extend(issue_section_lines(top_issues, summary, line_fn=_single_issue_line))

    lines.append("")
    lines.append(
        f"**Kampányok figyelve:** {total} "
        f"(ma adattal: {with_data})  |  **Anomáliák most:** {alert_count}"
    )
    lines.append(
        "─────────────\n"
        "*A nap még nem zárult le — ezek élő, részleges napi adatok. "
        "Riasztás nem került rögzítésre, ez csak pillanatkép.*"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Több fiók összesítve — `/my summary-now`
# ---------------------------------------------------------------------------

async def generate_instant_summary_for_accounts(
    accounts: list[dict[str, Any]],
    *,
    client_names: dict[int, str] | None = None,
    max_concurrency: int = _MAX_CONCURRENT_ACCOUNTS,
) -> dict[str, Any]:
    """Több fiók azonnali összefoglalója, KORLÁTOZOTT PÁRHUZAMOSSÁGGAL.

    Az `/my summary-now` ezt hívja az OM összes hozzárendelt fiókjára.
    Soros végrehajtással 10 fiókra (fiókonként 1-3 mp) 10-30 másodperc jönne
    össze; `max_concurrency` egyszerre futó lekéréssel ez kb. a leglassabb
    fiók futásidejének 2×-ére csökken, nem a fiókok számának összegére.

    Hiba-izoláció: EGY fiók API hibája (pl. lejárt token, jogosultsági hiba)
    NEM buktatja meg a többit — a hibás fiók az `errors` listába kerül, a
    többi eredmény érvényes marad (ugyanaz a minta, mint a
    `discover_campaigns_for_client` fault isolation-jénél).

    Visszatérés:
        {
            "accounts_total":  int,   — hány fiókra próbáltunk lekérni
            "accounts_ok":     int,   — hány sikerült
            "accounts_failed": int,
            "errors":          [{"account_label": str, "error": str}, ...],
            "total_campaigns": int,   — az összes SIKERES fiók figyelt kampányainak összege
            "campaigns_with_data": int,
            "critical_count":  int,
            "warning_count":   int,
            "healthy_campaigns": int,
            "alert_count":     int,
            "spend_today":     float,
            "per_account":     [...],  — fiókonkénti bontás (csak a sikeresek)
            "top_issues":      [...],  — az ÖSSZES fiók MINDEN anomáliája
                                         (nem top N), súlyosság szerint rendezve
            "issues_truncated": int,   — a biztonsági plafon miatt kimaradt sorok
            "fetched_at":      str,
            "is_live":         True,
        }
    """
    client_names = client_names or {}
    tz = ZoneInfo(get_config().timezone or "UTC")
    now = datetime.now(tz)

    if not accounts:
        return {
            "accounts_total": 0, "accounts_ok": 0, "accounts_failed": 0,
            "errors": [], "total_campaigns": 0, "campaigns_with_data": 0,
            "critical_count": 0, "warning_count": 0, "healthy_campaigns": 0,
            "alert_count": 0, "spend_today": 0.0, "per_account": [],
            "top_issues": [], "fetched_at": now.isoformat(), "is_live": True,
        }

    sem = asyncio.Semaphore(max_concurrency)

    async def _one(acct: dict[str, Any]) -> tuple[str, dict[str, Any], Any]:
        label = acct.get("account_name") or acct.get("external_account_id") or f"#{acct.get('id')}"
        async with sem:
            try:
                s = await generate_instant_account_summary(
                    acct, client_name=client_names.get(acct.get("client_id"))
                )
                return ("ok", acct, s)
            except Exception as exc:  # noqa: BLE001 — egy fiók hibája ne bukjon meg mindent
                log.error(
                    "Azonnali (multi) összefoglaló: fiók hiba (%s): %s", label, exc,
                )
                return ("error", acct, str(exc))

    results = await asyncio.gather(*[_one(a) for a in accounts])

    ok = [(a, s) for status, a, s in results if status == "ok"]
    failed = [(a, err) for status, a, err in results if status == "error"]

    top_issues_all: list[dict[str, Any]] = []
    per_account: list[dict[str, Any]] = []
    total_campaigns = campaigns_with_data = critical = warning = healthy = alert_count = 0
    spend_today = 0.0

    for acct, s in ok:
        total_campaigns += s["total_campaigns"]
        campaigns_with_data += s["campaigns_with_data"]
        critical += s["critical_count"]
        warning += s["warning_count"]
        healthy += s["healthy_campaigns"]
        alert_count += s["alert_count"]
        spend_today += s["spend_today"]
        top_issues_all.extend(s["top_issues"])
        per_account.append({
            "account_label": s["account_label"],
            "platform": s["platform"],
            "total_campaigns": s["total_campaigns"],
            "critical_count": s["critical_count"],
            "warning_count": s["warning_count"],
            "alert_count": s["alert_count"],
        })

    top_issues_all.sort(
        key=lambda i: _SEVERITY_RANK.get((i.get("severity") or "").lower(), 99)
    )
    top_issues = top_issues_all[:_MAX_ISSUE_LINES]

    # A kimaradt sorok az `alert_count`-hoz (a fiókok TELJES anomália-számához)
    # képest értendők — így egyszerre fedi a fiókonkénti és az összesítés-szintű
    # plafont, akkor is, ha mindkettő életbe lépett.
    issues_truncated = max(0, alert_count - len(top_issues))
    if issues_truncated:
        log.warning(
            "Azonnali (multi) összefoglaló: %d anomáliából csak %d fér a listába "
            "(biztonsági plafon: %d)",
            alert_count, len(top_issues), _MAX_ISSUE_LINES,
        )

    return {
        "accounts_total": len(accounts),
        "accounts_ok": len(ok),
        "accounts_failed": len(failed),
        "errors": [
            {
                "account_label": a.get("account_name") or a.get("external_account_id"),
                "error": err,
            }
            for a, err in failed
        ],
        "total_campaigns": total_campaigns,
        "campaigns_with_data": campaigns_with_data,
        "critical_count": critical,
        "warning_count": warning,
        "healthy_campaigns": healthy,
        "alert_count": alert_count,
        "spend_today": round(spend_today, 2),
        "per_account": per_account,
        "top_issues": top_issues,
        "issues_truncated": issues_truncated,
        "fetched_at": now.isoformat(),
        "is_live": True,
    }


def format_instant_summary_multi(summary: dict[str, Any], *, owner_label: str | None = None) -> str:
    """Az `/my summary-now` (több fiók) Discord-szövege.

    Fiókonkénti bontást csak a PROBLÉMÁS fiókokra mutat (sok fióknál a "minden
    OK" sorok zajt jelentenének) — a rendben lévő fiókok darabszáma összesítve
    jelenik meg.
    """
    total_accounts = summary.get("accounts_total", 0)
    ok_accounts = summary.get("accounts_ok", 0)
    failed = summary.get("errors") or []
    total = summary.get("total_campaigns", 0)
    with_data = summary.get("campaigns_with_data", 0)
    crit = summary.get("critical_count", 0)
    warn = summary.get("warning_count", 0)
    healthy = summary.get("healthy_campaigns", 0)
    alert_count = summary.get("alert_count", 0)
    spend = summary.get("spend_today", 0.0)
    top_issues = summary.get("top_issues") or []
    per_account = summary.get("per_account") or []

    fetched = str(summary.get("fetched_at") or "")[:16].replace("T", " ")
    spend_str = f"{spend:,.0f}".replace(",", " ")
    who = f" — {owner_label}" if owner_label else ""

    header = f"⚡ **Azonnali összefoglaló{who}** — {ok_accounts}/{total_accounts} fiók"
    meta_line = f"🔄 *Élő adat, lekérve: {fetched}* · mai összköltés: **{spend_str}**"

    lines = [header, meta_line, ""]

    if failed:
        lines.append(
            f"⚠️ {len(failed)} fióknál nem sikerült az élő lekérés: "
            + ", ".join(f"{e['account_label']}" for e in failed)
        )
        lines.append("")

    if ok_accounts == 0:
        lines.append("❌ Egyik fiókra sem sikerült élő adatot lekérni.")
        return "\n".join(lines)

    if with_data == 0:
        lines.append(
            f"ℹ️ Ma még egyik figyelt kampányra sincs adat a platform API-ban "
            f"(**{total}** figyelt kampány, {ok_accounts} fiókon)."
        )
        return "\n".join(lines)

    if alert_count == 0:
        lines[0] = f"✅ {header}"
        lines.append(
            f"Jelenleg nincs anomália egyik fiókban sem. Mind a **{with_data}** "
            f"ma adatot adó kampány rendben. 🎉"
        )
        lines.append(f"**Kampányok figyelve összesen:** {total}")
        return "\n".join(lines)

    lines.append(
        f"🔴 Kritikus: {crit}  |  🟡 Figyelmeztetés: {warn}  |  ✅ Egészséges: {healthy}"
    )

    problem_accounts = [a for a in per_account if a["alert_count"] > 0]
    if problem_accounts:
        lines.append("")
        lines.append("**Fiókonkénti bontás (csak problémás fiókok):**")
        for a in sorted(problem_accounts, key=lambda a: -a["critical_count"]):
            lines.append(
                f"• {a['account_label']} [{a['platform'].upper()}] — "
                f"🔴 {a['critical_count']} · 🟡 {a['warning_count']} "
                f"({a['total_campaigns']} kampány figyelve)"
            )
        n_clean = ok_accounts - len(problem_accounts)
        if n_clean > 0:
            lines.append(f"　+ {n_clean} fiók rendben")

    if top_issues:
        lines.append("")
        lines.append("**Problémák MOST (összes fiókból):**")
        # Több fiók keveredik → a sorban ott a fiók címkéje is.
        lines.extend(issue_section_lines(top_issues, summary, line_fn=_multi_issue_line))

    lines.append("")
    lines.append(
        f"**Kampányok figyelve:** {total} (ma adattal: {with_data})  |  "
        f"**Anomáliák most:** {alert_count}"
    )
    lines.append(
        "─────────────\n"
        "*A nap még nem zárult le — ezek élő, részleges napi adatok. "
        "Riasztás nem került rögzítésre, ez csak pillanatkép.*"
    )
    return "\n".join(lines)
