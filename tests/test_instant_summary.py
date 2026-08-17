"""
Azonnali (real-time) fiók-összefoglaló — `/account summary-now`.

A legfontosabb elvárás, hogy a parancs MELLÉKHATÁS-MENTES legyen: élő adatot
húz és detektál, de NEM ír insightot, NEM rögzít alertet és NEM küld riasztást.
Enélkül minden lefuttatás megduplázná az órás ciklus alertjeit és spamelné az
OM-eket.
"""
from __future__ import annotations

import pytest

from src.monitoring import instant_summary as isum


_ACCOUNT = {
    "id": 76,
    "external_account_id": "act_129819940882364",
    "platform": "meta",
    "account_name": "GTX reklama",
    "client_id": 74,
}


def _campaign(cid: int, ext: str, state: str = "mature", monitored: bool = True):
    return {
        "id": cid,
        "external_campaign_id": ext,
        "name": f"Kampany {cid}",
        "lifecycle_state": state,
        "is_monitored": monitored,
    }


@pytest.fixture
def wired(monkeypatch):
    """Élő API + DB helyett kontrollált adat; a hívásokat számoljuk."""
    calls = {"pull": 0, "detect": []}

    campaigns = [
        _campaign(1, "100"),
        _campaign(2, "200"),
        _campaign(3, "300", state="paused"),      # kihagyandó
        _campaign(4, "400", monitored=False),     # kihagyandó
        _campaign(5, "500"),                      # nem lesz rá mai adat
    ]

    insights = [
        {"external_campaign_id": "100", "spend": 1000.0, "roas": 1.0},
        {"external_campaign_id": "200", "spend": 500.0, "roas": 4.0},
        {"external_campaign_id": "300", "spend": 999.0, "roas": 0.1},  # paused → nem nézzük
    ]

    async def _fake_pull(platform, ext, day):
        calls["pull"] += 1
        return list(insights)

    async def _fake_detect(campaign_id, insight):
        calls["detect"].append(campaign_id)
        if campaign_id == 1:
            return [{
                "campaign_id": 1, "severity": "critical",
                "metric": "roas_drop", "message": "ROAS 1.0 (cél 3.0)",
            }]
        return []

    monkeypatch.setattr(isum, "_batch_pull", _fake_pull)
    monkeypatch.setattr(isum, "detect_anomalies_for_campaign", _fake_detect)
    monkeypatch.setattr(
        isum.campaigns_storage, "get_campaigns_by_ad_account", lambda _id: campaigns
    )
    return calls


@pytest.mark.asyncio
async def test_csak_egy_api_hivas_fiokonkent(wired):
    """A hatókör-korlát lényege: fiókonként PONTOSAN egy batch hívás."""
    await isum.generate_instant_account_summary(_ACCOUNT, client_name="GTX")
    assert wired["pull"] == 1


@pytest.mark.asyncio
async def test_paused_es_nem_monitorozott_kampany_kimarad(wired):
    s = await isum.generate_instant_account_summary(_ACCOUNT, client_name="GTX")
    # 5-bol 3 figyelt (a paused es a nem-monitored kiesik)
    assert s["total_campaigns"] == 3
    # Detektor csak azokra futott, amikre volt MAI adat (1 es 2)
    assert sorted(wired["detect"]) == [1, 2]


@pytest.mark.asyncio
async def test_szamlalok_es_kolteg(wired):
    s = await isum.generate_instant_account_summary(_ACCOUNT, client_name="GTX")
    assert s["critical_count"] == 1
    assert s["warning_count"] == 0
    assert s["alert_count"] == 1
    assert s["campaigns_with_data"] == 2
    assert s["healthy_campaigns"] == 2       # 3 figyelt - 1 problemas
    assert s["spend_today"] == 1500.0        # csak a figyelt kampanyoke
    assert s["is_live"] is True


@pytest.mark.asyncio
async def test_top_issues_a_napi_osszefoglalo_szerkezeteben(wired):
    s = await isum.generate_instant_account_summary(_ACCOUNT, client_name="GTX")
    assert len(s["top_issues"]) == 1
    issue = s["top_issues"][0]
    assert set(issue) == {
        "client", "campaign", "platform", "account_label", "severity", "message",
    }
    assert issue["client"] == "GTX"
    assert issue["campaign"] == "Kampany 1"
    assert issue["severity"] == "critical"


@pytest.mark.asyncio
async def test_nem_ir_adatbazisba(monkeypatch, wired):
    """Read-only garancia: se insight-mentes, se alert-insert, se routing."""
    import src.monitoring.scheduler as sched

    hits = []
    for mod, name in (
        (sched, "insert_campaign_insight"),
        (sched, "insert_alert"),
        (sched, "route_alert"),
    ):
        if hasattr(mod, name):
            monkeypatch.setattr(
                mod, name,
                lambda *a, _n=name, **k: hits.append(_n),
            )

    await isum.generate_instant_account_summary(_ACCOUNT, client_name="GTX")
    assert hits == [], f"az azonnali osszefoglalo irt valahova: {hits}"

    # A modul egyaltalan ne is hivatkozzon iro fuggvenyekre.
    src = (isum.__file__)
    with open(src, encoding="utf-8") as fh:
        code = fh.read()
    for forbidden in ("insert_alert", "insert_campaign_insight", "route_alert",
                      "set_lifecycle_state"):
        assert forbidden not in code, f"tiltott iro hivas a modulban: {forbidden}"


@pytest.mark.asyncio
async def test_detektor_hiba_nem_bukik_meg_mindent(monkeypatch, wired):
    async def _boom(campaign_id, insight):
        raise RuntimeError("detektor hiba")

    monkeypatch.setattr(isum, "detect_anomalies_for_campaign", _boom)
    s = await isum.generate_instant_account_summary(_ACCOUNT, client_name="GTX")
    assert s["alert_count"] == 0
    assert s["campaigns_with_data"] == 2


@pytest.mark.asyncio
async def test_api_hiba_tovabbdobodik(monkeypatch, wired):
    """Az orai ciklussal ellentetben itt a felhasznalonak latnia kell a hibat."""
    async def _boom(platform, ext, day):
        raise RuntimeError("Meta API 401")

    monkeypatch.setattr(isum, "_batch_pull", _boom)
    with pytest.raises(RuntimeError, match="Meta API 401"):
        await isum.generate_instant_account_summary(_ACCOUNT, client_name="GTX")


# ---------------------------------------------------------------------------
# Formázás
# ---------------------------------------------------------------------------

def _base_summary(**over):
    base = {
        "total_campaigns": 324, "campaigns_with_data": 5,
        "critical_count": 0, "warning_count": 0, "alert_count": 0,
        "healthy_campaigns": 5, "top_issues": [], "spend_today": 131.0,
        "fetched_at": "2026-07-26T20:40:00+02:00", "day": "2026-07-26",
        "account_label": "GTX reklama", "platform": "meta", "is_live": True,
    }
    base.update(over)
    return base


def test_format_jelzi_hogy_elo_adat():
    text = isum.format_instant_summary(_base_summary())
    assert "Élő adat" in text
    assert "2026-07-26 20:40" in text


def test_format_ezres_tagolas_nem_rontja_el_a_sort():
    """A szóközös ezres tagolás CSAK a számra vonatkozhat."""
    text = isum.format_instant_summary(_base_summary(spend_today=1234567.0))
    assert "1 234 567" in text
    assert "Élő adat, lekérve" in text, "a sor vesszője eltűnt"


def test_format_nincs_adat_ag():
    text = isum.format_instant_summary(_base_summary(campaigns_with_data=0))
    assert "Ma még egyik figyelt kampányra sincs adat" in text
    assert "324" in text


def test_format_anomaliakkal_es_figyelmeztetessel():
    text = isum.format_instant_summary(_base_summary(
        critical_count=2, warning_count=3, alert_count=5, healthy_campaigns=1,
        top_issues=[{
            "client": "GTX", "campaign": "Kampany 1", "platform": "meta",
            "account_label": "GTX reklama", "severity": "critical",
            "message": "ROAS 1.0 (cél 3.0)",
        }],
    ))
    assert "Kritikus: 2" in text
    assert "Figyelmeztetés: 3" in text
    assert "Problémák MOST:" in text
    assert "ROAS 1.0" in text
    # A felhasznalonak tudnia kell, hogy ez reszleges nap es nem rogzitett alert.
    assert "nap még nem zárult le" in text
    assert "Riasztás nem került rögzítésre" in text


# ---------------------------------------------------------------------------
# Teljes problémalista (NINCS top-5 vágás) — a napi összefoglalóval azonos elv
# ---------------------------------------------------------------------------

@pytest.fixture
def wired_many(monkeypatch):
    """Egy fiók sok anomáliával — a listázási limit vizsgálatához."""
    n_campaigns = 40
    campaigns = [_campaign(i, str(i)) for i in range(1, n_campaigns + 1)]
    insights = [
        {"external_campaign_id": str(i), "spend": 10.0, "roas": 1.0}
        for i in range(1, n_campaigns + 1)
    ]

    async def _fake_pull(platform, ext, day):
        return list(insights)

    async def _fake_detect(campaign_id, insight):
        # Minden kampány ad egy anomáliát: felváltva critical / warning.
        severity = "critical" if campaign_id % 2 else "warning"
        return [{
            "campaign_id": campaign_id, "severity": severity,
            "metric": "roas_drop", "message": f"ROAS gond #{campaign_id}",
        }]

    monkeypatch.setattr(isum, "_batch_pull", _fake_pull)
    monkeypatch.setattr(isum, "detect_anomalies_for_campaign", _fake_detect)
    monkeypatch.setattr(
        isum.campaigns_storage, "get_campaigns_by_ad_account", lambda _id: campaigns
    )
    return n_campaigns


@pytest.mark.asyncio
async def test_minden_anomalia_bekerul_a_listaba_nem_csak_top5(wired_many):
    """A fő regresszió: korábban fix top-5 volt, 40 anomáliából 35 kimaradt."""
    s = await isum.generate_instant_account_summary(_ACCOUNT, client_name="GTX")
    assert len(s["top_issues"]) == wired_many == 40
    assert s["issues_truncated"] == 0
    assert s["alert_count"] == 40
    assert s["critical_count"] + s["warning_count"] == 40


@pytest.mark.asyncio
async def test_biztonsagi_plafon_jelzi_a_kimaradt_sorokat(monkeypatch, wired_many):
    monkeypatch.setattr(isum, "_MAX_ISSUE_LINES", 25)
    s = await isum.generate_instant_account_summary(_ACCOUNT, client_name="GTX")

    assert len(s["top_issues"]) == 25
    assert s["issues_truncated"] == 15
    # Az összesített szám a TELJES halmaz, nem a kilistázott soroké.
    assert s["alert_count"] == 40


def test_format_hosszu_lista_csoportositva_jelenik_meg():
    issues = (
        [{"client": "GTX", "campaign": f"C{i}", "platform": "meta",
          "account_label": "GTX reklama", "severity": "critical", "message": "ROAS 0.5"}
         for i in range(12)]
        + [{"client": "GTX", "campaign": f"W{i}", "platform": "meta",
            "account_label": "GTX reklama", "severity": "warning", "message": "CTR -20%"}
           for i in range(8)]
    )
    text = isum.format_instant_summary(_base_summary(
        critical_count=12, warning_count=8, alert_count=20,
        healthy_campaigns=5, top_issues=issues,
    ))

    assert "🔴 **KRITIKUS: 12 db**" in text
    assert "🟡 **FIGYELMEZTETÉS: 8 db**" in text
    assert text.index("KRITIKUS: 12 db") < text.index("FIGYELMEZTETÉS: 8 db")
    for i in range(12):
        assert f"C{i}" in text
    for i in range(8):
        assert f"W{i}" in text


def test_format_rovid_lista_lapos_marad():
    issues = [{"client": "GTX", "campaign": f"C{i}", "platform": "meta",
               "account_label": "GTX reklama", "severity": "critical", "message": "ROAS 0.5"}
              for i in range(4)]
    text = isum.format_instant_summary(_base_summary(
        critical_count=4, alert_count=4, healthy_campaigns=1, top_issues=issues,
    ))

    assert "KRITIKUS: 4 db" not in text
    for i in range(4):
        assert f"C{i}" in text


def test_format_soraiban_nincs_redundans_fiokcimke():
    """A fiók és a platform már a fejlécben szerepel — a sorokban ne ismétlődjön."""
    text = isum.format_instant_summary(_base_summary(
        critical_count=1, alert_count=1, healthy_campaigns=4,
        top_issues=[{
            "client": "GTX", "campaign": "Prospecting", "platform": "meta",
            "account_label": "GTX reklama", "severity": "critical",
            "message": "ROAS 0.5",
        }],
    ))
    assert "• GTX — Prospecting — ROAS 0.5" in text
