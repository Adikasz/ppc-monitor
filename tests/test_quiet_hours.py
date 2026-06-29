"""
Csendes idő (quiet hours) unit tesztek — 17:00–08:00 hétköznap + teljes hétvége.

A `get_config`-ot fixáljuk (start=17, end=8, Europe/Budapest), hogy a logika a
környezeti változóktól függetlenül determinisztikus legyen. Ismert dátum-horgonyok:
    2024-01-01 = hétfő (weekday 0), 2024-01-06 = szombat (5), 2024-01-07 = vasárnap (6).
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from src.utils import quiet_hours

_TZ = ZoneInfo("Europe/Budapest")
_CFG = SimpleNamespace(timezone="Europe/Budapest", quiet_hours_start=17, quiet_hours_end=8)


def _at(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=_TZ)


def _is_quiet(dt) -> bool:
    with mock.patch.object(quiet_hours, "get_config", return_value=_CFG):
        return quiet_hours.is_quiet_now(dt)


# --- Hétköznap óra-ablak (hétfő) --------------------------------------------

def test_weekday_workhours_not_quiet():
    # 16:00 hétfő → munkaidő, NEM csendes
    assert _is_quiet(_at(2024, 1, 1, 16)) is False


def test_weekday_after_17_quiet():
    # 17:01 hétfő → csendes
    assert _is_quiet(_at(2024, 1, 1, 17, 1)) is True


def test_weekday_0800_not_quiet():
    # 08:00 hétfő → munkaidő kezdete (end=8), NEM csendes
    assert _is_quiet(_at(2024, 1, 1, 8, 0)) is False


def test_weekday_early_morning_quiet():
    # 07:30 hétfő → még csendes
    assert _is_quiet(_at(2024, 1, 1, 7, 30)) is True


# --- Hétvége: egész nap csendes ---------------------------------------------

def test_saturday_midday_quiet():
    # 12:00 szombat → csendes (hétvége), pedig óra szerint munkaidő lenne
    assert _is_quiet(_at(2024, 1, 6, 12)) is True


def test_sunday_midday_quiet():
    assert _is_quiet(_at(2024, 1, 7, 12)) is True


def test_saturday_workhour_still_quiet():
    # 10:00 szombat → a hétvége felülírja az óra-ablakot
    assert _is_quiet(_at(2024, 1, 6, 10)) is True
