"""
Autocomplete teljesség — a DB-szintű szűrésnek (ILIKE) a limit ELŐTT kell futnia,
és a keretnek 25-nek kell lennie (nem 15-nek). Fake Supabase-szel izoláljuk a
`search_account_choices` viselkedését, valós DB nélkül.
"""
from __future__ import annotations

import pytest

from src.storage import account_assignments as aa


class _FakeResp:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """A Supabase query-builder lánc minimál utánzata, memóriában szűrve."""

    def __init__(self, rows):
        self._rows = rows
        self._ilike = None
        self._eq: list[tuple[str, object]] = []
        self._in = None
        self._order = None
        self._limit = None

    def select(self, *_a, **_k):
        return self

    def ilike(self, col, pattern):
        self._ilike = (col, str(pattern).strip("%").lower())
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def in_(self, col, vals):
        self._in = (col, set(vals))
        return self

    def order(self, col):
        self._order = col
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        rows = list(self._rows)
        if self._ilike:
            col, sub = self._ilike
            rows = [r for r in rows if sub in str(r.get(col, "")).lower()]
        for col, val in self._eq:
            rows = [r for r in rows if r.get(col) == val]
        if self._in:
            col, vals = self._in
            rows = [r for r in rows if r.get(col) in vals]
        if self._order:
            rows = sorted(rows, key=lambda r: str(r.get(self._order)))
        if self._limit is not None:
            rows = rows[: self._limit]
        return _FakeResp(rows)


class _FakeSupabase:
    def __init__(self, clients, accounts):
        self._data = {"clients": clients, "ad_accounts": accounts}

    def table(self, name):
        return _FakeQuery(self._data[name])


@pytest.fixture()
def fake_db(monkeypatch):
    # 70 általános kliens + 2 "Stop" nevű, mindegyiknek 1 aktív fiókkal.
    clients = [
        {"id": i, "name": f"Client {i:02d}", "is_active": True} for i in range(1, 71)
    ]
    clients += [
        {"id": 101, "name": "Stopvill", "is_active": True},
        {"id": 102, "name": "Stop Shop", "is_active": True},
    ]
    accounts = [
        {"id": c["id"] * 10, "platform": "meta", "external_account_id": f"act_{c['id']}",
         "client_id": c["id"], "is_active": True}
        for c in clients
    ]
    fake = _FakeSupabase(clients, accounts)
    monkeypatch.setattr(aa, "get_supabase", lambda: fake)
    return fake


def test_empty_query_returns_25_alphabetical(fake_db):
    # Üres input: 72 kliens illeszkedik, de 25-ig kell kitölteni (nem 15-ig),
    # ABC-sorrendben (kliensnév szerint).
    out = aa.search_account_choices("")
    assert len(out) == 25
    names = [r["client_name"] for r in out]
    assert names == sorted(names, key=str.lower)
    assert names[0] == "Client 01"


def test_specific_query_filters_in_db(fake_db):
    # "Stop" keresés: CSAK a két Stop-os kliens fiókja, semmi más.
    out = aa.search_account_choices("Stop")
    names = sorted(r["client_name"] for r in out)
    assert names == ["Stop Shop", "Stopvill"]


def test_match_not_at_db_start_still_found(fake_db):
    # A "Stop" kliensek ABC-sorrendben hátul vannak (Client* után) — a régi
    # limit(15) levágta volna őket. Most is meg kell jelenniük.
    out = aa.search_account_choices("Stopvill")
    assert [r["client_name"] for r in out] == ["Stopvill"]
