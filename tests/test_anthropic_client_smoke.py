"""
Anthropic SDK smoke — létrejön-e a Claude kliens a TÉNYLEGESEN telepített csomagokkal.

Miért kell erre külön teszt:

A Claude hívásokat MINDEN meglévő teszt az SDK határa FÖLÖTT gúnyolja ki
(`war.generate_weekly_analysis`, `ai_insights.generate_ai_insight`). Ezért a
tesztek zöldek maradtak akkor is, amikor élesben egyetlen AI hívás sem ment át:
a `httpx` 0.28.0 eltávolította a `proxies` paramétert, amit az `anthropic`
0.39.0 még feltétel nélkül átad, így már a KLIENS LÉTREHOZÁSA elhasalt:

    AsyncClient.__init__() got an unexpected keyword argument 'proxies'

A hiba el volt kapva (fault isolation), a napi insight scan végigfutott — csak
éppen AI-narratíva nélkül, kampányonként egy ERROR sorral. Kódváltozás nem is
történt: egy Railway újratelepítés húzta be az újabb httpx-et. Hetekig
kizárólag a Railway logból lehetett volna kideríteni.

Ezért ezek a tesztek SZÁNDÉKOSAN nem mockolják sem az `anthropic`, sem a `httpx`
csomagot: a VALÓDI konstruktort futtatják, mégpedig a két éles hívási úton
keresztül (`ai_insights` és `weekly_action_report`), nem egy külön kis
másolaton. Hálózat és valódi API kulcs nélkül futnak — az `AsyncAnthropic()`
nem hív ki, csak HTTP klienst épít.

Az élő (hálózatot és kvótát fogyasztó) minimál hívás opt-in, lásd a fájl végét.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from anthropic import AsyncAnthropic

from src.monitoring import ai_insights
from src.monitoring import weekly_action_report as war

# Az élő API hívást végző teszt csak akkor fut, ha ezt kifejezetten kéred.
# Alapból NEM: a `src.config` betölti a `.env`-et, tehát a kulcs jelenléte
# önmagában még nem jelenti azt, hogy a fejlesztő hálózati hívást szeretne
# minden `pytest` futtatáskor.
_LIVE_ENV = "PPC_LIVE_API_TESTS"

_FAKE_KEY = "sk-ant-teszt-nem-valodi-kulcs"


@pytest.fixture(autouse=True)
def _reset_client_singletons():
    """A modul-szintű kliens singletonok ne szivárogjanak át a tesztek között."""
    ai_insights._client = None
    war._client = None
    yield
    ai_insights._client = None
    war._client = None


# ---------------------------------------------------------------------------
# 1) A kliens ténylegesen létrehozható — a két éles hívási úton
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_insight_client_is_constructible_with_the_installed_httpx():
    """A napi insight scan Claude kliense — VALÓDI AsyncAnthropic, mock nélkül."""
    cfg = SimpleNamespace(anthropic_api_key=_FAKE_KEY)

    with mock.patch.object(ai_insights, "get_config", return_value=cfg):
        client = ai_insights._get_client()

    assert isinstance(client, AsyncAnthropic)
    await client.close()


@pytest.mark.asyncio
async def test_the_weekly_report_client_is_constructible_with_the_installed_httpx():
    """A heti riport Claude kliense — külön singleton, ezért külön ellenőrizzük.

    A `weekly_action_report._get_client` az `ai_insights._get_client` MÁSOLATA
    (mindkét modul kommentje utal is rá). Amíg két példány van belőle, mindkettőt
    külön kell smoke-olni: egy jövőbeli javítás simán csak az egyiket érintheti.
    """
    cfg = SimpleNamespace(anthropic_api_key=_FAKE_KEY)

    with mock.patch.object(war, "get_config", return_value=cfg):
        client = war._get_client()

    assert isinstance(client, AsyncAnthropic)
    await client.close()


# ---------------------------------------------------------------------------
# 2) A javítás maga ne tűnhessen el némán
# ---------------------------------------------------------------------------

def test_httpx_has_an_explicit_version_constraint_in_requirements():
    """A `httpx` verziója legyen KIMONDVA a requirements.txt-ben.

    A hiba gyökere nem az volt, hogy rossz httpx verziót választottunk, hanem
    hogy EGYÁLTALÁN NEM választottunk: tranzitív függőségként a Railway build
    mindig a legfrissebbet húzta be. Ez a teszt nem egy konkrét verziót ír elő
    (az anthropic SDK emelésekor a sáv jogosan mozdul), csak azt, hogy a pin
    ne kerüljön vissza a véletlen kezébe.
    """
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(
        encoding="utf-8"
    )
    pins = [
        line for line in req.splitlines()
        if re.match(r"^httpx(\[[^\]]+\])?\s*(==|~=|>=|<=|<|>)", line.strip())
    ]
    assert pins, (
        "A httpx-nek explicit verzió-megkötése kell a requirements.txt-ben. "
        "Pin nélkül a deploy a legfrissebb httpx-et telepíti, és az anthropic "
        "SDK-val való inkompatibilitás (pl. a 0.28-ban eltávolított `proxies` "
        "paraméter) csak élesben, a Railway logban derül ki."
    )


# ---------------------------------------------------------------------------
# 3) Élő minimál hívás — opt-in (hálózat + kvóta)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv(_LIVE_ENV) != "1",
    reason=f"Élő Claude hívás — futtatáshoz: {_LIVE_ENV}=1 pytest ...",
)
async def test_a_minimal_live_claude_call_succeeds():
    """Egy valódi, 10 tokenes Claude hívás.

    UGYANAZT a függvényt hívja, amit a deploy előtti health check
    (`python -m scripts.health_check`) — nincs külön másolat a hívásból, hogy a
    kettő ne tudjon elsodródni egymástól.
    """
    from scripts.health_check import OK, check_anthropic

    status, detail = await check_anthropic()
    assert status == OK, detail
