"""
`describe_synced_commands` — a `setup_hook` deploy-verifikációs log-sorai.

Háttér: egy éles incidensnél (2026-07-27) élő Discord API hívással
bizonyítottuk, hogy a Railway-en futó bot `/account add` parancsa a
`client:`/`platform:`/`account_id:`/`account_name:` REGI mezőkkel volt
regisztrálva, holott a kódban már a `platform:`/`account:` UJ séma volt —
a `/account summary-now` parancs pedig teljesen HIÁNYZOTT a regisztrációból.
Nem duplikált parancs-definíció volt a hiba (a kódban egyetlen `/account add`
létezik), hanem az, hogy a Railway-en futó PROCESS jóval régebbi kódot
futtatott, mint a legutóbbi commit — vagyis a `setup_hook` egyszerűen nem a
várt kóddal futott le.

Ez a függvény pontosan ezt a fajta eltérést teszi láthatóvá a Railway logban
(mezőnkénti bontásban), anélkül hogy élő Discord API hívás kellene a
verifikációhoz — ezt a `Argument`/`AppCommandGroup` alakú fake objektumokkal
teszteljük, mert a valós `discord.app_commands.AppCommand` a gateway-hez
kötött, nem konstruálható egyszerűen tesztben.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from discord import app_commands
from src.bot.main import describe_synced_commands


@dataclass
class _FakeArgument:
    name: str


@dataclass
class _FakeCommand:
    """`app_commands.AppCommand` alakú fake — top-level parancs/csoport."""
    name: str
    options: list = field(default_factory=list)


def _group_like(name: str, options: list):
    """`app_commands.AppCommandGroup`-nak megfelelő objektum (subcommand).

    A tesztelt kód `isinstance(o, app_commands.AppCommandGroup)`-pal dönt —
    ezért a VALÓDI osztályt kell használnunk (nem egy sima dataclass-t),
    de `__init__` nélkül, mert az egy gateway-válasz payload-ot várna.
    """
    obj = app_commands.AppCommandGroup.__new__(app_commands.AppCommandGroup)
    obj.name = name
    obj.options = options
    return obj


def test_alcsoportos_parancs_mezonkent_bontva():
    """/account csoport, add subcommand — pontosan ezt a formát mutatta a
    2026-07-27-i incidens élő API válasza."""
    account_add = _group_like("add", [_FakeArgument("platform"), _FakeArgument("account")])
    account_list = _group_like("list", [_FakeArgument("client"), _FakeArgument("page")])
    account_group = _FakeCommand("account", [account_add, account_list])

    lines = describe_synced_commands([account_group])

    assert "  /account add -> mezők: ['platform', 'account']" in lines
    assert "  /account list -> mezők: ['client', 'page']" in lines


def test_regi_sema_egyertelmuen_megkulonbozhetо_az_ujtol():
    """A pontosan ezt a hibát felfedő eset: a régi séma `client`-et ÉS
    `account_id`-t tartalmaz, az új séma `platform`+`account`-ot."""
    old_schema = _group_like(
        "add", [_FakeArgument("client"), _FakeArgument("platform"),
                _FakeArgument("account_id"), _FakeArgument("account_name")],
    )
    new_schema = _group_like("add", [_FakeArgument("platform"), _FakeArgument("account")])

    old_lines = describe_synced_commands([_FakeCommand("account", [old_schema])])
    new_lines = describe_synced_commands([_FakeCommand("account", [new_schema])])

    assert "client" in old_lines[0]
    assert "client" not in new_lines[0]
    assert old_lines != new_lines


def test_alcsoport_nelkuli_parancs_sima_mezokent_jelenik_meg():
    """Nem minden top-level parancs csoport (pl. egy önálló `/discover`)."""
    simple_cmd = _FakeCommand("discover", [_FakeArgument("client_name")])
    lines = describe_synced_commands([simple_cmd])
    assert lines == ["  /discover -> mezők: ['client_name']"]


def test_ures_szinkron_ures_listat_ad():
    assert describe_synced_commands([]) == []
