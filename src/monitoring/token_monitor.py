"""
Meta API token egészség-figyelő (17. lépés).

A Meta System User access token lejárhat (vagy visszavonható). Ha lejár, a
discovery és az óránkénti metrika-lekérés is leáll — ezért hetente ellenőrizzük
a token állapotát a Graph API `debug_token` végpontján, és proaktívan szólunk az
admin csatornán, MIELŐTT baj lenne.

Belépők:
    result = await check_meta_token_health()   # tiszta státusz-lekérdezés
    await token_health_check()                 # lekérdezés + admin értesítés

A heti ütemezést a scheduler `weekly_token_check` jobja végzi (hétfő 08:00).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from src.config import get_config
from src.integrations import discord_router
from src.utils.logging import get_logger

log = get_logger(__name__)

_DEBUG_TOKEN_URL = "https://graph.facebook.com/debug_token"
# Ennyi (vagy kevesebb) nap hátralévő élettartamnál már figyelmeztetünk.
_WARNING_DAYS = 7


async def check_meta_token_health() -> dict[str, Any]:
    """A Meta access token állapotának lekérdezése (debug_token).

    Visszatérés:
        {
            "valid":          bool,
            "expires_at":     datetime | None,   # None = nem jár le (pl. system user)
            "days_remaining": int | None,
            "scopes":         list[str],
            "error":          str | None,        # ha a lekérdezés maga hibázott
        }

    Soha nem dob: hálózati/parse/API hiba esetén valid=False + error kitöltve.
    """
    cfg = get_config()
    token = cfg.meta_access_token
    result: dict[str, Any] = {
        "valid": False,
        "expires_at": None,
        "days_remaining": None,
        "scopes": [],
        "error": None,
    }

    if not token:
        result["error"] = "META_ACCESS_TOKEN nincs beállítva"
        log.warning("Meta token health: nincs token beállítva")
        return result

    # A debug_token-hez app access token a legmegbízhatóbb ("app_id|app_secret");
    # ha az app adatai nincsenek meg, magával a tokennel próbáljuk.
    if cfg.meta_app_id and cfg.meta_app_secret:
        access_token = f"{cfg.meta_app_id}|{cfg.meta_app_secret}"
    else:
        access_token = token

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                _DEBUG_TOKEN_URL,
                params={"input_token": token, "access_token": access_token},
            )
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.error("Meta token health: hálózati/parse hiba: %s", exc)
        result["error"] = str(exc)
        return result

    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        log.error("Meta token health: API hiba: %s", msg)
        result["error"] = msg
        return result

    data = (body or {}).get("data") or {}
    result["valid"] = bool(data.get("is_valid"))
    result["scopes"] = data.get("scopes") or []

    expires_unix = data.get("expires_at")
    # expires_at == 0 → nem jár le (tipikusan system user token).
    if expires_unix and int(expires_unix) > 0:
        expires_dt = datetime.fromtimestamp(int(expires_unix), tz=timezone.utc)
        result["expires_at"] = expires_dt
        delta_seconds = (expires_dt - datetime.now(timezone.utc)).total_seconds()
        result["days_remaining"] = max(0, int(delta_seconds // 86400))

    log.info(
        "Meta token health: valid=%s, expires_at=%s, days_remaining=%s",
        result["valid"], result["expires_at"], result["days_remaining"],
    )
    return result


async def token_health_check() -> dict[str, Any]:
    """Token-ellenőrzés + proaktív admin-értesítés a Discord admin csatornán.

    - érvénytelen / lejárt token → 🔴 CRITICAL üzenet
    - <= 7 nap van hátra          → ⚠️ WARNING üzenet
    - egyébként                   → csak log (nincs Discord üzenet)

    Visszatérés: a `check_meta_token_health` eredménye (a job/teszt láthatja).
    """
    result = await check_meta_token_health()
    admin_channel = get_config().discord_admin_channel_id

    valid = result["valid"]
    days = result["days_remaining"]

    if not valid:
        content = (
            "🔴 **Meta API token LEJÁRT / érvénytelen!**\n"
            "A discovery és a metrika-lekérés leállt — azonnali cselekvés szükséges.\n"
            "BM → System Users → ppc_monitor → Generate Token"
        )
        if result.get("error"):
            content += f"\n*(részlet: {result['error']})*"
        await discord_router.send_text_message(admin_channel, content)
        log.error("Meta token health: ÉRVÉNYTELEN token — admin CRITICAL kiküldve")
    elif days is not None and days <= _WARNING_DAYS:
        content = (
            f"⚠️ **Meta token lejár {days} nap múlva!**\n"
            "BM → System Users → ppc_monitor → Generate Token"
        )
        await discord_router.send_text_message(admin_channel, content)
        log.warning("Meta token health: %d nap van hátra — admin WARNING kiküldve", days)
    else:
        log.info(
            "Meta token health OK (valid=%s, days_remaining=%s) — nincs értesítés",
            valid, days,
        )

    return result
