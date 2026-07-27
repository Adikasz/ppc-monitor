"""
Pre-deploy health check — minden külső függőség egy paranccsal ellenőrizve.

Railway deploy ELŐTT futtasd, hogy kiderüljön, minden API/kapcsolat él-e:

    python -m scripts.health_check

Hat kategóriát néz (Supabase, Meta, Google Ads, Discord, Anthropic, Scheduler).
Minden ellenőrzés read-only / mellékhatás-mentes:
  - Discord: bejelentkezik, ELLENŐRZI az admin csatorna írhatóságát (jogosultság),
    de NEM küld üzenetet.
  - Anthropic: 1 minimál hívás (max 10 token).
  - Meta: token-állapot (debug_token) + a Stopvill fiók kampányainak lekérése.
  - Scheduler: regisztrálja a jobokat, megszámolja, majd azonnal leáll (egy job
    sem fut le).

Kilépési kód: 0 ha nincs blokkoló HIBA (a ⚠️ figyelmeztetés nem blokkol),
különben 1.
"""
from __future__ import annotations

import asyncio
import sys

from src.config import get_config
from src.integrations.meta_ads import MetaAdsClient
from src.monitoring.token_monitor import check_meta_token_health
from src.storage.supabase_client import get_supabase

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OK = "✅"
WARN = "⚠️"
FAIL = "❌"

# A teszt-fiók, amin a Meta kampány-lekérést próbáljuk (a feladatból).
_STOPVILL_ACCOUNT = "act_165789803008898"

# A botnak kötelezően létező táblák (a séma-probe ezeket igazolta vissza).
_REQUIRED_TABLES = (
    "campaigns", "alerts", "users", "ad_accounts",
    "account_assignments", "ad_account_kpis", "campaign_insights", "client_kpis",
)


# ---------------------------------------------------------------------------
# 1) Supabase
# ---------------------------------------------------------------------------
def _supabase_sync() -> tuple[list[str], dict[str, int | None]]:
    sb = get_supabase()
    missing: list[str] = []
    counts: dict[str, int | None] = {}
    for table in _REQUIRED_TABLES:
        try:
            res = sb.table(table).select("id", count="exact").limit(1).execute()
            counts[table] = res.count
        except Exception:  # noqa: BLE001 — hiányzó/elérhetetlen tábla
            missing.append(table)
    return missing, counts


async def check_supabase() -> tuple[str, str]:
    try:
        missing, counts = await asyncio.to_thread(_supabase_sync)
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"kapcsolat hiba: {exc}"
    if missing:
        return FAIL, f"hiányzó tábla(k): {', '.join(missing)}"
    return OK, f"OK ({counts.get('campaigns')} kampány, {counts.get('ad_accounts')} fiók)"


# ---------------------------------------------------------------------------
# 2) Meta Ads
# ---------------------------------------------------------------------------
async def check_meta() -> tuple[str, str]:
    health = await check_meta_token_health()
    if not health.get("valid"):
        err = health.get("error") or "token érvénytelen/lejárt"
        return FAIL, f"token HIBA: {err} → BM → System Users → Generate Token"

    days = health.get("days_remaining")
    camps_detail = ""
    try:
        client = MetaAdsClient.get_instance()
        camps = await asyncio.to_thread(client.get_campaigns, _STOPVILL_ACCOUNT)
        camps_detail = f", Stopvill: {len(camps)} kampány"
    except Exception as exc:  # noqa: BLE001
        return WARN, f"token érvényes, de a Stopvill kampánylekérés hiba: {exc}"

    if days is None:
        return OK, f"OK (token érvényes, nem jár le{camps_detail})"
    if days <= 7:
        return WARN, f"token HAMAROSAN lejár (~{days} nap){camps_detail}"
    return OK, f"OK (token érvényes, ~{days} nap{camps_detail})"


# ---------------------------------------------------------------------------
# 3) Google Ads
# ---------------------------------------------------------------------------
async def check_google() -> tuple[str, str]:
    cfg = get_config()
    if not cfg.google_ads_login_customer_id:
        return WARN, "skip — nincs GOOGLE_ADS_LOGIN_CUSTOMER_ID (tokenek később jönnek)"
    try:
        from src.integrations.google_ads import GoogleAdsClient

        await asyncio.to_thread(GoogleAdsClient.get_instance)
    except RuntimeError as exc:
        # Tipikus: az SDK nincs telepítve (Python 3.14 wheel hiány) vagy hiányzó token.
        return WARN, f"skip — {exc}"
    except Exception as exc:  # noqa: BLE001
        return WARN, f"skip — {exc}"
    return OK, f"OK (MCC: {cfg.google_ads_login_customer_id})"


# ---------------------------------------------------------------------------
# 4) Discord
# ---------------------------------------------------------------------------
async def check_discord() -> tuple[str, str]:
    cfg = get_config()
    token = cfg.discord_bot_token
    if not token:
        return FAIL, "DISCORD_BOT_TOKEN hiányzik"

    import discord

    from src.integrations.discord_router import _parse_channel_id

    admin_raw = cfg.discord_admin_channel_id
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    outcome: dict[str, str] = {}

    @client.event
    async def on_ready() -> None:
        try:
            cid = _parse_channel_id(admin_raw)
            channel = None
            if cid is not None:
                channel = client.get_channel(cid) or await client.fetch_channel(cid)
            if channel is None:
                outcome["status"] = WARN
                outcome["detail"] = (
                    f"bejelentkezett ({client.user}), de az admin csatorna nem "
                    f"feloldható (DISCORD_ADMIN_CHANNEL_ID: {admin_raw!r})"
                )
            else:
                guild = getattr(channel, "guild", None)
                me = guild.me if guild else None
                perms = channel.permissions_for(me) if me else None
                writable = bool(perms and perms.send_messages)
                name = getattr(channel, "name", "?")
                outcome["status"] = OK if writable else WARN
                outcome["detail"] = (
                    f"OK ({client.user}, #{name}"
                    f"{' írható' if writable else ' — NINCS írásjog!'})"
                )
        except Exception as exc:  # noqa: BLE001
            outcome["status"] = WARN
            outcome["detail"] = f"bejelentkezett, de a csatorna-ellenőrzés hibázott: {exc}"
        finally:
            await client.close()

    try:
        await asyncio.wait_for(client.start(token), timeout=40)
    except asyncio.TimeoutError:
        await client.close()
        return FAIL, "időtúllépés a bejelentkezésnél (40s)"
    except discord.LoginFailure as exc:
        return FAIL, f"érvénytelen bot token: {exc}"
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"login hiba: {exc}"

    return outcome.get("status", WARN), outcome.get("detail", "ismeretlen állapot")


# ---------------------------------------------------------------------------
# 5) Anthropic
# ---------------------------------------------------------------------------
async def check_anthropic() -> tuple[str, str]:
    cfg = get_config()
    if not cfg.anthropic_api_key:
        return FAIL, "ANTHROPIC_API_KEY hiányzik"
    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=cfg.anthropic_api_key)
        msg = await client.messages.create(
            model=cfg.claude_model,
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        _ = msg.content
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"API hiba: {exc}"
    return OK, f"OK ({cfg.claude_model})"


# ---------------------------------------------------------------------------
# 6) Scheduler
# ---------------------------------------------------------------------------
async def check_scheduler() -> tuple[str, str]:
    from src.monitoring.scheduler import shutdown_scheduler, start_scheduler

    try:
        sched = start_scheduler()
        jobs = sched.get_jobs()
        ids = [j.id for j in jobs]
        shutdown_scheduler()
    except Exception as exc:  # noqa: BLE001
        try:
            shutdown_scheduler()
        except Exception:  # noqa: BLE001
            pass
        return FAIL, f"scheduler hiba: {exc}"
    return OK, f"OK ({len(jobs)} job: {', '.join(ids)})"


# ---------------------------------------------------------------------------
# Futtató
# ---------------------------------------------------------------------------
_CHECKS = (
    ("Supabase", check_supabase),
    ("Meta API", check_meta),
    ("Google Ads", check_google),
    ("Discord", check_discord),
    ("Anthropic", check_anthropic),
    ("Scheduler", check_scheduler),
)

_LINE = "=" * 52
_THIN = "-" * 52


async def main() -> int:
    print(_LINE)
    print("  PPC Monitor — Health Check")
    print(_LINE)

    results: list[tuple[str, str, str]] = []
    for name, fn in _CHECKS:
        try:
            status, detail = await fn()
        except Exception as exc:  # noqa: BLE001 — egy check hibája ne állítson le mindent
            status, detail = FAIL, f"váratlan hiba: {exc}"
        results.append((name, status, detail))
        print(f"{status} {name:<12} — {detail}")

    print(_THIN)
    ok = sum(1 for _, s, _ in results if s == OK)
    warn = sum(1 for _, s, _ in results if s == WARN)
    fail = sum(1 for _, s, _ in results if s == FAIL)
    total = len(results)

    if fail == 0:
        tail = f" ({warn} figyelmeztetés)" if warn else ""
        print(f"{OK} {ok}/{total} OK{tail} — kész a Railway deploy-ra")
    else:
        print(f"{FAIL} {ok}/{total} OK, {fail} HIBA — javítsd a hibákat a deploy előtt")
    print(_LINE)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
