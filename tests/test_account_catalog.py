"""
Fiók-katalógus (a `/account add` és `/client onboard` account_id autocomplete forrása).

Kulcs-elvárások:
  - névre ÉS azonosítóra is keres (a felhasználó a nevet ismeri, nem az act_ ID-t)
  - cache-el (a Discord ~3 mp alatt vár választ, az élő API hívás lassú)
  - SOSEM dob kivételt — hiányzó SDK / token / API hiba esetén üres lista
    (egy kivétel az autocomplete-ben néma hibaként jelenne meg a Discordban)
"""
from __future__ import annotations

import pytest

from src.integrations import account_catalog as ac


_META_ACCOUNTS = [
    {"id": "act_668362820429286", "name": "Brands_Marquard Media", "status": 3},
    {"id": "act_3469840963104923", "name": "JOY-Napok_Marquard Media", "status": 1},
    {"id": "act_129819940882364", "name": "GTX reklama", "status": 1},
    {"id": "act_1872105117013351", "name": "LélekLiget Dabas", "status": 1},
]


@pytest.fixture(autouse=True)
def _clear_cache():
    ac.invalidate()
    yield
    ac.invalidate()


@pytest.fixture
def fake_meta(monkeypatch):
    """Meta fetcher kicserélve — hívásszámlálóval, hogy a cache-t mérni tudjuk."""
    calls = {"n": 0}

    def _fetch():
        calls["n"] += 1
        return list(_META_ACCOUNTS)

    monkeypatch.setitem(ac._FETCHERS, "meta", _fetch)
    return calls


def test_nevre_keres(fake_meta):
    m = ac.search_accounts("meta", "marquard")
    assert {a["name"] for a in m} == {
        "Brands_Marquard Media", "JOY-Napok_Marquard Media",
    }


def test_azonositora_is_keres(fake_meta):
    m = ac.search_accounts("meta", "129819940882364")
    assert len(m) == 1
    assert m[0]["name"] == "GTX reklama"


def test_kis_nagybetu_fuggetlen(fake_meta):
    assert ac.search_accounts("meta", "GTX") == ac.search_accounts("meta", "gtx")


def test_ures_query_mindent_ad_nev_szerint_rendezve(fake_meta):
    m = ac.search_accounts("meta", "")
    assert len(m) == len(_META_ACCOUNTS)
    names = [a["name"] for a in m]
    assert names == sorted(names, key=str.lower)


def test_exclude_ids_kiszuri_a_mar_regisztraltakat(fake_meta):
    m = ac.search_accounts(
        "meta", "", exclude_ids={"act_668362820429286", "act_129819940882364"}
    )
    ids = {a["id"] for a in m}
    assert "act_668362820429286" not in ids
    assert "act_129819940882364" not in ids
    assert len(m) == 2


def test_limit_betartva(fake_meta):
    assert len(ac.search_accounts("meta", "", limit=2)) == 2


def test_cache_nem_hiv_ujra(fake_meta):
    ac.get_accounts("meta")
    ac.get_accounts("meta")
    ac.get_accounts("meta")
    assert fake_meta["n"] == 1, "a cache ellenére többször hívtuk az API-t"


def test_force_refresh_ujra_hiv(fake_meta):
    ac.get_accounts("meta")
    ac.get_accounts("meta", force_refresh=True)
    assert fake_meta["n"] == 2


def test_invalidate_utan_ujra_hiv(fake_meta):
    ac.get_accounts("meta")
    ac.invalidate("meta")
    ac.get_accounts("meta")
    assert fake_meta["n"] == 2


def test_api_hiba_eseten_ures_lista_nem_kivetel(monkeypatch):
    """Hiányzó SDK / token / API hiba: az autocomplete nem törhet el."""
    def _boom():
        raise RuntimeError("A google-ads SDK nincs telepítve.")

    monkeypatch.setitem(ac._FETCHERS, "google", _boom)
    assert ac.get_accounts("google") == []
    assert ac.search_accounts("google", "bármi") == []


def test_api_hiba_eseten_lejart_cache_meg_mindig_jobb_mint_semmi(monkeypatch):
    """Ha egyszer sikerült a lekérés, egy későbbi hiba ne ürítse ki a listát."""
    state = {"fail": False}

    def _flaky():
        if state["fail"]:
            raise RuntimeError("átmeneti API hiba")
        return list(_META_ACCOUNTS)

    monkeypatch.setitem(ac._FETCHERS, "meta", _flaky)
    assert len(ac.get_accounts("meta")) == 4

    state["fail"] = True
    assert len(ac.get_accounts("meta", force_refresh=True)) == 4


def test_ismeretlen_platform_ures_lista():
    assert ac.get_accounts("tiktok") == []
    assert ac.search_accounts("tiktok", "x") == []
    assert ac.get_accounts("") == []
