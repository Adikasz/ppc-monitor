"""
scripts/backfill_account_names.py — visszamenőleges account_name kitöltés.

Kulcs-elvárások:
  - katalógus-forrás elsőbbséget kap a direkt lekérdezés előtt (olcsóbb)
  - ha egy fiók sem a katalógusban, sem direktben nem oldható fel, NEM ír
    a DB-be, és a riportban felsorolásra kerül
  - --dry-run esetén SOHA nem hívja a DB-írást (set_account_name)
  - fiókok, amiknek MÁR van account_name-je, ki sem kerülnek feldolgozásra
"""
from __future__ import annotations

import sys
from unittest import mock

import pytest

from scripts import backfill_account_names as backfill


@pytest.fixture
def fake_accounts():
    return [
        {"id": 1, "platform": "meta", "external_account_id": "act_100", "account_name": None},
        {"id": 2, "platform": "meta", "external_account_id": "act_200", "account_name": None},
        {"id": 3, "platform": "google", "external_account_id": "300", "account_name": None},
        {"id": 4, "platform": "meta", "external_account_id": "act_400", "account_name": "Már van neve"},
    ]


def _run(monkeypatch, argv, *, accounts, catalog_meta, catalog_google, direct_fetch_results):
    monkeypatch.setattr(sys, "argv", ["backfill_account_names.py", *argv])
    monkeypatch.setattr(
        backfill.ad_accounts_storage, "list_ad_accounts", lambda: accounts,
    )

    def _get_accounts(platform, **kwargs):
        return catalog_meta if platform == "meta" else catalog_google
    monkeypatch.setattr(backfill.account_catalog, "get_accounts", _get_accounts)

    def _direct(platform, ext_id):
        return direct_fetch_results.get((platform, ext_id))
    monkeypatch.setattr(backfill, "_direct_fetch", _direct)

    set_name = mock.Mock()
    monkeypatch.setattr(backfill.ad_accounts_storage, "set_account_name", set_name)
    return set_name


def test_katalogusbol_feloldott_fiokokra_ir(monkeypatch, fake_accounts, capsys):
    set_name = _run(
        monkeypatch, ["--yes"],
        accounts=fake_accounts,
        catalog_meta=[{"id": "act_100", "name": "Fiok Száz"}],
        catalog_google=[{"id": "300", "name": "Google Háromszáz"}],
        direct_fetch_results={},
    )
    rc = backfill.main()
    assert rc == 0

    set_name.assert_any_call(1, "Fiok Száz")
    set_name.assert_any_call(3, "Google Háromszáz")
    # #4-nek mar van neve -> nem szerepelhet a hivasok kozott
    assert all(call.args[0] != 4 for call in set_name.call_args_list)


def test_katalogus_hianyaban_direkt_lekerdezes_fallback(monkeypatch, fake_accounts, capsys):
    set_name = _run(
        monkeypatch, ["--yes"],
        accounts=fake_accounts,
        catalog_meta=[{"id": "act_100", "name": "Fiok Száz"}],  # act_200 NINCS a katalógusban
        catalog_google=[],
        direct_fetch_results={("meta", "act_200"): "Direktben talalt nev"},
    )
    backfill.main()

    set_name.assert_any_call(2, "Direktben talalt nev")


def test_sem_katalogus_sem_direkt_nem_ir_dbbe(monkeypatch, fake_accounts, capsys):
    set_name = _run(
        monkeypatch, ["--yes"],
        accounts=fake_accounts,
        catalog_meta=[],
        catalog_google=[],
        direct_fetch_results={},  # semmi nem oldódik fel
    )
    backfill.main()

    set_name.assert_not_called()
    out = capsys.readouterr().out
    assert "34" not in out  # csak sanity, a fő állítás lent
    assert "0/3 fiók nevesítve" in out or "nevesítve" in out


def test_dry_run_sosem_ir_dbbe_meg_sikeres_feloldas_eseten(monkeypatch, fake_accounts, capsys):
    set_name = _run(
        monkeypatch, ["--dry-run"],
        accounts=fake_accounts,
        catalog_meta=[{"id": "act_100", "name": "Fiok Száz"}, {"id": "act_200", "name": "Fiok Ketszaz"}],
        catalog_google=[{"id": "300", "name": "Google Háromszáz"}],
        direct_fetch_results={},
    )
    rc = backfill.main()
    assert rc == 0
    set_name.assert_not_called()
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert "3/3 fiók nevesítve" in out


def test_nincs_hianyzo_fiok_korai_visszateres(monkeypatch, capsys):
    all_named = [{"id": 1, "platform": "meta", "external_account_id": "act_1", "account_name": "X"}]
    set_name = _run(
        monkeypatch, ["--yes"],
        accounts=all_named, catalog_meta=[], catalog_google=[], direct_fetch_results={},
    )
    rc = backfill.main()
    assert rc == 0
    set_name.assert_not_called()
    assert "Nincs tennivaló" in capsys.readouterr().out


def test_yes_flag_atugorja_a_megerositest(monkeypatch, fake_accounts):
    """--yes esetén NEM szabad input()-ot hívni (a script nem interaktív módban is futtatható)."""
    _run(
        monkeypatch, ["--yes"],
        accounts=fake_accounts, catalog_meta=[], catalog_google=[], direct_fetch_results={},
    )
    monkeypatch.setattr("builtins.input", mock.Mock(side_effect=AssertionError("nem kellett volna kérdezni")))
    backfill.main()  # nem dobhat AssertionError-t
