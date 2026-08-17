-- =============================================================================
-- READ-ONLY: a `_MIN_IMPRESSIONS_FOR_RATIOS` küszöb visszamenőleges hatása
-- =============================================================================
-- Cél: megválaszolni, hogy a detektorba bevezetett 100-as megjelenés-küszöb
-- (src/monitoring/detector.py) visszamenőleg hány riasztást nyomott volna el,
-- és hogy a küszöb a JÓ HELYEN van-e.
--
-- Ez a fájl KIZÁRÓLAG SELECT-eket tartalmaz. Nem hoz létre view-t, temp táblát
-- vagy indexet, nem ír és nem töröl. Nyugodtan futtatható éles adatbázison
-- (Supabase → SQL Editor), a lekérdezéseket egyesével kijelölve.
--
-- -----------------------------------------------------------------------------
-- HOGYAN PÁROSÍTJUK A RIASZTÁST A MEGJELENÉS-SZÁMMAL
-- -----------------------------------------------------------------------------
-- Az `alerts` tábla NEM tárolja a megjelenés-számot, ezért a `campaign_insights`
-- órás snapshotból vesszük. A párosítás pontos, nem közelítés:
--
--   scheduler.hourly_monitoring() egyetlen ciklusiterációban, ebben a sorrendben:
--     1. insert_campaign_insight(cid, insight)   → campaign_insights sor
--     2. detect_anomalies_for_campaign(cid, insight)
--     3. insert_alert(...)                       → alerts sor (detected_at = now())
--
--   Vagyis a riasztás előtti LEGKÖZELEBBI insight-sor pontosan az a snapshot,
--   amit a detektor látott — ezt olvassa ki a LATERAL join.
--
-- Két eset, ahol a párosítás mégis hiányos lehet (a lekérdezés külön kimutatja,
-- ne értelmezd „0 megjelenés"-ként):
--   a) az órás unique index (campaign_id, óra) miatt a 2. ciklus insertje
--      kimarad, így a snapshot az óra elejéről való;
--   b) az insight insert hibára futott, de a detektálás lefutott.
--
-- -----------------------------------------------------------------------------
-- ⚠ KORLÁT: X LEGFELJEBB 7 NAP LEHET
-- -----------------------------------------------------------------------------
-- A `campaign_insights` tábla 7 napos retencióval fut: minden órás ciklus végén
-- `prune_old_insights(7)` törli az ennél régebbi sorokat
-- (src/monitoring/scheduler.py:217). A 7 napnál régebbi riasztásokhoz tehát MÁR
-- NINCS megjelenés-adat — azok „nincs párosított snapshot"-ként jelennének meg.
--
-- Ezért a `napok` paramétert 6-ra állítottam (nem 7-re): a prune a határon lévő
-- órákat már ritkíthatta, a 6 nap biztosan teljes. Feljebb ne vidd.
-- =============================================================================


-- =============================================================================
-- 0. SANITY CHECK — van-e egyáltalán értékelhető adat az ablakban?
-- =============================================================================
-- Futtasd ELŐSZÖR. Ha az 1–4. lekérdezés nullákat ad, itt látszik, hogy azért,
-- mert nincs riasztás, vagy azért, mert a retenció már elvitte az insightokat.
select
    (select count(*) from alerts
      where detected_at >= now() - interval '6 days')          as riasztas_6_nap,
    (select min(detected_at) from alerts)                      as legregebbi_riasztas,
    (select count(*) from campaign_insights)                   as insight_sorok,
    (select min(fetched_at) from campaign_insights)            as legregebbi_insight,
    (select max(fetched_at) from campaign_insights)            as legujabb_insight;


-- =============================================================================
-- 1. FŐ EREDMÉNY — hány arány-alapú riasztást nyomott volna el a küszöb?
-- =============================================================================
with params as (
    select 6::int as napok, 100::int as kuszob
),
gated as (
    -- Pontosan azok a metrikák, amiket a `has_enough_data` kapu véd
    -- (detector.py 3. és 4. szekció + a 6. szekció három arány-szabálya).
    -- NINCS benne: ads_stopped, no_conversion, budget_*, impressions_drop.
    select unnest(array[
        'roas_drop', 'ctr_low', 'roi_low',
        'cpa_spike', 'cpl_high', 'cpc_high',
        'ctr_drop', 'cpm_spike', 'frequency_spike'
    ]) as metric
),
scoped as (
    -- A `napok` / `kuszob` értéket OSZLOPKÉNT visszük tovább, és lent GROUP BY-ban
    -- csoportosítunk rájuk. Így a küszöb egyetlen helyen (a `params` CTE-ben)
    -- állítható, és nem kell al-lekérdezés az aggregátumok FILTER-ébe.
    select a.id, a.campaign_id, a.metric, a.severity, a.observed_value, a.detected_at,
           p.napok, p.kuszob
    from alerts a
    join gated g on g.metric = a.metric
    cross join params p
    where a.detected_at >= now() - make_interval(days => p.napok)
),
paired as (
    select s.*, i.impressions
    from scoped s
    left join lateral (
        select ci.impressions
        from campaign_insights ci
        where ci.campaign_id = s.campaign_id
          and ci.fetched_at <= s.detected_at
          and ci.fetched_at >  s.detected_at - interval '2 hours'
        order by ci.fetched_at desc
        limit 1
    ) i on true
)
select
    napok                                                             as ablak_nap,
    kuszob,
    count(*)                                                          as arany_alapu_riasztas,
    count(*) filter (where impressions is null)                       as nincs_parositott_snapshot,
    count(*) filter (where impressions is not null
                       and impressions <  kuszob)                     as elnyomta_volna,
    count(*) filter (where impressions is not null
                       and impressions >= kuszob)                     as megmaradt_volna,
    round(
        100.0 * count(*) filter (where impressions is not null and impressions < kuszob)
        / nullif(count(*) filter (where impressions is not null), 0)
    , 1)                                                              as elnyomott_szazalek
from paired
group by napok, kuszob;


-- =============================================================================
-- 2. BONTÁS metrika és súlyosság szerint
-- =============================================================================
-- Ez mutatja meg, melyik szabály termelte a hamis riasztások zömét. Ha egyetlen
-- metrika dominál, lehet hogy nem a globális küszöb, hanem az a szabály a hibás.
with params as (
    select 6::int as napok, 100::int as kuszob
),
gated as (
    select unnest(array[
        'roas_drop', 'ctr_low', 'roi_low',
        'cpa_spike', 'cpl_high', 'cpc_high',
        'ctr_drop', 'cpm_spike', 'frequency_spike'
    ]) as metric
),
paired as (
    select a.id, a.metric, a.severity, a.detected_at, i.impressions
    from alerts a
    join gated g on g.metric = a.metric
    cross join params p
    left join lateral (
        select ci.impressions
        from campaign_insights ci
        where ci.campaign_id = a.campaign_id
          and ci.fetched_at <= a.detected_at
          and ci.fetched_at >  a.detected_at - interval '2 hours'
        order by ci.fetched_at desc
        limit 1
    ) i on true
    where a.detected_at >= now() - make_interval(days => p.napok)
)
select
    metric,
    severity,
    count(*)                                                       as osszes,
    count(*) filter (where impressions is not null
                       and impressions < 100)                      as elnyomta_volna,
    round(
        100.0 * count(*) filter (where impressions is not null and impressions < 100)
        / nullif(count(*) filter (where impressions is not null), 0)
    , 1)                                                           as elnyomott_szazalek,
    count(*) filter (where impressions is null)                    as nincs_snapshot
from paired
group by metric, severity
order by elnyomta_volna desc, metric, severity;


-- =============================================================================
-- 3. A KÜSZÖB HELYE — megjelenés-eloszlás a riasztás pillanatában
-- =============================================================================
-- EZ a lekérdezés validálja magát a 100-as számot. Ha a sávok között éles
-- szakadék van (pl. tömeg a 0–9 sávban, majd szinte semmi 10–499 között), akkor
-- a küszöb pontos értéke lényegtelen. Ha viszont a 100 körüli sávok is sűrűn
-- laknak, a küszöb valódi riasztásokat is levág — ilyenkor érdemes lejjebb
-- (pl. 50) vagy metrikánként külön értékre állítani.
--
-- A `nulla_ertek` oszlop a hajnali „0.00" jelenséget méri (issue #5): ahol az
-- observed_value 0, ott szinte biztosan adathiány volt, nem teljesítmény-gond.
with params as (
    select 6::int as napok
),
gated as (
    select unnest(array[
        'roas_drop', 'ctr_low', 'roi_low',
        'cpa_spike', 'cpl_high', 'cpc_high',
        'ctr_drop', 'cpm_spike', 'frequency_spike'
    ]) as metric
),
paired as (
    select a.observed_value, i.impressions
    from alerts a
    join gated g on g.metric = a.metric
    cross join params p
    left join lateral (
        select ci.impressions
        from campaign_insights ci
        where ci.campaign_id = a.campaign_id
          and ci.fetched_at <= a.detected_at
          and ci.fetched_at >  a.detected_at - interval '2 hours'
        order by ci.fetched_at desc
        limit 1
    ) i on true
    where a.detected_at >= now() - make_interval(days => p.napok)
),
bucketed as (
    select
        case
            when impressions is null       then 'z) nincs snapshot'
            when impressions = 0           then 'a) 0'
            when impressions < 10          then 'b) 1-9'
            when impressions < 50          then 'c) 10-49'
            when impressions < 100         then 'd) 50-99      <- a küszöb alatti utolsó sáv'
            when impressions < 250         then 'e) 100-249    <- a küszöb feletti első sáv'
            when impressions < 1000        then 'f) 250-999'
            else                                'g) 1000+'
        end as sav,
        observed_value
    from paired
)
select
    sav,
    count(*)                                              as riasztas,
    count(*) filter (where observed_value = 0)            as ebbol_nulla_ertek,
    round(100.0 * count(*) / sum(count(*)) over (), 1)    as szazalek
from bucketed
group by sav
order by sav;


-- =============================================================================
-- 4. VOLT-E KÁR? — az elnyomandó riasztások félrevezetőek voltak-e
-- =============================================================================
-- A 3. lekérdezés azt mutatja, hány riasztást vág le a küszöb. Ez azt, hogy
-- JOGGAL-e: összeveti a riasztáskor rögzített értéket a NAP VÉGI (utolsó
-- snapshot) értékkel ugyanarra a kampányra.
--
-- Ha a `nap_vegi_ertek` egészséges, miközben a `riasztas_erteke` 0.00 volt,
-- akkor a riasztás adathiány-műtermék volt — és a napi dedup
-- (kampány_metrika_nap) miatt EGÉSZ NAPRA elnyomta a valódi riasztást is.
--
-- Csak azokat a metrikákat tudja összevetni, amiknek van oszlopa a
-- campaign_insights táblában (roas, ctr, cpa, cpc). A roi_low / cpl_high /
-- cpm_spike / frequency_spike sorokban a nap_vegi_ertek NULL — ez nem hiba.
with params as (
    select 6::int as napok, 100::int as kuszob
),
gated as (
    select unnest(array[
        'roas_drop', 'ctr_low', 'roi_low',
        'cpa_spike', 'cpl_high', 'cpc_high',
        'ctr_drop', 'cpm_spike', 'frequency_spike'
    ]) as metric
),
paired as (
    select
        a.id, a.campaign_id, a.metric, a.severity, a.observed_value, a.detected_at,
        -- A dedup-kulcs UTOLSÓ 10 karaktere maga a nap (ISO dátum). Ezt
        -- használjuk, nem a detected_at-ot: így pontosan az a nap, ami szerint
        -- az `insert_alert` deduplikált — időzóna-találgatás nélkül.
        right(a.dedup_key, 10)::date as dedup_nap,
        i.impressions
    from alerts a
    join gated g on g.metric = a.metric
    cross join params p
    left join lateral (
        select ci.impressions
        from campaign_insights ci
        where ci.campaign_id = a.campaign_id
          and ci.fetched_at <= a.detected_at
          and ci.fetched_at >  a.detected_at - interval '2 hours'
        order by ci.fetched_at desc
        limit 1
    ) i on true
    where a.detected_at >= now() - make_interval(days => p.napok)
      and a.dedup_key is not null
),
elnyomando as (
    select * from paired
    where impressions is not null
      and impressions < (select kuszob from params)
),
nap_vege as (
    -- Kampányonként és naponta az UTOLSÓ snapshot (= a nap végi állapot).
    select distinct on (ci.campaign_id, (ci.fetched_at at time zone 'UTC')::date)
        ci.campaign_id,
        (ci.fetched_at at time zone 'UTC')::date as nap,
        ci.impressions as nap_vegi_impressions,
        ci.ctr, ci.roas, ci.cpa, ci.cpc
    from campaign_insights ci
    order by ci.campaign_id,
             (ci.fetched_at at time zone 'UTC')::date,
             ci.fetched_at desc
)
select
    c.name                                    as kampany,
    e.metric,
    e.severity,
    e.detected_at,
    e.impressions                             as megjelenes_riasztaskor,
    round(e.observed_value, 2)                as riasztas_erteke,
    n.nap_vegi_impressions,
    round(
        case e.metric
            when 'roas_drop' then n.roas
            when 'ctr_low'   then n.ctr * 100      -- observed_value %-ban, a DB arányban tárol
            when 'ctr_drop'  then n.ctr * 100
            when 'cpa_spike' then n.cpa
            when 'cpc_high'  then n.cpc
        end
    , 2)                                      as nap_vegi_ertek
from elnyomando e
join campaigns c on c.id = e.campaign_id
left join nap_vege n
       on n.campaign_id = e.campaign_id
      and n.nap        = e.dedup_nap
order by e.observed_value nulls last, e.detected_at desc
limit 100;
