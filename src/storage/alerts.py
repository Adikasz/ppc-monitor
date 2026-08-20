"""
Riasztás (alerts tábla) adathozzáférés.

Az `alerts` tábla minden detektált anomáliát naplóz (deduplikáció,
összefoglaló, audit alapja). A detektor (src.monitoring.detector) állítja elő
az anomáliákat, ez a modul perzisztálja őket.

Séma (0001_initial_schema.sql, §9 — a fontos mezők):
    id, campaign_id, client_id, severity ('critical'|'warning'|'insight'),
    metric, observed_value, threshold_value, message,
    status ('pending'|'sent'|'suppressed'|'summarized', default 'pending'),
    detected_at (default now()), dedup_key

Függvények:
    insert_alert(...)  — egy alert beszúrása deduplikációval
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.storage.supabase_client import get_supabase
from src.utils.logging import get_logger

_TABLE = "alerts"

log = get_logger(__name__)


def insert_alert(
    campaign_id: int,
    severity: str,
    metric: str,
    observed_value: float | None,
    threshold_value: float | None,
    message: str,
) -> dict[str, Any] | None:
    """Egy riasztás beszúrása az alerts táblába, deduplikációval.

    Deduplikáció:
        A `dedup_key = "{campaign_id}_{metric}_{ma}"` kulcsra naponta egyetlen
        SOR létezik. Ha a mai napra ezzel a kulccsal már van sor (bármilyen
        státuszban), nem szúrunk be újat — különben az óránkénti ciklus a már
        kiküldött riasztást is újraküldené ugyanaznap.

    Frissítés duplikátum esetén (a Discord-üzenet NEM megy ki újra):
        A meglévő sort a FRISS méréssel felülírjuk. Enélkül a nap első
        mérése ragadt volna bent egész napra: a 02:00-kor mért "CPA 13 620 Ft"
        maradt az összefoglalóban akkor is, ha 15:00-ra 5 732 Ft-ra állt be —
        pedig az utóbbi a nap valós képe.

        Amit felülírunk: severity, observed_value, threshold_value, message és
        `detected_at`. A `detected_at` frissítése szándékos: az összefoglaló
        soronként kiírja az észlelés idejét, és az ott látható SZÁMHOZ tartozó
        időpontnak kell szerepelnie — különben az üzenet egy 15:00-s értéket
        02:00-s időbélyeggel mutatna.

        Amit NEM nyúlunk: `status`, `sent_at`, `discord_message_id` és a
        routing mezői — a riasztás naponta EGYSZER megy ki, a frissítés csak a
        tárolt értéket pontosítja.

        KORLÁT: ha a kampány időközben RENDBE JÖN, a detektor már nem ad
        anomáliát, tehát nincs mit frissíteni — a sor a nap végéig a legutolsó
        rossz értéken marad. Ezt nem a dedup, hanem az adatmennyiség-kapuk
        (detector) hivatottak megelőzni, illetve egy külön alert-feloldás.

    Paraméterek:
        campaign_id     — érintett kampány DB ID-ja
        severity        — 'critical' | 'warning' (a séma 'insight'-ot is enged)
        metric          — mi váltotta ki (pl. 'cpa_spike', 'budget_depleted')
        observed_value  — mért érték (lehet None)
        threshold_value — küszöbérték (lehet None)
        message         — emberi olvasható üzenet

    Visszatérés:
        a beszúrt alert sor (dict) — új riasztás esetén (a router ezt kapja,
                                     és ilyenkor MEGY KI a Discord-üzenet)
        None                        — ma már volt ilyen; a sort frissítettük,
                                     de új üzenet nem megy ki
    """
    dedup_key = f"{campaign_id}_{metric}_{date.today().isoformat()}"
    sb = get_supabase()

    # Deduplikáció: létezik-e már ma ezzel a kulccsal?
    existing = (
        sb.table(_TABLE)
        .select("id")
        .eq("dedup_key", dedup_key)
        .limit(1)
        .execute()
    )
    if existing.data:
        _refresh_alert_measurement(
            existing.data[0]["id"],
            severity=severity,
            observed_value=observed_value,
            threshold_value=threshold_value,
            message=message,
        )
        log.debug("Alert dedup — ma már létezik, érték frissítve: %s", dedup_key)
        return None

    payload = {
        "campaign_id": campaign_id,
        "severity": severity,
        "metric": metric,
        "observed_value": observed_value,
        "threshold_value": threshold_value,
        "message": message,
        "status": "pending",
        "dedup_key": dedup_key,
    }
    res = sb.table(_TABLE).insert(payload).execute()
    row = res.data[0] if res.data else None
    log.info(
        "Új alert: %s (severity=%s, metric=%s, campaign=%s)",
        dedup_key, severity, metric, campaign_id,
    )
    return row


def _refresh_alert_measurement(
    alert_id: int,
    *,
    severity: str,
    observed_value: float | None,
    threshold_value: float | None,
    message: str,
) -> None:
    """A már meglévő napi riasztás felülírása a friss méréssel.

    Csak a MÉRÉST írja felül (érték, küszöb, szöveg, súlyosság, időbélyeg) — a
    kiküldés-státuszt (`status`, `sent_at`, `discord_message_id`) szándékosan
    nem, mert a riasztás naponta egyszer megy ki. Lásd `insert_alert`.

    Nem dob: egy sikertelen frissítés miatt nem eshet ki a monitoring ciklus —
    ilyenkor a korábbi (pontatlanabb) érték marad, ami rosszabb, de nem végzetes.
    """
    try:
        get_supabase().table(_TABLE).update({
            "severity": severity,
            "observed_value": observed_value,
            "threshold_value": threshold_value,
            "message": message,
            "detected_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", alert_id).execute()
    except Exception as exc:  # noqa: BLE001
        log.warning("Alert frissítés sikertelen (id=%s): %s", alert_id, exc)


def recent_insight_metrics(campaign_id: int, *, days: int = 7) -> set[str]:
    """A kampányra az utolsó `days` napban kiküldött INSIGHT metrikák halmaza.

    Az insight-motor 7 napos deduplikációjához: ha egy metrika (pl.
    'scaling_opportunity' vagy 'ai_recommendation') szerepel a halmazban, azt a
    motor kihagyja (ne spammeljen ugyanazzal a javaslattal hetente többször).
    Csak severity='insight' sorokat néz (az anomáliák nem zavarnak be).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    res = (
        get_supabase()
        .table(_TABLE)
        .select("metric")
        .eq("campaign_id", campaign_id)
        .eq("severity", "insight")
        .gt("detected_at", cutoff)
        .execute()
    )
    return {r["metric"] for r in (res.data or []) if r.get("metric")}


def count_metric_today(metric: str) -> int:
    """Ma (UTC nap eleje óta) hány alert keletkezett ezzel a metrikával.

    Az AI insight napi globális limitjéhez (cost control): a scan csak addig hív
    Claude-ot, amíg a `metric='ai_recommendation'` napi darabszám el nem éri a
    küszöböt.
    """
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    res = (
        get_supabase()
        .table(_TABLE)
        .select("id", count="exact")
        .eq("metric", metric)
        .gte("detected_at", start)
        .execute()
    )
    if res.count is not None:
        return res.count
    return len(res.data or [])


def mark_alert_routed(
    alert_id: int,
    *,
    discord_message_id: str | None = None,
    clickup_task_id: str | None = None,
    routed_to_discord_user_id: str | None = None,
) -> None:
    """Az alert megjelölése elküldöttként (a router hívja sikeres kiküldés után).

    Beállítja: status='sent', sent_at=now, és (ha van) a Discord üzenet /
    ClickUp task / címzett azonosítókat.
    """
    payload: dict[str, Any] = {
        "status": "sent",
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    if discord_message_id:
        payload["discord_message_id"] = discord_message_id
    if clickup_task_id:
        payload["clickup_task_id"] = clickup_task_id
    if routed_to_discord_user_id:
        payload["routed_to_discord_user_id"] = routed_to_discord_user_id

    get_supabase().table(_TABLE).update(payload).eq("id", alert_id).execute()


def get_alerts_for_user_in_range(
    user_id: int,
    from_dt: datetime,
    to_dt: datetime,
) -> list[dict[str, Any]]:
    """A userhez rendelt kampányok alertjei egy időablakban (összefoglalóhoz).

    A felhasználó kampányait az assignments alapján oldjuk fel
    (kampány- + ügyfél-szintű), majd az alerts táblát szűrjük:
        campaign_id IN (user kampányai) AND detected_at ∈ [from_dt, to_dt)

    A határok fél-nyitottak: `.gte(from_dt)` INKLUZÍV, `.lt(to_dt)` EXKLUZÍV —
    így két egymást követő ablak sem fed át, és egyetlen alert sem esik ki
    közülük. A napi összefoglalónál ez a teljes előző naptári napot jelenti
    (lásd `monitoring.summary.daily_range`).

    A szűrés KIZÁRÓLAG időalapú: nincs `status` feltétel, tehát a már kiküldött
    (`sent`) és az elnyomott (`suppressed`) alertek is beleszámítanak — az
    összefoglaló az adott nap TELJES riasztás-naplója, nem a "még nyitott
    problémák" listája.

    FIGYELEM: az alerts időbélyege `detected_at` (NEM `created_at`).
    Rendezés: detected_at szerint csökkenő (a severity-prioritást a hívó
    summary réteg végzi, mert az SQL ABC-sorrendje nem szemantikus).
    """
    from src.storage import assignments as assignments_storage

    campaign_ids = assignments_storage.get_campaign_ids_for_user(user_id)
    if not campaign_ids:
        return []

    res = (
        get_supabase()
        .table(_TABLE)
        .select(
            "*, campaigns(name, ad_accounts(id, platform, external_account_id, "
            "account_name, client_id, clients(name)))"
        )
        .in_("campaign_id", campaign_ids)
        .gte("detected_at", from_dt.isoformat())
        .lt("detected_at", to_dt.isoformat())
        .order("detected_at", desc=True)
        .execute()
    )
    return res.data or []


def get_alert_counts_for_campaigns(
    campaign_ids: list[int],
    from_dt: datetime,
    to_dt: datetime,
) -> list[dict[str, Any]]:
    """CRITICAL/WARNING riasztások darabszáma severity+metric bontásban egy ablakban.

    A heti riport ebből ad kontextust a Claude promptnak ("mi ment félre ezen a
    héten"), ezért a nyers sorok helyett csak a bontás kell.

    Az ablak fél-nyitott (`.gte()` / `.lt()`) — ugyanaz a konvenció, mint a
    `get_alerts_for_user_in_range`-nél, és az időbélyeg itt is `detected_at`
    (NEM `created_at`).

    Az `insight` súlyosságú sorokat KIHAGYJA: azok javaslatok, nem problémák —
    a heti riport szempontjából félrevezető lenne riasztásként számolni őket.

    Két szinten kötegel, mint a `weekly_metrics.get_insight_rows_for_campaigns`:
    `in_()` ID-koszorú 200-asával (URL-hossz), kötegen belül `range()` lapozás
    (PostgREST 1000-es sorlimit).

    Visszatérés: [{"severity": "critical", "metric": "cpa_spike", "count": 3}, …]
    darabszám szerint csökkenő sorrendben. Üres lista, ha nem volt riasztás.
    """
    if not campaign_ids:
        return []

    sb = get_supabase()
    tally: dict[tuple[str, str], int] = {}
    ids = list(campaign_ids)

    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        start = 0
        while True:
            rows = (
                sb.table(_TABLE)
                .select("severity, metric, id")
                .in_("campaign_id", chunk)
                .in_("severity", ["critical", "warning"])
                .gte("detected_at", from_dt.isoformat())
                .lt("detected_at", to_dt.isoformat())
                .order("id", desc=False)
                .range(start, start + 999)
                .execute()
                .data
                or []
            )
            for row in rows:
                key = (
                    (row.get("severity") or "?").lower(),
                    row.get("metric") or "?",
                )
                tally[key] = tally.get(key, 0) + 1
            if len(rows) < 1000:
                break
            start += 1000

    return [
        {"severity": severity, "metric": metric, "count": count}
        for (severity, metric), count in sorted(
            tally.items(), key=lambda item: (-item[1], item[0])
        )
    ]


def mark_alert_emailed(alert_id: int) -> None:
    """Az alert ügyfél-email kiküldésének időbélyege (email-dedup, 0007 migration)."""
    get_supabase().table(_TABLE).update(
        {"email_sent_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", alert_id).execute()


def mark_alert_suppressed(alert_id: int) -> None:
    """Az alert megjelölése elnyomottként (csendes időben keletkezett, nem-kritikus).

    A 10. lépés summarizere a 'suppressed' státuszú sorokból állít majd össze
    egy reggeli összefoglalót; addig is jelzi, hogy szándékosan nem küldtük ki.
    """
    get_supabase().table(_TABLE).update({"status": "suppressed"}).eq("id", alert_id).execute()


def get_latest_alert_for_campaigns(campaign_ids: list[int]) -> dict[str, Any] | None:
    """A legutóbbi alert (detected_at szerint) a megadott kampányokra. None ha nincs.

    A `/client status` használja az ügyfél kampányaira ("Utolsó alert").
    """
    if not campaign_ids:
        return None
    res = (
        get_supabase()
        .table(_TABLE)
        .select("*")
        .in_("campaign_id", campaign_ids)
        .order("detected_at", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None
