"""
Handover-return storage logika — pontos keresés + fallback + graceful degrade.

Fake Supabase-szel, valós DB nélkül:
  - get_handover_assignments None-t ad, ha a 0012 handover_* oszlop hiányzik.
  - get_shared_account_assignments a KÖZÖS fiókok metszetét adja (fallback).
  - mark_handover False-t ad (nem dob), ha az oszlop hiányzik.
"""
from __future__ import annotations

from src.storage import account_assignments as aa


class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    """Query-builder lánc memóriában; opcionálisan hibát dob (hiányzó oszlop)."""

    def __init__(self, rows, raise_msg=None):
        self._rows = rows
        self._raise = raise_msg
        self._eq: list[tuple] = []
        self._in = None

    def select(self, *_a):
        return self

    def update(self, _fields):
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def in_(self, col, vals):
        self._in = (col, set(vals))
        return self

    def execute(self):
        if self._raise:
            raise Exception(self._raise)
        rows = list(self._rows)
        for col, val in self._eq:
            rows = [r for r in rows if r.get(col) == val]
        if self._in:
            col, vals = self._in
            rows = [r for r in rows if r.get(col) in vals]
        return _Resp(rows)


class _SB:
    def __init__(self, rows, raise_msg=None):
        self._rows = rows
        self._raise = raise_msg

    def table(self, _name):
        return _Query(self._rows, self._raise)


def _rows():
    # user 10 (fogadó): fiók 1,2,3 ; user 20 (eredeti): fiók 2,3,4
    # a 2,3 KÖZÖS ; a handover_from_user_id a 10-es fogadó 2,3 során = 20.
    def acc(aid, name):
        return {"id": aid, "platform": "meta", "external_account_id": f"act_{aid}",
                "client_id": aid, "clients": {"name": name}}
    return [
        {"ad_account_id": 1, "user_id": 10, "handover_from_user_id": None, "ad_accounts": acc(1, "A")},
        {"ad_account_id": 2, "user_id": 10, "handover_from_user_id": 20, "ad_accounts": acc(2, "B")},
        {"ad_account_id": 3, "user_id": 10, "handover_from_user_id": 20, "ad_accounts": acc(3, "C")},
        {"ad_account_id": 2, "user_id": 20, "handover_from_user_id": None, "ad_accounts": acc(2, "B")},
        {"ad_account_id": 3, "user_id": 20, "handover_from_user_id": None, "ad_accounts": acc(3, "C")},
        {"ad_account_id": 4, "user_id": 20, "handover_from_user_id": None, "ad_accounts": acc(4, "D")},
    ]


def test_precise_returns_only_handover_rows(monkeypatch):
    monkeypatch.setattr(aa, "get_supabase", lambda: _SB(_rows()))
    out = aa.get_handover_assignments(10, 20)
    ids = sorted(r["ad_account_id"] for r in out)
    assert ids == [2, 3]  # csak amit 20-tól kapott handover-rel, az 1-es nem


def test_precise_returns_none_when_column_missing(monkeypatch):
    msg = 'column account_assignments.handover_from_user_id does not exist'
    monkeypatch.setattr(aa, "get_supabase", lambda: _SB(_rows(), raise_msg=msg))
    assert aa.get_handover_assignments(10, 20) is None


def test_shared_fallback_intersection(monkeypatch):
    monkeypatch.setattr(aa, "get_supabase", lambda: _SB(_rows()))
    out = aa.get_shared_account_assignments(10, 20)
    ids = sorted(r["ad_account_id"] for r in out)
    assert ids == [2, 3]  # a 10-es sorai a 20-szal KÖZÖS fiókokra (2,3); az 1 nem közös


def test_mark_handover_graceful_when_column_missing(monkeypatch):
    msg = "Could not find the 'handover_until' column of 'account_assignments' in the schema cache"
    monkeypatch.setattr(aa, "get_supabase", lambda: _SB(_rows(), raise_msg=msg))
    assert aa.mark_handover(2, 10, from_user_id=20, until=None) is False


def test_mark_handover_ok(monkeypatch):
    monkeypatch.setattr(aa, "get_supabase", lambda: _SB(_rows()))
    assert aa.mark_handover(2, 10, from_user_id=20, until="2026-07-09T00:00:00+00:00") is True
