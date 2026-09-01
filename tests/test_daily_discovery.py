"""
Automatikus napi kampány-discovery — a job, a fault isolation és a last_seen_at.

MIÉRT LÉTEZIK EZ A TESZTFÁJL:
    A discovery (új kampányok felismerése a Meta/Google API-ból) hónapokig CSAK
    kézi `/discover` futtatáskor történt meg: a scheduler egyetlen jobja sem
    hívta. Élesben ez úgy látszott, hogy a `campaigns.last_seen_at` az EGÉSZ
    adatbázisban három dátumot tartalmazott — a három kézi futtatás napját —,
    és a legfrissebb is hetekkel korábbi volt. Vagyis minden ügyfélnél lehettek
    kampányok, amikről a rendszer nem tudott, és lezárt kampányok, amiket még
    mindig aktívként figyelt.

    A tesztek ezt a hézagot zárják be, négy irányból:

    1. A JOB TÉNYLEG VÉGIGMEGY MINDEN AKTÍV ÜGYFÉLEN (és így minden aktív
       fiókon) — nem csak az elsőn, nem csak azokon, amiknek már van kampánya.
    2. FAULT ISOLATION KÉT SZINTEN: egy ügyfél elszállása nem viszi magával a
       többit, és egy fiók API-hibája nem viszi magával az ügyfél többi fiókját
       (ez utóbbi az 5 ismert, jogosultsági hibás ügynökségi Meta fiók miatt
       nem elmélet).
    3. A `last_seen_at` VALÓBAN FRISSÜL — ez az a mező, aminek az állása
       egyáltalán felfedte a hibát.
    4. A JOB BE VAN KÖTVE a schedulerbe, és NEM ütközik az óránkénti ciklussal.
       Enélkül minden fenti teszt zöld lehetne úgy, hogy élesben soha semmi nem
       fut le — pontosan ez volt az eredeti hiba.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from src.bot.commands import discovery as discovery_cmd
from src.monitoring import discovery as discovery_mod
from src.monitoring import scheduler as sched


# ---------------------------------------------------------------------------
# Segédek
# ---------------------------------------------------------------------------

def _result(**kw):
    """Egy `discover_campaigns_for_client` eredmény, teljes kulcskészlettel."""
    base = {
        "accounts": 1, "accounts_failed": 0,
        "inserted": 0, "updated": 0, "deactivated": 0, "errors": [],
    }
    base.update(kw)
    return base


def _clients(n: int):
    return [{"id": i, "name": f"Ugyfel{i}"} for i in range(1, n + 1)]


def _patch_job(stack, *, clients, per_client):
    """A job külső függéseit mockolja; visszaadja a hívás-naplót.

    `per_client`: client_id → eredmény dict VAGY Exception (amit a discovery dob).
    """
    hivott: list[int] = []

    def _discover(client_id):
        hivott.append(client_id)
        outcome = per_client.get(client_id, _result())
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    stack.enter_context(mock.patch.object(
        sched.clients_storage, "list_clients", return_value=clients))
    stack.enter_context(mock.patch.object(
        sched.clients_storage, "get_client",
        side_effect=lambda cid: next((c for c in clients if c["id"] == cid), None)))
    stack.enter_context(mock.patch.object(
        sched, "discover_campaigns_for_client", side_effect=_discover))
    return hivott


# ---------------------------------------------------------------------------
# 1) A job minden aktív ügyfélre (és így minden aktív fiókra) lefut
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_job_runs_discovery_for_every_active_client():
    """Az összes aktív ügyfél sorra kerül — nem áll meg az elsőnél."""
    clients = _clients(5)
    with contextlib.ExitStack() as stack:
        hivott = _patch_job(stack, clients=clients, per_client={})
        stats = await sched.daily_discovery_job()

    assert hivott == [1, 2, 3, 4, 5]
    assert stats["clients"] == 5


@pytest.mark.asyncio
async def test_the_job_aggregates_account_and_campaign_counters():
    """Az összesítő a FIÓK-szintű számokat is viszi — ebből derül ki a néma kimaradás."""
    clients = _clients(3)
    per_client = {
        1: _result(accounts=2, inserted=4, updated=10),
        2: _result(accounts=3, accounts_failed=1, updated=5,
                   errors=[{"account": "act_1", "error": "permission"}]),
        3: _result(accounts=1, deactivated=2, updated=7),
    }
    with contextlib.ExitStack() as stack:
        _patch_job(stack, clients=clients, per_client=per_client)
        stats = await sched.daily_discovery_job()

    assert stats["accounts"] == 6
    assert stats["accounts_failed"] == 1
    assert stats["inserted"] == 4
    assert stats["updated"] == 22
    assert stats["deactivated"] == 2
    assert stats["errors"] == 1
    assert stats["failed_clients"] == 0


@pytest.mark.asyncio
async def test_the_job_can_be_scoped_to_given_clients():
    """A `/discover google` szűkítése ugyanezt a jobot hívja, nem egy másolatot."""
    clients = _clients(4)
    with contextlib.ExitStack() as stack:
        hivott = _patch_job(stack, clients=clients, per_client={})
        stats = await sched.daily_discovery_job(client_ids=[2, 4])

    assert hivott == [2, 4]
    assert stats["clients"] == 2


@pytest.mark.asyncio
async def test_the_job_returns_full_stats_even_with_no_clients():
    """Korai kilépésnél is teljes kulcskészlet — a hívónak ne kelljen `.get()`-elnie."""
    with contextlib.ExitStack() as stack:
        _patch_job(stack, clients=[], per_client={})
        stats = await sched.daily_discovery_job()

    for key in ("clients", "accounts", "accounts_failed", "inserted",
                "updated", "deactivated", "errors", "failed_clients", "per_client"):
        assert key in stats, key
    assert stats["clients"] == 0


@pytest.mark.asyncio
async def test_a_failing_client_list_does_not_raise():
    """A DB elérhetetlensége sem dobhat: a job némán, teljes statsszal kilép."""
    with mock.patch.object(
        sched.clients_storage, "list_clients", side_effect=RuntimeError("DB le")
    ):
        stats = await sched.daily_discovery_job()

    assert stats["clients"] == 0
    assert stats["inserted"] == 0


# ---------------------------------------------------------------------------
# 2) Fault isolation — ügyfél-szinten és fiók-szinten
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_failing_client_does_not_stop_the_others():
    """Egy ügyfél elszállása után a job MEGY TOVÁBB a többire."""
    clients = _clients(4)
    per_client = {
        2: RuntimeError("Meta token lejárt"),
        1: _result(inserted=1),
        3: _result(inserted=2),
        4: _result(inserted=3),
    }
    with contextlib.ExitStack() as stack:
        hivott = _patch_job(stack, clients=clients, per_client=per_client)
        stats = await sched.daily_discovery_job()

    assert hivott == [1, 2, 3, 4], "a hibás ügyfél után is folytatódott"
    assert stats["failed_clients"] == 1
    assert stats["inserted"] == 6, "a többi ügyfél eredménye megmaradt"

    hibas = [e for e in stats["per_client"] if e.get("error")]
    assert len(hibas) == 1
    assert "Meta token lejárt" in hibas[0]["error"]


def test_one_failing_account_does_not_stop_the_clients_other_accounts():
    """Egy fiók API-hibája után az ügyfél TÖBBI fiókja feldolgozódik.

    Ez a VALÓDI `discover_campaigns_for_client`-en fut (nem mockolt), mert pont
    az itteni per-fiók `continue` az, ami az 5 ismert jogosultsági hibás
    ügynökségi Meta fióknál számít.
    """
    accounts = [
        {"id": 10, "platform": "meta", "external_account_id": "act_ok1"},
        {"id": 11, "platform": "meta", "external_account_id": "act_bad"},
        {"id": 12, "platform": "meta", "external_account_id": "act_ok2"},
    ]

    def _fetch(platform, client, ext_account_id):
        if ext_account_id == "act_bad":
            raise RuntimeError("(#200) Permissions error")
        return [{"ext_campaign_id": f"c-{ext_account_id}", "name": "K", "status": "ACTIVE"}]

    feldolgozott: list[str] = []

    def _upsert(**kw):
        feldolgozott.append(kw["ext_campaign_id"])
        kw["result"]["inserted"] += 1

    with mock.patch.object(
        discovery_mod.ad_accounts_storage, "get_ad_accounts_for_client",
        return_value=accounts,
    ), mock.patch.object(
        discovery_mod, "_make_platform_client", return_value=object(),
    ), mock.patch.object(
        discovery_mod, "_fetch_campaigns", side_effect=_fetch,
    ), mock.patch.object(
        discovery_mod, "_upsert_campaign", side_effect=_upsert,
    ), mock.patch.object(
        discovery_mod, "_deactivate_stale",
    ), mock.patch.object(
        discovery_mod, "_has_monitored_campaigns", return_value=False,
    ):
        result = discovery_mod.discover_campaigns_for_client(1)

    assert feldolgozott == ["c-act_ok1", "c-act_ok2"], "a hibás fiók után is folytatódott"
    assert result["accounts"] == 3
    assert result["accounts_failed"] == 1
    assert result["inserted"] == 2
    assert len(result["errors"]) == 1
    assert result["errors"][0]["account"] == "act_bad"


def test_a_platform_init_failure_only_affects_that_platform():
    """Hiányzó Google SDK/token esetén a Meta fiókok discoveryje fut tovább."""
    accounts = [
        {"id": 10, "platform": "google", "external_account_id": "123"},
        {"id": 11, "platform": "meta", "external_account_id": "act_ok"},
        {"id": 12, "platform": "google", "external_account_id": "456"},
    ]

    def _make(platform):
        if platform == "google":
            raise RuntimeError("Google Ads SDK nincs telepítve")
        return object()

    with mock.patch.object(
        discovery_mod.ad_accounts_storage, "get_ad_accounts_for_client",
        return_value=accounts,
    ), mock.patch.object(
        discovery_mod, "_make_platform_client", side_effect=_make,
    ), mock.patch.object(
        discovery_mod, "_fetch_campaigns", return_value=[],
    ), mock.patch.object(
        discovery_mod, "_deactivate_stale",
    ), mock.patch.object(
        discovery_mod, "_has_monitored_campaigns", return_value=False,
    ):
        result = discovery_mod.discover_campaigns_for_client(1)

    assert result["accounts"] == 3
    assert result["accounts_failed"] == 2, "csak a két Google fiók esett ki"
    # A platform init-hiba platformonként EGYSZER fut le, de mindkét érintett
    # fiók megkapja a hibáját — különben némán tűnnének el.
    assert len(result["errors"]) == 2


# ---------------------------------------------------------------------------
# 3) last_seen_at — a mező, aminek az állása felfedte a hibát
# ---------------------------------------------------------------------------

def test_an_existing_campaign_gets_a_fresh_last_seen_at():
    """Meglévő kampánynál a discovery FRISS `last_seen_at`-et ír."""
    frissitve: dict = {}

    class _Q:
        def select(self, *_a): return self
        def eq(self, *_a): return self
        def limit(self, *_a): return self
        def execute(self): return SimpleNamespace(data=[{"id": 77, "platform_status": "ACTIVE"}])

    now = datetime(2026, 9, 1, 3, 30, tzinfo=timezone.utc)

    with mock.patch.object(discovery_mod, "get_supabase",
                           return_value=SimpleNamespace(table=lambda _n: _Q())), \
         mock.patch.object(
             discovery_mod.campaigns_storage, "update_campaign_status",
             side_effect=lambda **kw: frissitve.update(kw)):
        result = _result()
        discovery_mod._upsert_campaign(
            client_id=1, db_account_id=10, ext_campaign_id="c1",
            api_campaign={"name": "K", "status": "PAUSED"}, now=now, result=result,
        )

    assert frissitve["campaign_id"] == 77
    assert frissitve["last_seen_at"] == now, "a last_seen_at a MOSTANI időre frissül"
    assert frissitve["status"] == "PAUSED"
    assert result["updated"] == 1


def test_a_new_campaign_is_inserted_and_counted():
    """Ismeretlen kampány → INSERT, 'new' lifecycle-lal, monitorozottan."""
    letrehozott: dict = {}

    class _Q:
        def select(self, *_a): return self
        def eq(self, *_a): return self
        def limit(self, *_a): return self
        def execute(self): return SimpleNamespace(data=[])

    with mock.patch.object(discovery_mod, "get_supabase",
                           return_value=SimpleNamespace(table=lambda _n: _Q())), \
         mock.patch.object(
             discovery_mod.campaigns_storage, "create_campaign",
             side_effect=lambda **kw: letrehozott.update(kw) or {"id": 99}), \
         mock.patch.object(
             discovery_mod.account_assignments_storage,
             "inherit_account_assignments_for_campaign", return_value=0), \
         mock.patch.object(
             discovery_mod.assignments_storage,
             "inherit_client_assignments_for_campaign", return_value=0):
        result = _result()
        discovery_mod._upsert_campaign(
            client_id=1, db_account_id=10, ext_campaign_id="uj-1",
            api_campaign={"name": "Új kampány", "status": "ACTIVE"},
            now=datetime.now(timezone.utc), result=result,
        )

    assert letrehozott["external_campaign_id"] == "uj-1"
    assert letrehozott["lifecycle_state"] == "new"
    assert letrehozott["is_monitored"] is True
    assert result["inserted"] == 1


def test_stale_campaigns_are_deactivated_only_when_not_seen():
    """Amit az API most visszaadott, az MARAD; a 24h+ nem látott leáll."""
    regi = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows = [
        {"id": 1, "external_campaign_id": "latott", "last_seen_at": regi},
        {"id": 2, "external_campaign_id": "eltunt", "last_seen_at": regi},
        {"id": 3, "external_campaign_id": "friss",
         "last_seen_at": datetime.now(timezone.utc).isoformat()},
    ]
    torolt: list[int] = []

    class _Q:
        def select(self, *_a): return self
        def eq(self, *_a): return self
        def order(self, *_a): return self
        def range(self, start, _end): return SimpleNamespace(
            execute=lambda: SimpleNamespace(data=rows if start == 0 else []))

    with mock.patch.object(discovery_mod, "get_supabase",
                           return_value=SimpleNamespace(table=lambda _n: _Q())), \
         mock.patch.object(discovery_mod.campaigns_storage, "soft_delete_campaign",
                           side_effect=torolt.append):
        result = _result()
        discovery_mod._deactivate_stale(
            db_account_id=10,
            stale_cutoff=datetime.now(timezone.utc) - timedelta(hours=24),
            api_campaign_ids={"latott"},
            result=result,
        )

    assert torolt == [2], "csak a nem látott ÉS régi kampány áll le"
    assert result["deactivated"] == 1


def test_an_empty_api_response_does_not_wipe_an_accounts_monitoring():
    """ÜRES API-válasz nem állíthatja le egy fiók ÖSSZES kampányát.

    A napi automatikus futás miatt ez valós kockázat: egyetlen csendes (hibát
    nem dobó, de üres) válaszból az egész ügyfél kiesne a monitoringból. Egy
    nappal tovább figyelt lezárt kampány olcsóbb hiba, mint egy némán vakká
    tett fiók — ezért ilyenkor kihagyjuk a leállítást.
    """
    accounts = [{"id": 10, "platform": "meta", "external_account_id": "act_ures"}]

    with mock.patch.object(
        discovery_mod.ad_accounts_storage, "get_ad_accounts_for_client",
        return_value=accounts,
    ), mock.patch.object(
        discovery_mod, "_make_platform_client", return_value=object(),
    ), mock.patch.object(
        discovery_mod, "_fetch_campaigns", return_value=[],
    ), mock.patch.object(
        discovery_mod, "_has_monitored_campaigns", return_value=True,
    ), mock.patch.object(
        discovery_mod, "_deactivate_stale",
    ) as deactivate:
        result = discovery_mod.discover_campaigns_for_client(1)

    deactivate.assert_not_called()
    assert result["deactivated"] == 0


def test_a_legitimately_empty_account_still_runs_the_stale_check():
    """Ha a fióknak amúgy sincs monitorozott kampánya, a védelem nem áll az útba."""
    accounts = [{"id": 10, "platform": "meta", "external_account_id": "act_ures"}]

    with mock.patch.object(
        discovery_mod.ad_accounts_storage, "get_ad_accounts_for_client",
        return_value=accounts,
    ), mock.patch.object(
        discovery_mod, "_make_platform_client", return_value=object(),
    ), mock.patch.object(
        discovery_mod, "_fetch_campaigns", return_value=[],
    ), mock.patch.object(
        discovery_mod, "_has_monitored_campaigns", return_value=False,
    ), mock.patch.object(
        discovery_mod, "_deactivate_stale",
    ) as deactivate:
        discovery_mod.discover_campaigns_for_client(1)

    deactivate.assert_called_once()


# ---------------------------------------------------------------------------
# 4) A job BE VAN KÖTVE — enélkül minden fenti teszt hiába zöld
# ---------------------------------------------------------------------------

def _registered_jobs():
    """A `start_scheduler` által regisztrált jobok, valós APScheduler indítás nélkül."""
    jobs: dict[str, dict] = {}

    class _FakeScheduler:
        running = False

        def __init__(self, *a, **kw):
            pass

        def add_job(self, func, **kw):
            jobs[kw["id"]] = {"func": func, **kw}

        def start(self):
            _FakeScheduler.running = True

    with mock.patch.object(sched, "_scheduler", None), \
         mock.patch.object(sched, "AsyncIOScheduler", _FakeScheduler), \
         mock.patch.object(sched, "get_config",
                           return_value=SimpleNamespace(timezone="Europe/Budapest")):
        sched.start_scheduler()
    return jobs


def test_the_discovery_job_is_actually_registered_in_the_scheduler():
    """A job szerepel a schedulerben — ez volt az EREDETI hiba: sehol nem futott.

    A kampánylista hónapokig csak kézi `/discover` futtatáskor frissült, mert a
    `start_scheduler` egyetlen jobja sem hívta a discoveryt.
    """
    jobs = _registered_jobs()

    assert "daily_discovery" in jobs, (
        "nincs ütemezett discovery — a kampánylista megint csak kézzel frissülne"
    )
    assert jobs["daily_discovery"]["func"] is sched.daily_discovery_job


def test_the_discovery_job_runs_daily_at_a_quiet_hour():
    jobs = _registered_jobs()
    job = jobs["daily_discovery"]

    assert job["trigger"] == "cron"
    assert job["hour"] == 3, "hajnalban fut, amikor nincs más API-terhelés"
    assert job.get("day_of_week") is None, "minden nap fut, nem csak hétköznap"
    assert job["max_instances"] == 1, "egy futásnál több ne induljon egyszerre"
    assert job["misfire_grace_time"] >= 3600, (
        "hajnali deploy után is fusson le — egy kihagyott nap újra láthatatlan "
        "kampányokat jelentene"
    )


def test_the_discovery_job_does_not_collide_with_the_hourly_cycle():
    """NEM ugyanabban a percben fut, mint az óránkénti ciklus.

    A `hourly_monitoring` MINDEN óra :00-kor indul — 03:00-kor is. A discovery
    ott indítva pont a napi legnagyobb API-terheléssel esne egybe, ugyanazokra
    a fiókokra.
    """
    jobs = _registered_jobs()

    assert jobs["hourly_monitoring"]["minute"] == 0
    assert jobs["daily_discovery"]["minute"] != jobs["hourly_monitoring"]["minute"]


# ---------------------------------------------------------------------------
# 5) A parancsok ugyanazt a jobot hívják (nincs párhuzamos "kézi változat")
# ---------------------------------------------------------------------------

class _Interaction:
    def __init__(self, channel_id=1, user_id=42):
        self.channel_id = channel_id
        self.user = mock.Mock(id=user_id)
        self.response = mock.AsyncMock()
        self.followup = mock.AsyncMock()


@pytest.mark.asyncio
async def test_discover_all_calls_the_scheduled_job():
    """`/discover all` a cron függvényét hívja — nem egy másolatot.

    Ha itt egy párhuzamos implementáció futna, a parancs sikere semmit nem
    mondana arról, hogy hajnalban is működik-e (ez a hibaosztály korábban
    hetekig rejtett insight-kimaradást okozott).
    """
    stats = {
        "clients": 2, "accounts": 3, "accounts_failed": 0, "inserted": 5,
        "updated": 7, "deactivated": 1, "errors": 0, "failed_clients": 0,
        "per_client": [{"client_id": 1, "name": "A", "result": _result(inserted=5)}],
    }
    interaction = _Interaction()
    job = mock.AsyncMock(return_value=stats)

    with mock.patch.object(discovery_cmd, "_is_admin_channel", return_value=True), \
         mock.patch.object(discovery_cmd.scheduler_mod, "daily_discovery_job", new=job):
        await discovery_cmd.DiscoveryCog.all_.callback(
            discovery_cmd.DiscoveryCog(mock.Mock()), interaction)

    job.assert_awaited_once_with()
    assert discovery_cmd.scheduler_mod is sched


@pytest.mark.asyncio
async def test_discover_google_scopes_the_same_job_to_google_clients():
    """`/discover google` ugyanazt a jobot hívja, csak ügyfélkörre szűkítve."""
    accounts = [
        {"id": 1, "platform": "google", "client_id": 7},
        {"id": 2, "platform": "meta", "client_id": 8},
        {"id": 3, "platform": "google", "client_id": 7},
        {"id": 4, "platform": "google", "client_id": 9},
    ]
    stats = {
        "clients": 2, "accounts": 3, "accounts_failed": 0, "inserted": 0,
        "updated": 0, "deactivated": 0, "errors": 0, "failed_clients": 0,
        "per_client": [],
    }
    interaction = _Interaction()
    job = mock.AsyncMock(return_value=stats)

    with mock.patch.object(discovery_cmd, "_is_admin_channel", return_value=True), \
         mock.patch.object(discovery_cmd.ad_accounts_storage, "list_ad_accounts",
                           return_value=accounts), \
         mock.patch.object(discovery_cmd.scheduler_mod, "daily_discovery_job", new=job):
        await discovery_cmd.DiscoveryCog.google.callback(
            discovery_cmd.DiscoveryCog(mock.Mock()), interaction)

    job.assert_awaited_once_with(client_ids=[7, 9])


def test_the_job_summary_reports_unreachable_accounts():
    """Az összesítő KIMONDJA, ha fiókok nem voltak elérhetők.

    A néma "0 új kampány" pont attól veszélyes, hogy jól sikerült futásnak
    látszik, miközben fiókok maradtak ki jogosultsági hiba miatt.
    """
    stats = {
        "clients": 3, "accounts": 5, "accounts_failed": 2, "inserted": 0,
        "updated": 4, "deactivated": 0, "errors": 2, "failed_clients": 1,
        "per_client": [{"client_id": 1, "name": "A", "error": "boom"}],
    }
    leiras = discovery_cmd._job_description(stats)

    assert "2 fiók nem elérhető" in leiras
    assert "1 ügyfél elszállt" in leiras
    assert discovery_cmd._job_had_problems(stats) is True
