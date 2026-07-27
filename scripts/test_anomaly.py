"""
End-to-end anomália teszt EGY kampányra.

    python -m scripts.test_anomaly --campaign-id <id> [--keep-alert]

Mit csinál:
  1. Elmenti a kampány aktuális (hatékony) campaign_kpis állapotát.
  2. Ideiglenesen monthly_budget = 1 Ft-ot kényszerít (campaign-szintű KPI), így
     bármilyen valós költés → budget_depleted (CRITICAL).
  3. Lefuttatja az óránkénti monitoring per-kampány csővezetékét CSAK erre a
     kampányra: friss metrika-pull (vagy az utolsó tárolt insight) → detektor →
     alert beszúrás → routing (Discord + ClickUp).
  4. Ellenőrzi: keletkezett-e alert a DB-ben, kiment-e Discordra, készült-e
     ClickUp task.
  5. try/finally: a KPI MINDENKÉPP visszaáll (akkor is, ha hiba történt).
  6. A teszt-alertet alapból törli a végén (--keep-alert megtartja).

FONTOS — SZÁNDÉKOS ELTÉRÉS: NEM a globális `hourly_monitoring()` fut le, mert az
mind a ~69 fiókot lekérné és VALÓS riasztásokat küldene MINDEN OM-nek (+ ClickUp
taskok). Ehelyett a teljes csővezeték ugyanazon lépéseit futtatjuk egyetlen
kampányra szkópolva. A kiküldött üzenet "🧪 [E2E TEST]" előtaggal megy, hogy a
címzett egyértelműen lássa: ez teszt.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date

from src.config import get_config  # noqa: F401 — .env betöltése
from src.monitoring.detector import detect_anomalies_for_campaign
from src.monitoring.router import route_alert
from src.monitoring.scheduler import _batch_pull
from src.storage import ad_accounts as ad_accounts_storage
from src.storage import campaigns as campaigns_storage
from src.storage import insights as insights_storage
from src.storage import kpis as kpis_storage
from src.storage.alerts import insert_alert
from src.storage.supabase_client import get_supabase

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OK = "✅"
WARN = "⚠️"
FAIL = "❌"
_KPI_TABLE = "campaign_kpis"
_TEST_PREFIX = "🧪 [E2E TEST] "


# ---------------------------------------------------------------------------
# KPI kényszerítés / visszaállítás (try/finally biztosította)
# ---------------------------------------------------------------------------
def force_budget_one(campaign_id: int) -> dict:
    """monthly_budget = 1 Ft kényszerítése campaign-szinten. Visszaad egy
    `state` dict-et, amivel a `restore_kpi` pontosan visszaállít."""
    sb = get_supabase()
    active = kpis_storage.get_active_kpis(campaign_id)
    if active:
        state = {"mode": "update", "kpi_id": active["id"], "orig_budget": active.get("monthly_budget")}
        sb.table(_KPI_TABLE).update({"monthly_budget": 1}).eq("id", active["id"]).execute()
        return state

    # Nincs aktív campaign_kpis sor (a kampány örököl) → ideiglenes sort szúrunk be,
    # amit a végén TÖRLÜNK (így az öröklés visszaáll).
    res = (
        sb.table(_KPI_TABLE)
        .insert({
            "campaign_id": campaign_id,
            "is_active": True,
            "monthly_budget": 1,
            "inherited_from_client": False,
            "inherited_from_account": False,
        })
        .execute()
    )
    return {"mode": "insert", "kpi_id": res.data[0]["id"], "orig_budget": None}


def restore_kpi(state: dict) -> None:
    """A `force_budget_one` által módosított állapot visszaállítása."""
    if not state:
        return
    sb = get_supabase()
    if state["mode"] == "update":
        sb.table(_KPI_TABLE).update({"monthly_budget": state["orig_budget"]}).eq(
            "id", state["kpi_id"]
        ).execute()
    elif state["mode"] == "insert" and state.get("kpi_id"):
        sb.table(_KPI_TABLE).delete().eq("id", state["kpi_id"]).execute()


# ---------------------------------------------------------------------------
# Metrika feloldása (friss pull → fallback utolsó tárolt insight)
# ---------------------------------------------------------------------------
async def resolve_insight(campaign: dict) -> tuple[dict | None, str]:
    """A kampány aktuális metrikái. Először friss API pull a fiókra (mint az
    óránkénti ciklus), majd fallback az utolsó tárolt insightra."""
    account = ad_accounts_storage.get_ad_account(campaign.get("ad_account_id"))
    platform = (account or {}).get("platform")
    ext_account = (account or {}).get("external_account_id")
    ext_campaign = str(campaign.get("external_campaign_id"))

    if platform and ext_account:
        try:
            today = date.today().isoformat()
            insights = await _batch_pull(platform, ext_account, today)
            match = {str(i["external_campaign_id"]): i for i in insights}.get(ext_campaign)
            if match:
                return match, "friss API pull"
        except Exception as exc:  # noqa: BLE001 — pl. Google SDK hiány → fallback
            print(f"   ({WARN} friss pull nem sikerült: {exc} — fallback tárolt insightra)")

    stored = insights_storage.get_latest_insight(campaign["id"])
    if stored:
        return stored, "utolsó tárolt insight"
    return None, "nincs elérhető metrika"


def _existing_alert(campaign_id: int, metric: str) -> dict | None:
    res = (
        get_supabase()
        .table("alerts")
        .select("*")
        .eq("campaign_id", campaign_id)
        .eq("metric", metric)
        .order("detected_at", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


# ---------------------------------------------------------------------------
# E2E futtatás (szkópolt: egyetlen kampány)
# ---------------------------------------------------------------------------
async def run_e2e(campaign: dict, *, keep_alert: bool) -> int:
    cid = campaign["id"]
    name = campaign.get("name") or "?"
    lifecycle = (campaign.get("lifecycle_state") or "?").lower()
    print(f"\nKampány #{cid} — {name} (lifecycle={lifecycle})")

    insight, source = await resolve_insight(campaign)
    if not insight:
        print(f"{FAIL} Nincs elérhető metrika ({source}) — válassz aktív kampányt.")
        return 1

    spend = insight.get("spend")
    print(f"   Metrika forrása: {source} (spend={spend}, impressions={insight.get('impressions')})")

    anomalies = await detect_anomalies_for_campaign(cid, insight)
    metrics = [a["metric"] for a in anomalies]
    print(f"   Detektált anomáliák: {metrics or '—'}")

    target = next((a for a in anomalies if a["metric"] == "budget_depleted"), None)
    target = target or (anomalies[0] if anomalies else None)
    if not target:
        print(
            f"{WARN} Nem keletkezett anomália. Valószínű ok: spend=0 vagy a kampány "
            f"lifecycle-je nem aktív ({lifecycle}) / némítva van."
        )
        return 1

    # Teszt-jelölés a kiküldött üzeneten (a címzett lássa, hogy ez E2E teszt).
    test_message = _TEST_PREFIX + target["message"]

    alert_row = await asyncio.to_thread(
        insert_alert, cid, target["severity"], target["metric"],
        target.get("observed_value"), target.get("threshold_value"), test_message,
    )

    created_now = alert_row is not None
    if not created_now:
        existing = _existing_alert(cid, target["metric"])
        print(
            f"{WARN} Ma már van '{target['metric']}' alert erre a kampányra "
            f"(dedup, id: {existing.get('id') if existing else '?'}) — nem hozok létre duplikátumot."
        )
        if not existing:
            return 1
        alert_row = existing

    created_alert_id = alert_row.get("id") if created_now else None

    try:
        # 1) Alert a DB-ben
        print(f"{OK} Alert keletkezett (id: {alert_row.get('id')}, severity: {alert_row.get('severity')})")

        # 2) Routing (Discord + ClickUp) — csendes idő bypass, hogy bármikor menjen
        route = await route_alert(alert_row, bypass_quiet_hours=True)
        channels = route.get("channels") or []
        recipients = route.get("recipients") or []

        if "discord" in channels:
            who = f"{len(recipients)} címzett" if recipients else "admin fallback"
            print(f"{OK} Discord üzenet kiküldve ({who})")
        else:
            print(f"{FAIL} Discord üzenet NEM ment ki (ok: {route.get('reason') or 'ismeretlen'})")

        # 3) ClickUp
        if "clickup" in channels:
            print(f"{OK} ClickUp task létrehozva")
        elif target["severity"] != "critical":
            print(f"{WARN} ClickUp skip (csak CRITICAL alerthez készül task)")
        elif not get_config().clickup_api_token or not get_config().clickup_anomalies_list_id:
            print(f"{WARN} ClickUp skip (nincs CLICKUP_API_TOKEN / LIST_ID még)")
        else:
            print(f"{WARN} ClickUp task NEM jött létre (lásd a logot — token/jogosultság?)")
    finally:
        # Teszt-alert takarítás (a sajátunkat, ha most hoztuk létre)
        if created_alert_id and not keep_alert:
            get_supabase().table("alerts").delete().eq("id", created_alert_id).execute()
            print(f"{OK} Teszt-alert törölve (id: {created_alert_id})")
        elif created_alert_id:
            print(f"{WARN} Teszt-alert MEGTARTVA (--keep-alert, id: {created_alert_id})")

    return 0


# ---------------------------------------------------------------------------
# KPI a try/finally-ben mindenképp visszaáll
# ---------------------------------------------------------------------------
async def run_full(campaign: dict, *, keep_alert: bool) -> int:
    state: dict = {}
    rc = 1
    try:
        state = await asyncio.to_thread(force_budget_one, campaign["id"])
        print(f"{OK} KPI kényszerítve: monthly_budget = 1 Ft ({state['mode']})")
        rc = await run_e2e(campaign, keep_alert=keep_alert)
    finally:
        try:
            await asyncio.to_thread(restore_kpi, state)
            print(f"{OK} KPI visszaállítva")
        except Exception as exc:  # noqa: BLE001 — a visszaállítás hibája is látszódjon
            print(f"{FAIL} KPI visszaállítás HIBA: {exc} — ellenőrizd kézzel a campaign_kpis-t!")
    return rc


# ---------------------------------------------------------------------------
# Belépő — Discord klienst is bekötünk, hogy a routing VALÓBAN küldjön
# ---------------------------------------------------------------------------
async def main() -> int:
    parser = argparse.ArgumentParser(description="E2E anomália teszt egy kampányra.")
    parser.add_argument("--campaign-id", type=int, required=True, help="A kampány DB ID-ja")
    parser.add_argument("--keep-alert", action="store_true", help="Ne törölje a teszt-alertet a végén")
    parser.add_argument("--no-discord", action="store_true", help="Ne kössön be Discord klienst (a küldés kimarad)")
    args = parser.parse_args()

    print("=" * 52)
    print("  PPC Monitor — E2E anomália teszt")
    print("=" * 52)

    campaign = await asyncio.to_thread(campaigns_storage.get_campaign, args.campaign_id)
    if campaign is None:
        print(f"{FAIL} Nincs ilyen kampány: #{args.campaign_id}")
        return 1

    holder = {"rc": 1, "ran": False}

    # A discord_router egy ÉLŐ bot kliensen küld (set_client). Standalone scriptben
    # ezt nekünk kell bekötni — különben a routing "nincs kliens" miatt skippel.
    token = get_config().discord_bot_token
    if token and not args.no_discord:
        import discord

        from src.integrations.discord_router import set_client

        client = discord.Client(intents=discord.Intents.default())

        @client.event
        async def on_ready() -> None:
            try:
                set_client(client)
                print(f"{OK} Discord kliens csatlakozva ({client.user}) — routing élesben")
                holder["ran"] = True
                holder["rc"] = await run_full(campaign, keep_alert=args.keep_alert)
            finally:
                await client.close()

        try:
            await asyncio.wait_for(client.start(token), timeout=90)
        except Exception as exc:  # noqa: BLE001
            print(f"{WARN} Discord kliens hiba ({exc}) — kliens nélkül folytatom")
        # Az aiohttp connector a close() után még egy loop-ciklust kér a tiszta
        # bontáshoz (különben "Unclosed connector" figyelmeztetés).
        await asyncio.sleep(0.3)

    if not holder["ran"]:
        print(f"{WARN} Discord kliens nélkül futtatom (a Discord-küldés kimaradhat)")
        holder["rc"] = await run_full(campaign, keep_alert=args.keep_alert)

    print("=" * 52)
    return holder["rc"]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
