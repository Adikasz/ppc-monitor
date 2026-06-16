"""
Csendes idő (quiet hours) — mikor NE menjen ki nem-kritikus riasztás.

A konfigurált időzónában (config.timezone) egy napi ablakot definiál
(QUIET_HOURS_START..QUIET_HOURS_END óra). Az ablakon belül a WARNING és INSIGHT
riasztásokat elnyomjuk (status='suppressed'), hogy ne zavarjuk a kollégákat
éjszaka; a CRITICAL riasztás MINDIG kimegy (a csendes idő nem blokkolja).

Az ablak átfordulhat éjfélen: pl. start=18, end=9 → 18:00-tól másnap 09:00-ig
csendes. Ha start == end, nincs csendes idő (mindig kimegy minden).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.config import get_config


def is_quiet_now(now: datetime | None = None) -> bool:
    """True, ha a megadott (vagy aktuális) időpont csendes időben van.

    Paraméter:
        now — opcionális időpont (tesztekhez). Ha None, a config időzónájában
              vett aktuális idő. Ha megadott és van tz-infója, átkonvertáljuk
              a config időzónájába.
    """
    cfg = get_config()
    tz = ZoneInfo(cfg.timezone or "UTC")

    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is not None:
        now = now.astimezone(tz)

    hour = now.hour
    start = cfg.quiet_hours_start
    end = cfg.quiet_hours_end

    if start == end:
        return False
    if start < end:
        # Nem fordul át éjfélen (pl. 1..6 → 01:00–06:00).
        return start <= hour < end
    # Átfordul éjfélen (pl. 18..9 → 18:00–09:00).
    return hour >= start or hour < end
