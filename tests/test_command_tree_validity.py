"""
Discord parancsfa build-time validáció — a `CommandSyncFailure` hibaosztály
(HTTP 400, error code 50035) SOHA többé ne juthasson be észrevétlenül.

Éles incidens (2026-07-27): a `/account add` parancs saját `description=`
mezője 107 karakter volt (Discord limitje: 1-100). Ez a `setup_hook`
`tree.sync()` hívását MINDEN induláskor elbuktatta — a bot crash-loopolt,
és emiatt SEMMILYEN korábbi commit sosem lépett életbe éles környezetben,
akármennyire is helyes volt a kódjuk.

A hiba oka: az unit tesztek a parancsokat `.callback()`-en át hívják
(lásd tests/test_account_add_auto_client.py), ami MEGKERÜLI a Discord-féle
validációt — a `.callback()` sosem néz description-hosszt. Ez a teszt EZT
a hézagot zárja be: a VALÓS `commands.Bot` + `CommandTree`-t építi fel
(ugyanazokkal a cogokkal, mint a `setup_hook`), és minden parancsra,
alcsoportra és paraméterre ellenőrzi a Discord által megkövetelt
hosszkorlátokat — MIELŐTT bármi hálózatra menne.

Discord korlátok (slash command payload validáció):
    name / parancsnév:        1-32 karakter
    description:               1-100 karakter
(forrás: Discord Developer Docs — Application Command Object)
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from discord import app_commands
from discord.ext import commands

from src.bot.main import _EXTENSIONS

_NAME_MIN, _NAME_MAX = 1, 32
_DESC_MIN, _DESC_MAX = 1, 100


class _NoOpBot(commands.Bot):
    """Cog-betöltéshez elég `commands.Bot` — sosem csatlakozik Discordhoz."""

    def __init__(self) -> None:
        import discord
        super().__init__(command_prefix="!", intents=discord.Intents.default())


@pytest_asyncio.fixture
async def built_tree():
    """A VALÓS parancsfa, minden cog-gal betöltve — a `setup_hook` 1. lépése,
    hálózat (sync) nélkül. Ugyanaz a `_EXTENSIONS` lista, mint a bot indulásakor."""
    bot = _NoOpBot()
    for ext in _EXTENSIONS:
        await bot.load_extension(ext)
    yield bot.tree
    await bot.close()


def _iter_all_commands(tree: app_commands.CommandTree):
    """Minden parancs és alcsoport (rekurzívan) — globális (nem guild-specifikus)
    fában, mert a cogok ide regisztrálnak; a `copy_global_to(guild=...)` csak
    ebből MÁSOL a guild-fába, a validálandó tartalom ugyanaz."""
    yield from tree.walk_commands()


@pytest.mark.asyncio
async def test_minden_parancs_es_alcsoport_description_1_100_kozott(built_tree):
    violations = []
    for cmd in _iter_all_commands(built_tree):
        desc = cmd.description or ""
        if not (_DESC_MIN <= len(desc) <= _DESC_MAX):
            violations.append(f"{cmd.qualified_name!r}: description len={len(desc)} ({desc!r})")

    assert not violations, (
        "Discord description hosszkorlát (1-100) sértve — ez CommandSyncFailure "
        "crash-loopot okoz éles induláskor:\n" + "\n".join(violations)
    )


@pytest.mark.asyncio
async def test_minden_parameter_description_1_100_kozott(built_tree):
    violations = []
    for cmd in _iter_all_commands(built_tree):
        if not isinstance(cmd, app_commands.Command):
            continue  # Group-oknak nincs saját parameters listája
        for param in cmd.parameters:
            desc = param.description or ""
            if not (_DESC_MIN <= len(desc) <= _DESC_MAX):
                violations.append(
                    f"{cmd.qualified_name!r} paraméter {param.name!r}: "
                    f"description len={len(desc)} ({desc!r})"
                )

    assert not violations, (
        "Discord paraméter-description hosszkorlát (1-100) sértve — ez "
        "CommandSyncFailure crash-loopot okoz éles induláskor:\n" + "\n".join(violations)
    )


@pytest.mark.asyncio
async def test_minden_parancs_es_parameter_nev_1_32_kozott(built_tree):
    """Ugyanez a hibaosztály NÉV-re is fennáll (1-32 karakter) — ritkábban
    érintett (a nevek statikusak és rövidek), de a validáció ugyanolyan olcsó."""
    violations = []
    for cmd in _iter_all_commands(built_tree):
        if not (_NAME_MIN <= len(cmd.name) <= _NAME_MAX):
            violations.append(f"parancs név {cmd.name!r}: len={len(cmd.name)}")
        if isinstance(cmd, app_commands.Command):
            for param in cmd.parameters:
                if not (_NAME_MIN <= len(param.name) <= _NAME_MAX):
                    violations.append(
                        f"{cmd.qualified_name!r} paraméter név {param.name!r}: "
                        f"len={len(param.name)}"
                    )

    assert not violations, "Discord név hosszkorlát (1-32) sértve:\n" + "\n".join(violations)


@pytest.mark.asyncio
async def test_to_dict_payload_epul_hiba_nelkul(built_tree):
    """A VALÓDI szerializáció, amit a `tree.sync()` a HTTP hívás előtt hív
    (`command.to_dict(tree)` — lásd discord/app_commands/tree.py `sync()`).

    Ez nem hálózati hívás, csak a payload felépítése — de pontosan ugyanaz a
    kód-út, mint élesben a `bulk_upsert_guild_commands` elé. Ha ez bármelyik
    top-level parancsra kivételt dobna, a `sync()` is elhasalna induláskor.
    """
    top_level = [
        cmd for cmd in built_tree.walk_commands()
        if cmd.parent is None
    ]
    assert top_level, "a fixture nem talalt top-level parancsot"

    for cmd in top_level:
        payload = cmd.to_dict(built_tree)
        assert payload.get("description"), f"{cmd.name!r}: üres description a payloadban"


@pytest.mark.asyncio
async def test_legalabb_a_var_szamu_parancsot_latjuk(built_tree):
    """Sanity: a fixture valóban betöltötte a cogokat (nem üres fán futunk át
    hamis pozitív 'minden rendben' eredménnyel)."""
    names = {cmd.qualified_name for cmd in _iter_all_commands(built_tree)}
    assert "account add" in names
    assert "account summary-now" in names
    assert "my summary-now" in names
    assert len(names) >= 30
