"""
/alert test — opcionális `client:` mező, ami a `campaign:` autocomplete-et az
adott ügyfél kampányaira szűkíti (namespace-alapú szűrés, ugyanaz a minta, mint
a `/my mute` account → campaign és a `/account add` platform → account mezőinél).

Kliens nélkül a keresés globális marad (backward compat), és a `/alert mute` /
`/alert unmute` viselkedése változatlan.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from src.bot.commands import alerts as alerts_cmd
from src.storage import campaigns as campaigns_storage


class _FakeBot:
    pass


_STOPVILL = {"id": 7, "name": "Stopvill"}

_GLOBAL_ROWS = [
    {"id": 10, "name": "Sales", "ad_accounts": {"clients": {"name": "Stopvill"}}},
    {"id": 11, "name": "Sales", "ad_accounts": {"clients": {"name": "LEGRAND"}}},
]
_STOPVILL_ROWS = [_GLOBAL_ROWS[0]]


def _cog():
    return alerts_cmd.AlertsCog(_FakeBot())


def _interaction(**namespace):
    return SimpleNamespace(namespace=SimpleNamespace(**namespace))


# ---------------------------------------------------------------------------
# campaign: autocomplete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_nelkul_globalis_marad():
    cog = _cog()
    with mock.patch.object(
        alerts_cmd.campaigns_storage,
        "search_campaign_choices_global",
        return_value=_GLOBAL_ROWS,
    ) as search:
        choices = await cog.test_campaign_autocomplete(_interaction(client=None), "Sales")

    assert search.call_args.kwargs["client_id"] is None
    assert [c.value for c in choices] == ["10", "11"]
    assert choices[0].name == "Stopvill / Sales"
    assert choices[1].name == "LEGRAND / Sales"


@pytest.mark.asyncio
async def test_client_megadva_csak_az_o_kampanyai():
    cog = _cog()
    with mock.patch.object(
        alerts_cmd.clients_storage, "get_client", return_value=_STOPVILL,
    ), mock.patch.object(
        alerts_cmd.campaigns_storage,
        "search_campaign_choices_global",
        return_value=_STOPVILL_ROWS,
    ) as search:
        # az ügyfél-autocomplete a #id-t adja át értékként
        choices = await cog.test_campaign_autocomplete(_interaction(client="7"), "Sales")

    assert search.call_args.kwargs["client_id"] == 7
    assert [c.value for c in choices] == ["10"]


@pytest.mark.asyncio
async def test_client_nevvel_is_szukit():
    """Kézzel begépelt (nem autocomplete-ből választott) ügyfélnév is szűkít."""
    cog = _cog()
    with mock.patch.object(
        alerts_cmd.clients_storage, "find_client_by_name_ci", return_value=_STOPVILL,
    ) as by_name, mock.patch.object(
        alerts_cmd.campaigns_storage,
        "search_campaign_choices_global",
        return_value=_STOPVILL_ROWS,
    ) as search:
        choices = await cog.test_campaign_autocomplete(
            _interaction(client="stopvill"), "Sales"
        )

    by_name.assert_called_once_with("stopvill")
    assert search.call_args.kwargs["client_id"] == 7
    assert [c.value for c in choices] == ["10"]


@pytest.mark.asyncio
async def test_ismeretlen_client_ures_listat_ad():
    """Félig begépelt ügyfélnévnél inkább üres lista, mint a globális találatok."""
    cog = _cog()
    with mock.patch.object(
        alerts_cmd.clients_storage, "find_client_by_name_ci", return_value=None,
    ), mock.patch.object(
        alerts_cmd.campaigns_storage, "search_campaign_choices_global",
    ) as search:
        choices = await cog.test_campaign_autocomplete(_interaction(client="Stop"), "Sales")

    assert choices == []
    search.assert_not_called()


@pytest.mark.asyncio
async def test_mute_autocomplete_globalis_marad():
    """A /alert mute-nak nincs client mezője — a namespace-e sem tartalmazza."""
    cog = _cog()
    with mock.patch.object(
        alerts_cmd.campaigns_storage,
        "search_campaign_choices_global",
        return_value=_GLOBAL_ROWS,
    ) as search:
        choices = await cog.mute_campaign_autocomplete(_interaction(), "Sales")

    assert search.call_args.kwargs["client_id"] is None
    assert [c.value for c in choices] == ["10", "11"]


@pytest.mark.asyncio
async def test_rovid_query_nem_kerdez_le():
    cog = _cog()
    with mock.patch.object(
        alerts_cmd.campaigns_storage, "search_campaign_choices_global",
    ) as search:
        choices = await cog.test_campaign_autocomplete(_interaction(client="7"), "S")

    assert choices == []
    search.assert_not_called()


# ---------------------------------------------------------------------------
# client: autocomplete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_autocomplete_id_erteket_ad():
    cog = _cog()
    with mock.patch.object(
        alerts_cmd.clients_storage,
        "search_clients",
        return_value=[_STOPVILL, {"id": 8, "name": "Stopshop"}],
    ) as search:
        choices = await cog.test_client_autocomplete(_interaction(), "Stop")

    search.assert_called_once_with("Stop", active=None)
    assert [(c.name, c.value) for c in choices] == [("Stopvill", "7"), ("Stopshop", "8")]


# ---------------------------------------------------------------------------
# kampány-feloldás (a parancs futásakor)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_client_szukitessel_hivja_a_storaget():
    cog = _cog()
    interaction = SimpleNamespace(followup=SimpleNamespace(send=mock.AsyncMock()))
    row = {"id": 10, "name": "Sales", "lifecycle_state": "mature", "ad_account_id": 3}
    with mock.patch.object(
        alerts_cmd.campaigns_storage, "resolve_campaign", return_value=row,
    ) as resolve:
        out = await cog._resolve_campaign_or_reject(
            interaction, "Sales", client_row=_STOPVILL,
        )

    assert out == row
    assert resolve.call_args.kwargs["client_id"] == 7


@pytest.mark.asyncio
async def test_resolve_client_nelkul_nem_szukit():
    cog = _cog()
    interaction = SimpleNamespace(followup=SimpleNamespace(send=mock.AsyncMock()))
    row = {"id": 10, "name": "Sales", "lifecycle_state": "mature", "ad_account_id": 3}
    with mock.patch.object(
        alerts_cmd.campaigns_storage, "resolve_campaign", return_value=row,
    ) as resolve:
        out = await cog._resolve_campaign_or_reject(interaction, "Sales")

    assert out == row
    assert resolve.call_args.kwargs["client_id"] is None


@pytest.mark.asyncio
async def test_nem_talalt_kampany_uzenete_megnevezi_az_ugyfelet():
    cog = _cog()
    send = mock.AsyncMock()
    interaction = SimpleNamespace(followup=SimpleNamespace(send=send))
    with mock.patch.object(
        alerts_cmd.campaigns_storage, "resolve_campaign", return_value=None,
    ):
        out = await cog._resolve_campaign_or_reject(
            interaction, "Nincs ilyen", client_row=_STOPVILL,
        )

    assert out is None
    assert "Stopvill" in send.call_args[0][0]


# ---------------------------------------------------------------------------
# storage: search_campaign_choices_global(client_id=…)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """A Supabase query-builder lánc minimál utánzata, memóriában szűrve."""

    def __init__(self, rows):
        self._rows = rows
        self._ilike = None
        self._eq: list[tuple[str, object]] = []
        self._neq: list[tuple[str, object]] = []
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

    def neq(self, col, val):
        self._neq.append((col, val))
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
        for col, val in self._neq:
            rows = [r for r in rows if r.get(col) != val]
        if self._in:
            col, vals = self._in
            rows = [r for r in rows if r.get(col) in vals]
        if self._order:
            rows = sorted(rows, key=lambda r: str(r.get(self._order)))
        if self._limit is not None:
            rows = rows[: self._limit]
        return _FakeResp(rows)


class _FakeSupabase:
    def __init__(self, data):
        self._data = data

    def table(self, name):
        return _FakeQuery(self._data[name])


@pytest.fixture()
def fake_db(monkeypatch):
    accounts = [
        {"id": 70, "client_id": 7},    # Stopvill
        {"id": 80, "client_id": 8},    # LEGRAND
    ]
    campaigns = [
        {"id": 10, "name": "Sales", "ad_account_id": 70, "lifecycle_state": "mature"},
        {"id": 11, "name": "Sales", "ad_account_id": 80, "lifecycle_state": "mature"},
        {"id": 12, "name": "Traffic", "ad_account_id": 80, "lifecycle_state": "mature"},
        {"id": 13, "name": "Sales-régi", "ad_account_id": 70, "lifecycle_state": "ended"},
    ]
    fake = _FakeSupabase({"ad_accounts": accounts, "campaigns": campaigns})
    monkeypatch.setattr(campaigns_storage, "get_supabase", lambda: fake)
    return fake


def test_storage_client_id_nelkul_minden_ugyfel(fake_db):
    rows = campaigns_storage.search_campaign_choices_global("Sales")
    assert [r["id"] for r in rows] == [10, 11]  # az 'ended' (13) kimarad


def test_storage_client_id_szukit(fake_db):
    rows = campaigns_storage.search_campaign_choices_global("Sales", client_id=7)
    assert [r["id"] for r in rows] == [10]


def test_storage_ismeretlen_client_ures(fake_db):
    assert campaigns_storage.search_campaign_choices_global("Sales", client_id=999) == []


def test_storage_id_kereses_is_szukit(fake_db):
    """Numerikus bevitel: a másik ügyfél kampány-ID-ja nem szivárog át."""
    assert campaigns_storage.search_campaign_choices_global("11", client_id=7) == []
    rows = campaigns_storage.search_campaign_choices_global("10", client_id=7)
    assert [r["id"] for r in rows] == [10]


def test_storage_resolve_campaign_client_szukitessel(fake_db):
    # A "Sales" a 7-es ügyfélnél is többértelmű (aktív 10 + lezárt 13), de a
    # MÁSIK ügyfél azonos nevű kampánya (11) már nem kerül a találatok közé.
    res = campaigns_storage.resolve_campaign("Sales", client_id=7)
    assert [m["id"] for m in res["matches"]] == [10, 13]
    assert campaigns_storage.resolve_campaign("Sales-régi", client_id=7)["id"] == 13
    # másik ügyfél kampány-ID-ja nem oldható fel a szűkítés alatt
    assert campaigns_storage.resolve_campaign("11", client_id=7) is None
    # kliens nélkül minden marad a régiben (globális, mindhárom "Sales")
    assert campaigns_storage.resolve_campaign("Sales")["ambiguous"] is True
