"""
Heti riport-összefoglaló & akciójavaslat (ügyfelenként, hétfő reggel).

Mit csinál egy futás:
    1. Kiszámolja az ELŐZŐ teljes hét ablakát (hétfő 00:00 → hétfő 00:00,
       Europe/Budapest, fél-nyitott — lásd `previous_week_range`).
    2. Minden aktív, legalább egy `mature` kampánnyal rendelkező ügyfélre a MÁR
       MEGLÉVŐ `campaign_insights` sorokból aggregálja a heti számokat.
       SEMMILYEN Meta/Google API hívás nincs — az adat óránként amúgy is gyűlik.
    3. Elmenti az aggregátumot a `weekly_report_metrics` cache-be, és onnan
       olvassa az AZT MEGELŐZŐ hét számait a változás %-hoz (a nyers sorokat az
       óránkénti ciklus 7 nap után törli — lásd a 0013 migration fejlécét).
    4. Claude Sonnet-tel írat vezetői összefoglalót + 3 pontos akciótervet
       (STRUKTURÁLT JSON válasz).
    5. ClickUp Doc-ot hoz létre a konfigurált Folder/Space alatt.

MIÉRT NEM A CLAUDE SZÁMOL: a kulcsmetrikák táblázatát KÓDBÓL építjük a már
kiszámolt aggregátumból. Az AI-val számoltatott szám drágább is, és — ami
rosszabb — némán téved; a riport hitelessége azon áll, hogy a táblázat a DB-vel
egyezik. A Claude csak azt kapja feladatul, amihez ért: értelmezni és javasolni.

Hibatűrés: egy ügyfél hibája (Claude vagy ClickUp) NEM állítja meg a kört — a
`generate_weekly_action_reports` ügyfelenként izolál, számol, és a végén egy
összesítő logsort ír. Ugyanezt a függvényt hívja a hétfői cron job ÉS a
`/report weekly-now` parancs is — nincs párhuzamos implementáció.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.config import get_config
from src.integrations import clickup
from src.storage import alerts as alerts_storage
from src.storage import campaigns as campaigns_storage
from src.storage import clients as clients_storage
from src.storage import weekly_metrics as weekly_metrics_storage
from src.utils.logging import get_logger

log = get_logger(__name__)

# A riport csak olyan ügyfélre készül, akinek van már "felnőtt" kampánya — a
# tanulási fázisban (new/learning) lévő fiókokra a heti trend félrevezető.
_ELIGIBLE_LIFECYCLE = "mature"

# Legfeljebb ennyi akciópont kerül a Docba (a prompt 3-at kér; ez a felső korlát,
# ha a modell többet adna vissza).
_MAX_ACTION_ITEMS = 5

# A JSON válasz elfér ennyiben (3-5 mondat + 3 feladat), de a modellnek legyen
# levegője, hogy ne csonkuljon félbe a JSON — a csonka JSON parse-olhatatlan.
_MAX_TOKENS = 1500

_SYSTEM = (
    "Te egy tapasztalt PPC (Meta Ads / Google Ads) stratéga vagy, aki egy magyar "
    "ügynökség heti ügyfél-riportjához ír elemzést.\n"
    "A számokat KÉSZEN KAPOD — ne számolj újra és ne találj ki újakat, csak "
    "értelmezd őket.\n"
    "KIZÁRÓLAG egyetlen JSON objektummal válaszolj, minden más szöveg, "
    "magyarázat vagy kódblokk-jelölés nélkül. A séma pontosan ez:\n"
    '{"vezetoi_osszefoglalo": "3-5 mondat magyarul, emberi nyelven, '
    'szakzsargon nélkül", '
    '"akcioterv": ["konkrét, elvégezhető feladat", "...", "..."]}\n'
    "Az akcióterv PONTOSAN 3 elemet tartalmazzon. Minden elem egy konkrét, a "
    "következő héten elvégezhető lépés legyen (mit, hol, milyen irányba) — ne "
    "általánosság, mint „optimalizáld a kampányokat”."
)


# ---------------------------------------------------------------------------
# Időablak
# ---------------------------------------------------------------------------

def _tz() -> ZoneInfo:
    return ZoneInfo(get_config().timezone or "UTC")


def previous_week_range(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Az ELŐZŐ TELJES naptári hét: hétfő 00:00 → a rákövetkező hétfő 00:00.

    A határok fél-nyitottak, ugyanaz a konvenció, mint a `summary.daily_range`-nél:
        fetched_at >= from_dt  ÉS  fetched_at < to_dt
    Az alsó határ inkluzív (a hétfő 00:00:00-kor mért sor benne van), a felső
    exkluzív — így a vasárnap 23:59:59.999999 még beleesik, a hétfő 00:00:00 már
    nem. Ez pontosabb, mint egy `<= vasárnap 23:59:59` felső határ, ami a
    másodperc törtrészében keletkezett méréseket némán elhagyná.

    NEM gördülő 7×24 óra: a `now`-ból CSAK a naptári napot vesszük, az időt
    nullázzuk. Így teljesen mindegy, hogy a cron hétfő 08:00-kor futtatja, vagy
    valaki kézzel kéri le a `/report weekly-now`-val szerdán — MINDKETTŐ
    ugyanarra a lezárult hétre számol. Ez szándékos: a kézi teszt pontosan azt
    mutassa, amit a hétfői futás produkálna.

    Óraátállításkor is teljes naptári heteket fed: a `timedelta` itt fali-óra
    aritmetika (hétfő 00:00 − 7 nap = az előző hétfő 00:00), nem "168 óra".
    """
    tz = _tz()
    now = now.astimezone(tz) if now else datetime.now(tz)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    this_monday0 = today0 - timedelta(days=now.weekday())   # weekday(): hétfő=0
    return this_monday0 - timedelta(days=7), this_monday0


def week_label(from_dt: datetime, to_dt: datetime) -> str:
    """A hét ember-olvasható címkéje: "2026-08-10 – 2026-08-16".

    A `to_dt` EXKLUZÍV (a rákövetkező hétfő), ezért a megjelenített záró nap
    eggyel korábbi — különben a címke egy nappal többet ígérne, mint amennyi
    adat benne van.
    """
    return f"{from_dt.date().isoformat()} – {(to_dt - timedelta(days=1)).date().isoformat()}"


# ---------------------------------------------------------------------------
# Aggregáció (tiszta függvények — DB nélkül tesztelhetők)
# ---------------------------------------------------------------------------

_SUM_METRICS = ("spend", "impressions", "clicks", "conversions", "conversion_value")


def _num(value: Any) -> float | None:
    """Szám vagy None. A Supabase numeric mezői stringként is érkezhetnek."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _div(numerator: float | None, denominator: float | None) -> float | None:
    """Biztonságos osztás — None, ha bármelyik oldal hiányzik vagy a nevező 0."""
    if numerator is None or not denominator:
        return None
    return numerator / denominator


def aggregate_weekly_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Nyers (óránkénti) campaign_insights sorokból heti összesítés.

    KRITIKUS a helyességhez: a `campaign_insights` sorai ÓRÁNKÉNTIAK, de a NAPRA
    HALMOZOTTAK — a batch pull mindig az adott nap eddigi ÖSSZEGÉT hozza (lásd
    `storage.insights_history` modul-docstring). A sorok naiv összeadása ezért
    a napi költés ~24-szeresét adná.

    Ezért két lépés:
      1. kampányonként és NAPONKÉNT a LEGUTOLSÓ sor (= aznap legteljesebb
         kumulált érték) — ugyanaz a szabály, mint az
         `insights_history.to_daily_series`-ben, hogy a heti riport és az
         insight-motor ugyanazt értse "napi értéken";
      2. ezek összege a hét összes napjára és az összes kampányra.

    A bemenet fetched_at szerint NÖVEKVŐ (lásd
    `weekly_metrics.get_insight_rows_for_campaigns`) — így a később jövő sor
    felülírja a korábbit, és a végén az aznapi utolsó marad. A fetched_at
    nélküli sorok (mock/teszt) mindegyike külön "napnak" számít, nem olvadnak
    össze.

    Származtatott metrikák a NYERS ÖSSZEGEKBŐL, nem sor-átlagként:
        ctr  = clicks / impressions   → ez PONTOSAN az impresszió-súlyozott CTR
        cpa  = spend / conversions
        roas = conversion_value / spend  → None, ha nincs bevétel-adat

    Visszatérés: mindig teljes kulcskészlet (a hívónak ne kelljen `.get()`-elnie);
    az érték None, ha nem számolható.
    """
    daily_last: dict[tuple[Any, str], dict[str, Any]] = {}
    for idx, row in enumerate(rows):
        fetched = row.get("fetched_at")
        # Napi kulcs a fetched_at dátum-része (UTC) — ugyanaz a konvenció, mint
        # a to_daily_series-ben. Ha hiányzik, egyedi index-kulcs.
        day = str(fetched)[:10] if fetched else f"__{idx}"
        daily_last[(row.get("campaign_id"), day)] = row

    totals: dict[str, Any] = {key: 0.0 for key in _SUM_METRICS}
    has_value: dict[str, bool] = {key: False for key in _SUM_METRICS}

    for row in daily_last.values():
        for key in _SUM_METRICS:
            value = _num(row.get(key))
            if value is None:
                continue
            totals[key] += value
            has_value[key] = True

    # Ha egy metrikára EGYETLEN mérés sem volt, az None (nem 0). A kettő nem
    # ugyanaz: a "0 Ft költés" tény, a "nem tudjuk" nem az — és a változás %
    # számításánál a nullával osztás így nem tűnik valós adatnak.
    result: dict[str, Any] = {
        key: (totals[key] if has_value[key] else None) for key in _SUM_METRICS
    }
    result["ctr"] = _div(result["clicks"], result["impressions"])
    result["cpa"] = _div(result["spend"], result["conversions"])
    result["roas"] = _div(result["conversion_value"], result["spend"])
    result["campaign_days"] = len(daily_last)
    return result


def pct_change(current: Any, previous: Any) -> float | None:
    """Változás százalékban: (most − előző) / előző × 100.

    None, ha bármelyik érték hiányzik, VAGY ha az előző 0 volt — a nullához
    képesti változásnak nincs értelmes százaléka (a "+∞%" nem információ).
    """
    cur = _num(current)
    prev = _num(previous)
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / prev * 100.0


# ---------------------------------------------------------------------------
# Formázás
# ---------------------------------------------------------------------------

_MISSING = "nincs adat"


def _hu(text: str) -> str:
    """Magyar számformátum: ezres elválasztó szóköz, tizedes vessző."""
    return text.replace(",", " ").replace(".", ",")


def _fmt_money(value: Any) -> str:
    v = _num(value)
    return _MISSING if v is None else f"{_hu(f'{v:,.0f}')} Ft"


def _fmt_pct_from_ratio(value: Any) -> str:
    """Arány (0.0512) → "5,12%". A campaign_insights.ctr arányként tárol."""
    v = _num(value)
    return _MISSING if v is None else _hu(f"{v * 100:.2f}") + "%"


def _fmt_ratio(value: Any) -> str:
    v = _num(value)
    return _MISSING if v is None else _hu(f"{v:.2f}")


def _fmt_change(value: Any) -> str:
    v = _num(value)
    if v is None:
        return _MISSING
    return f"{'+' if v >= 0 else ''}{_hu(f'{v:.1f}')}%"


# (kulcs, címke, formázó) — a táblázat sorai a spec sorrendjében.
_METRIC_SPECS: tuple[tuple[str, str, Any], ...] = (
    ("spend", "Költés", _fmt_money),
    ("ctr", "CTR", _fmt_pct_from_ratio),
    ("cpa", "CPA", _fmt_money),
    ("roas", "ROAS", _fmt_ratio),
)


def build_metric_rows(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """A kulcsmetrikák táblázatának sorai (már formázott szövegként).

    A ROAS sor KIMARAD, ha sem ezen a héten, sem az előzőn nincs bevétel-adat —
    a spec szerint ("ha van bevétel adat, egyébként hagyd ki ezt a sort"). Egy
    lead-generációs ügyfélnél a „ROAS: nincs adat" sor csak zaj lenne.

    Ugyanezeket a sorokat kapja a Markdown-építő ÉS a Claude prompt is: így a
    Docban látható táblázat és az AI által elemzett számok nem tudnak eltérni.
    """
    prev = previous or {}
    rows: list[dict[str, str]] = []
    for key, label, fmt in _METRIC_SPECS:
        cur_value = current.get(key)
        prev_value = prev.get(key)
        if key == "roas" and _num(cur_value) is None and _num(prev_value) is None:
            continue
        rows.append({
            "label": label,
            "current": fmt(cur_value),
            "previous": fmt(prev_value),
            "change": _fmt_change(pct_change(cur_value, prev_value)),
        })
    return rows


def format_alert_summary(alert_counts: list[dict[str, Any]]) -> str:
    """A heti CRITICAL/WARNING riasztások rövid szöveges bontása a prompthoz."""
    if not alert_counts:
        return "Nem keletkezett CRITICAL vagy WARNING riasztás ezen a héten."
    parts = [
        f"{(row.get('severity') or '?').upper()} / {row.get('metric') or '?'}: "
        f"{row.get('count', 0)} db"
        for row in alert_counts
    ]
    total = sum(int(row.get("count") or 0) for row in alert_counts)
    return f"Összesen {total} riasztás — " + "; ".join(parts)


# ---------------------------------------------------------------------------
# Claude Sonnet elemzés
# ---------------------------------------------------------------------------

# Lazán inicializált AsyncAnthropic kliens (egyszer, folyamat-szinten) — ugyanaz
# a minta, mint az `ai_insights._get_client`-ben.
_client: Any = None


def _get_client() -> Any:
    """AsyncAnthropic kliens (singleton). RuntimeError ha nincs API kulcs."""
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic  # lazy import — bot indul SDK-hiba nélkül is

        api_key = get_config().anthropic_api_key
        if not api_key:
            raise RuntimeError("Hiányzó ANTHROPIC_API_KEY")
        _client = AsyncAnthropic(api_key=api_key)
    return _client


def build_analysis_prompt(
    client_name: str,
    label: str,
    metric_rows: list[dict[str, str]],
    alert_counts: list[dict[str, Any]],
) -> str:
    """A user-prompt: ügyfél + a KÉSZ metrika-táblázat + a heti riasztás-kontextus."""
    table_lines = [
        f"- {row['label']}: ez a hét {row['current']} | előző hét "
        f"{row['previous']} | változás {row['change']}"
        for row in metric_rows
    ]
    return (
        f"Ügyfél: {client_name}\n"
        f"Vizsgált hét: {label}\n\n"
        f"Heti kulcsmetrikák (készen kapod, ne számolj újra):\n"
        + "\n".join(table_lines)
        + "\n\n"
        f"A héten keletkezett riasztások:\n{format_alert_summary(alert_counts)}\n\n"
        f"Írd meg a vezetői összefoglalót és a következő heti akciótervet a "
        f"megadott JSON sémában!"
    )


def parse_analysis_response(text: str | None) -> dict[str, Any] | None:
    """A Claude válaszának JSON-parszolása. None, ha nem értelmezhető.

    Toleráns: a modell hajlamos ```json kódblokkba tenni a választ, vagy egy
    bevezető mondatot írni elé — ezért a legkülső `{ … }` párt vágjuk ki, nem a
    teljes szöveget adjuk a `json.loads`-nak.

    Validál is: üres összefoglaló vagy üres akcióterv NEM elfogadható válasz
    (None). Így a hívó hibaként számolja, ahelyett hogy egy üres Doc születne.
    """
    if not text:
        return None

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None

    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    summary = data.get("vezetoi_osszefoglalo")
    plan = data.get("akcioterv")
    if not isinstance(summary, str) or not summary.strip():
        return None
    if not isinstance(plan, list):
        return None

    items = [item.strip() for item in plan if isinstance(item, str) and item.strip()]
    if not items:
        return None

    return {
        "vezetoi_osszefoglalo": summary.strip(),
        "akcioterv": items[:_MAX_ACTION_ITEMS],
    }


async def generate_weekly_analysis(
    client_name: str,
    label: str,
    metric_rows: list[dict[str, str]],
    alert_counts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Vezetői összefoglaló + akcióterv Claude Sonnet-tel. None, ha nem parszolható.

    A hívást SZÁNDÉKOSAN nem burkoljuk try/except-be: az API-hiba (rate limit,
    hálózat) szálljon fel a hívó ügyfél-szintű izolációjához, hogy a `/report
    weekly-now` válaszában NÉVVEL és okkal jelenjen meg — a csendben elnyelt
    hiba pont az a hibaosztály, ami az insight scan-nél hetekig rejtve maradt.
    """
    cfg = get_config()
    prompt = build_analysis_prompt(client_name, label, metric_rows, alert_counts)

    client = _get_client()
    msg = await client.messages.create(
        model=cfg.claude_sonnet_model,
        max_tokens=_MAX_TOKENS,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(
        getattr(block, "text", "") for block in (msg.content or [])
        if getattr(block, "type", None) == "text"
    ).strip()
    return parse_analysis_response(text)


# ---------------------------------------------------------------------------
# A Doc tartalma
# ---------------------------------------------------------------------------

def build_doc_title(client_name: str, label: str) -> str:
    return f"Heti Riport — {client_name} — {label}"


def build_report_markdown(
    client_name: str,
    label: str,
    analysis: dict[str, Any],
    metric_rows: list[dict[str, str]],
) -> str:
    """A ClickUp Doc oldal Markdown tartalma (a spec szerinti struktúra)."""
    lines = [
        f"# {build_doc_title(client_name, label)}",
        "",
        "## Vezetői összefoglaló",
        "",
        analysis.get("vezetoi_osszefoglalo", "").strip(),
        "",
        "## Kulcsmetrikák",
        "",
        "| Metrika | Ez a hét | Előző hét | Változás |",
        "|---|---|---|---|",
    ]
    for row in metric_rows:
        lines.append(
            f"| {row['label']} | {row['current']} | {row['previous']} | {row['change']} |"
        )

    lines += ["", "## Akcióterv a következő hétre", ""]
    for idx, item in enumerate(analysis.get("akcioterv") or [], start=1):
        lines.append(f"{idx}. {item}")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# A generátor — EZT hívja a cron job ÉS a /report weekly-now is
# ---------------------------------------------------------------------------

def _empty_stats() -> dict[str, Any]:
    """Teljes kulcskészlet a korai kilépési ágakhoz is (a hívó ne `.get()`-eljen)."""
    return {
        "total": 0,
        "success": 0,
        "failed": 0,
        "skipped_no_mature": 0,
        "skipped_no_data": 0,
        "skipped_config": 0,
        "config_error": None,
        "errors": [],
        "docs": [],
        "week_label": "",
    }


def _has_enough_data(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> bool:
    """Van-e értelme riportot generálni: legalább az egyik héten volt költés."""
    return bool(_num(current.get("spend"))) or bool(_num((previous or {}).get("spend")))


def _config_problem() -> str | None:
    """Van-e olyan hiányzó beállítás, ami miatt Doc nem tud születni? None ha nincs.

    EGYSZER, a kör elején fut — nem ügyfelenként. Így egy hiányzó token nem húsz
    azonos warningként jelenik meg a logban, és — ami fontosabb — nem hívunk
    húsz Claude kérést olyan riportokhoz, amiket úgysem tudnánk kézbesíteni.
    """
    if not get_config().anthropic_api_key:
        return "hiányzik az `ANTHROPIC_API_KEY`"
    return clickup.weekly_report_config_error()


async def generate_weekly_action_reports(
    *,
    client_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Heti riport minden jogosult ügyfélre. EZ a hétfői cron ÉS a parancs közös belépője.

    Paraméterek:
        client_id — csak erre az ügyfélre (gyorsabb kézi teszteléshez)
        now       — a "most" felülírása (teszteléshez); az ablakot ebből számoljuk

    Jogosultság: `clients.is_active = true` ÉS van legalább 1 `mature` kampánya.
    Az AGGREGÁCIÓ viszont az ügyfél ÖSSZES kampányát nézi (az 'ended'/'paused'
    állapotúakat is): a múlt heti költés akkor is a hétre esik, ha a kampányt
    azóta leállították — a riport visszatekintő, nem pillanatkép.

    Visszatérés (mindig teljes kulcskészlettel — ebből épül a Discord válasz ÉS
    a záró log sor is):
        {"total":              feldolgozott (adattal rendelkező) ügyfelek,
         "success":            elkészült ClickUp Doc,
         "failed":             hibázott ügyfelek,
         "skipped_no_mature":  nincs mature kampánya,
         "skipped_no_data":    egyik héten sem volt költése,
         "skipped_config":     hiányzó ANTHROPIC/CLICKUP beállítás miatt maradt ki,
         "config_error":       mi hiányzik (str) vagy None,
         "errors":             [{"client": név, "error": ok}, …],
         "docs":               [{"client": név, "url": …}, …],
         "week_label":         "2026-08-10 – 2026-08-16"}
    """
    stats = _empty_stats()

    from_dt, to_dt = previous_week_range(now)
    label = week_label(from_dt, to_dt)
    stats["week_label"] = label
    week_start = from_dt.date()
    prev_week_start = (from_dt - timedelta(days=7)).date()

    log.info("Heti riport job indítva — vizsgált hét: %s", label)

    # Konfiguráció EGYSZER. Ha hiányos, a számokat akkor is kiszámoljuk és
    # cache-eljük (hogy jövő héten legyen mihez hasonlítani), de Claude-ot nem
    # hívunk és Docot nem hozunk létre.
    problem = _config_problem()
    stats["config_error"] = problem
    if problem:
        log.warning(
            "Heti riport: %s — a Doc-ok NEM készülnek el, de a heti "
            "aggregátumok a cache-be mentődnek.", problem,
        )

    try:
        clients = await asyncio.to_thread(clients_storage.list_clients, active_only=True)
    except Exception:
        log.exception("Heti riport: az ügyféllista lekérése sikertelen — job kihagyva")
        return stats

    if client_id is not None:
        clients = [c for c in clients if c.get("id") == client_id]

    for client in clients:
        cid = client.get("id")
        name = client.get("name") or f"#{cid}"
        try:
            await _process_client(
                cid, name, from_dt, to_dt, label, week_start, prev_week_start,
                skip_delivery=bool(problem), stats=stats,
            )
        except Exception as exc:  # noqa: BLE001 — egy ügyfél hibája ne állítsa le a kört
            stats["failed"] += 1
            stats["errors"].append({"client": name, "error": str(exc)[:300]})
            # `exception` (nem `error`): traceback nélkül nem derül ki, MI hasalt el.
            log.exception("Heti riport: ügyfél #%s (%s) hiba", cid, name)

    log.info(
        "Heti riport kész: %d ügyfél feldolgozva, %d sikeres, %d hibázott "
        "(kihagyva: %d mature kampány nélkül, %d adat nélkül, %d hiányzó "
        "beállítás miatt; hét: %s)",
        stats["total"], stats["success"], stats["failed"],
        stats["skipped_no_mature"], stats["skipped_no_data"],
        stats["skipped_config"], label,
    )
    return stats


async def _process_client(
    cid: int,
    name: str,
    from_dt: datetime,
    to_dt: datetime,
    label: str,
    week_start: date,
    prev_week_start: date,
    *,
    skip_delivery: bool,
    stats: dict[str, Any],
) -> None:
    """Egy ügyfél riportja. Kivételt DOB — a hívó izolál és számol."""
    campaigns = await asyncio.to_thread(
        campaigns_storage.list_campaigns, cid, active_only=False
    )
    if not any(
        (c.get("lifecycle_state") or "").lower() == _ELIGIBLE_LIFECYCLE
        for c in campaigns
    ):
        stats["skipped_no_mature"] += 1
        log.debug("Heti riport: ügyfél #%s (%s) — nincs mature kampánya, kihagyva", cid, name)
        return

    campaign_ids = [c["id"] for c in campaigns if c.get("id") is not None]
    rows = await asyncio.to_thread(
        weekly_metrics_storage.get_insight_rows_for_campaigns,
        campaign_ids, from_dt, to_dt,
    )
    current = aggregate_weekly_totals(rows)

    # A cache-írás a KAPUK ELŐTT és a Claude/ClickUp lépés ELŐTT történik: a
    # jövő heti összehasonlítás így akkor is megmarad, ha az AI vagy a ClickUp
    # most elhasal — vagy ha ezt az ügyfelet most adathiány miatt kihagyjuk.
    await asyncio.to_thread(
        weekly_metrics_storage.upsert_cached_week, cid, week_start, current
    )
    previous = await asyncio.to_thread(
        weekly_metrics_storage.get_cached_week, cid, prev_week_start
    )

    if not _has_enough_data(current, previous):
        stats["skipped_no_data"] += 1
        log.debug(
            "Heti riport: ügyfél #%s (%s) — egyik héten sem volt költés, kihagyva",
            cid, name,
        )
        return

    stats["total"] += 1
    metric_rows = build_metric_rows(current, previous)

    if skip_delivery:
        stats["skipped_config"] += 1
        return

    alert_counts = await asyncio.to_thread(
        alerts_storage.get_alert_counts_for_campaigns, campaign_ids, from_dt, to_dt
    )

    analysis = await generate_weekly_analysis(name, label, metric_rows, alert_counts)
    if analysis is None:
        raise RuntimeError(
            "a Claude elemzés nem adott értelmezhető JSON választ (részletek a logban)"
        )

    markdown = build_report_markdown(name, label, analysis, metric_rows)
    doc = await clickup.create_weekly_report_doc(
        build_doc_title(name, label), markdown
    )
    if doc is None:
        raise RuntimeError(
            "a ClickUp Doc létrehozása sikertelen (a pontos okot a Railway log "
            "`ClickUp …` sorai mutatják)"
        )

    stats["success"] += 1
    stats["docs"].append({"client": name, "url": doc.get("url")})
    log.info("Heti riport kész: %s → %s", name, doc.get("url"))
