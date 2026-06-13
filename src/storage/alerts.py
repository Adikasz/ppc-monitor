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

from datetime import date

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
) -> bool:
    """Egy riasztás beszúrása az alerts táblába, deduplikációval.

    Deduplikáció:
        A `dedup_key = "{campaign_id}_{metric}_{ma}"` kulcsra naponta egyetlen
        riasztás kerül be. Ha a mai napra ezzel a kulccsal MÁR létezik sor
        (bármilyen státuszban), nem szúrunk be újat.

        Megjegyzés: a specifikáció a 'pending' státuszú duplikátumot említi, de
        a kulcs eleve tartalmazza a dátumot, ezért bármely státuszú találatra
        skippelünk — különben az óránkénti ciklus a már 'sent' riasztást is
        újraküldené ugyanaznap. (Cél: "ne duplikálj".)

    Paraméterek:
        campaign_id     — érintett kampány DB ID-ja
        severity        — 'critical' | 'warning' (a séma 'insight'-ot is enged)
        metric          — mi váltotta ki (pl. 'cpa_spike', 'budget_depleted')
        observed_value  — mért érték (lehet None)
        threshold_value — küszöbérték (lehet None)
        message         — emberi olvasható üzenet

    Visszatérés:
        True  — új riasztás beszúrva
        False — már létezett ma ugyanerre (dedup), nem szúrtunk be
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
        log.debug("Alert dedup — ma már létezik: %s", dedup_key)
        return False

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
    sb.table(_TABLE).insert(payload).execute()
    log.info(
        "Új alert: %s (severity=%s, metric=%s, campaign=%s)",
        dedup_key, severity, metric, campaign_id,
    )
    return True
