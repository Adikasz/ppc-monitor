"""
Nagy fiókok kampánylistája NEM veszhet el (Bug: "Meta kampányok nem látszanak mind").

Valós eset: Brands_Marquard Media — 895 kampány a Meta API-ban, 895 a DB-ben,
de a `/campaign list` egyetlen Discord embed description-be írta őket 3900
karakteres csonkítással → 895-ből csak ~35 sor jelent meg. A Meta API oldali
lapozás (facebook_business Cursor) végig helyes volt; a veszteség a
MEGJELENÍTÉSBEN keletkezett.

Két réteget fedünk le:
  1. `paginate_lines` — egyetlen sor sem veszhet el, és egy oldal sem lépheti
     túl a Discord 4096-os description limitjét.
  2. `campaigns_storage.get_campaigns_by_ad_account` / `list_campaigns` — a
     PostgREST 1000-es sorlimitje fölött is minden sort visszaad (range-lapozás).
"""
from __future__ import annotations

import pytest

from src.utils.paginator import paginate_lines, _MAX_CHARS_PER_PAGE


# ---------------------------------------------------------------------------
# 1) Megjelenítés — a régi csonkítás vs. az új lapozás
# ---------------------------------------------------------------------------

def _campaign_lines(n: int) -> list[str]:
    """A `/campaign list` sorformátumát utánzó, realisztikus hosszúságú sorok."""
    return [
        f"🟢 **#{1000 + i}** — Marquard | Prospecting | Q{i % 4 + 1} | Broad Audience {i}\n"
        f"　`mature` · 🖥 `meta` · típus: `conversion`"
        for i in range(n)
    ]


def test_regi_csonkitas_reprodukalja_a_hibat():
    """A javítás ELŐTTI logika 895 kampányból csak egy töredéket mutatott."""
    lines = _campaign_lines(895)

    description = ""
    shown = 0
    for line in lines:
        if len(description) + len(line) + 1 > 3900:
            break
        description += line + "\n"
        shown += 1

    # Ez a hiba lényege: a sorok 95%-a néma módon eltűnt.
    assert shown < 60, f"a régi logika {shown} sort mutatott"
    assert len(lines) - shown > 800


def test_lapozas_egyetlen_kampanyt_sem_veszit_el():
    """A javítás UTÁN mind a 895 sor előjön — csak több oldalon."""
    lines = _campaign_lines(895)
    pages = paginate_lines(lines)

    rendered = "\n".join(pages)
    for line in lines:
        assert line in rendered

    # Soronkénti egyezés is: nem csak részstringként van meg, hanem hiánytalanul.
    total_lines = sum(page.count("**#") for page in pages)
    assert total_lines == 895
    assert len(pages) > 1


@pytest.mark.parametrize("n", [1, 19, 20, 21, 100, 341, 895])
def test_egy_oldal_sem_lepi_tul_a_discord_limitet(n):
    """Minden oldalnak bele kell férnie a 4096-os embed description limitbe."""
    pages = paginate_lines(_campaign_lines(n))
    assert pages, "üres oldallista"
    for page in pages:
        assert len(page) <= _MAX_CHARS_PER_PAGE
        assert len(page) < 4096


def test_ures_lista_nem_dob_hibat():
    assert paginate_lines([]) == []


def test_tulhosszu_sor_sajat_oldalra_kerul():
    """Egy önmagában limit fölötti sor sem tüntethet el más sorokat."""
    lines = ["rövid egy", "X" * (_MAX_CHARS_PER_PAGE + 500), "rövid kettő"]
    pages = paginate_lines(lines)
    assert len(pages) == 3
    assert pages[0] == "rövid egy"
    assert len(pages[1]) == _MAX_CHARS_PER_PAGE
    assert pages[2] == "rövid kettő"


# ---------------------------------------------------------------------------
# 2) Tároló réteg — PostgREST 1000-es sorlimit fölötti lapozás
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Supabase query-builder minimál utánzata, PostgREST 1000-es plafonnal."""

    HARD_CAP = 1000

    def __init__(self, rows):
        self._rows = rows
        self._eq: list[tuple[str, object]] = []
        self._neq: list[tuple[str, object]] = []
        self._in: tuple[str, set] | None = None
        self._range: tuple[int, int] | None = None

    def select(self, *_a, **_k):
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

    def order(self, *_a, **_k):
        return self

    def limit(self, n):
        self._range = (0, n - 1)
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        rows = list(self._rows)
        for col, val in self._eq:
            rows = [r for r in rows if r.get(col) == val]
        for col, val in self._neq:
            rows = [r for r in rows if r.get(col) != val]
        if self._in:
            col, vals = self._in
            rows = [r for r in rows if r.get(col) in vals]
        if self._range:
            start, end = self._range
            # A PostgREST sosem ad vissza 1000-nél több sort egy kérésre.
            end = min(end, start + self.HARD_CAP - 1)
            rows = rows[start:end + 1]
        else:
            rows = rows[: self.HARD_CAP]
        return _FakeResp(rows)


class _FakeSupabase:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _FakeQuery(self._tables.get(name, []))


@pytest.fixture
def big_account(monkeypatch):
    """Egy 2500 kampányos fiók — jóval a PostgREST 1000-es limitje fölött."""
    campaigns = [
        {
            "id": i,
            "ad_account_id": 42,
            "external_campaign_id": str(900000 + i),
            "name": f"Kampany {i:05d}",
            "lifecycle_state": "mature",
            "is_monitored": True,
        }
        for i in range(2500)
    ]
    fake = _FakeSupabase({
        "campaigns": campaigns,
        "ad_accounts": [{"id": 42, "client_id": 7}],
    })
    from src.storage import campaigns as cs
    monkeypatch.setattr(cs, "get_supabase", lambda: fake)
    return cs


def test_get_campaigns_by_ad_account_lapoz_1000_folott(big_account):
    rows = big_account.get_campaigns_by_ad_account(42)
    assert len(rows) == 2500, f"csak {len(rows)} kampány jött vissza a 2500-ból"
    assert len({r["id"] for r in rows}) == 2500, "duplikált sorok a lapozásban"


def test_list_campaigns_lapoz_1000_folott(big_account):
    rows = big_account.list_campaigns(7, active_only=True)
    assert len(rows) == 2500, f"csak {len(rows)} kampány jött vissza a 2500-ból"
    assert len({r["id"] for r in rows}) == 2500, "duplikált sorok a lapozásban"
