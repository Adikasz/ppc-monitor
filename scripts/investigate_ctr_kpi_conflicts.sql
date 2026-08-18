-- =============================================================================
-- READ-ONLY: dupla CTR riasztás + irreális CTR célok felderítése (issue #6)
-- =============================================================================
-- Két, egymástól független kérdést válaszol meg:
--
--   1. Hány kampányon van EGYIDEJŰLEG effektív `min_ctr` ÉS `target_ctr`?
--      Ezeken minden nap KÉT CTR riasztás keletkezik: a `ctr_low` (target_ctr)
--      és a `ctr_drop` (min_ctr). A dedup kulcs `{kampány}_{metrika}_{nap}`,
--      és a két szabály KÜLÖNBÖZŐ metrikanevet ad — ezért nem fogja őket össze.
--
--   2. Hol van irreálisan magas (> 5%) CTR cél? Egy 15%-os cél gyakorlatilag
--      garantálja a napi CRITICAL riasztást, függetlenül a teljesítménytől.
--
-- Csak SELECT-eket tartalmaz. Supabase → SQL Editor, lekérdezésenként kijelölve.
--
-- -----------------------------------------------------------------------------
-- AZ „EFFEKTÍV" KPI FOGALMA — miért nem elég egy tábla
-- -----------------------------------------------------------------------------
-- A detektor nem egy táblából olvassa a KPI-t, hanem MEZŐNKÉNT örököl
-- (detector._merge_kpis):
--
--     campaign_kpis  →  ad_account_kpis  →  client_kpis  →  (nincs)
--
-- Mezőnként külön, ezért a `min_ctr` és a `target_ctr` MÁS-MÁS SZINTRŐL is
-- jöhet: pl. a kampányon `min_ctr`, a kliensen `target_ctr` — a detektor
-- szemében ilyenkor is mindkettő be van állítva, és a kampány dupla riasztást
-- kap. Egyetlen tábla vizsgálata ezt nem mutatná ki, ezért a lekérdezés
-- ugyanazt a coalesce-láncot építi fel, mint a Python kód.
--
-- FIGYELEM: a `client_kpis` táblában NINCS `min_ctr` oszlop — a 0011 migration
-- csak a `campaign_kpis` és az `ad_account_kpis` táblát bővítette. A detektor
-- ezért a kliens szintjén sosem talál `min_ctr`-t (hiányzó kulcs → None), és a
-- lekérdezés is ugyanígy, kétszintű láncot használ rá.
--
-- Kampány-szűrés: ugyanaz, mint `campaigns_storage.get_active_campaigns`
-- (is_monitored = true ÉS lifecycle_state NOT IN ('paused','ended')) — a
-- detektor csak ezeket értékeli ki, a többi kampány KPI-ja nem okoz riasztást.
-- =============================================================================


-- =============================================================================
-- 1. HÁNY KAMPÁNYON VAN EGYSZERRE min_ctr ÉS target_ctr? — összesítés
-- =============================================================================
with active_campaigns as (
    select c.id, c.name, c.ad_account_id
    from campaigns c
    where c.is_monitored is true
      and coalesce(c.lifecycle_state, 'new') not in ('paused', 'ended')
),
ck as (
    -- campaign_kpis verziózott: kampányonként a LEGFRISSEBB aktív sor számít
    -- (kpis_storage.get_active_kpis: is_active=true, created_at desc, limit 1).
    select distinct on (campaign_id)
        campaign_id, target_ctr, min_ctr
    from campaign_kpis
    where is_active is true
    order by campaign_id, created_at desc
),
ak as (
    select ad_account_id, target_ctr, min_ctr
    from ad_account_kpis
    where is_active is true
),
clk as (
    select client_id, target_ctr
    from client_kpis
    where is_active is true
),
eff as (
    select
        ac.id,
        ac.name,
        coalesce(ck.target_ctr, ak.target_ctr, clk.target_ctr) as target_ctr,
        coalesce(ck.min_ctr,    ak.min_ctr)                    as min_ctr
    from active_campaigns ac
    left join ad_accounts aa on aa.id = ac.ad_account_id
    left join ck  on ck.campaign_id   = ac.id
    left join ak  on ak.ad_account_id = ac.ad_account_id
    left join clk on clk.client_id    = aa.client_id
)
select
    count(*)                                                       as aktiv_kampany,
    count(*) filter (where target_ctr is not null
                       and min_ctr    is not null)                 as mindketto_beallitva,
    count(*) filter (where target_ctr is not null
                       and min_ctr    is null)                     as csak_target_ctr,
    count(*) filter (where target_ctr is null
                       and min_ctr    is not null)                 as csak_min_ctr,
    count(*) filter (where target_ctr is null
                       and min_ctr    is null)                     as egyik_sem,
    round(
        100.0 * count(*) filter (where target_ctr is not null and min_ctr is not null)
        / nullif(count(*), 0)
    , 1)                                                           as mindketto_szazalek
from eff;


-- =============================================================================
-- 2. AZ ÉRINTETT KAMPÁNYOK TÉTELESEN — és MELYIK SZINTRŐL jön a két érték
-- =============================================================================
-- A `honnan_*` oszlopok döntik el, mi a helyes javítás:
--   - ha mindkettő 'kampány' → kézzel, kampányonként állították be mindkettőt
--   - ha az egyik 'fiók'/'kliens' → egy magasabb szintű beállítás csorog le
--     sok kampányra; ilyenkor EGY helyen javítva sok kampány rendbe jön
with active_campaigns as (
    select c.id, c.name, c.ad_account_id
    from campaigns c
    where c.is_monitored is true
      and coalesce(c.lifecycle_state, 'new') not in ('paused', 'ended')
),
ck as (
    select distinct on (campaign_id) campaign_id, target_ctr, min_ctr
    from campaign_kpis
    where is_active is true
    order by campaign_id, created_at desc
),
ak as (
    select ad_account_id, target_ctr, min_ctr
    from ad_account_kpis where is_active is true
),
clk as (
    select client_id, target_ctr
    from client_kpis where is_active is true
),
eff as (
    select
        ac.id, ac.name,
        cl.name  as ugyfel,
        aa.platform,
        aa.account_name,
        coalesce(ck.target_ctr, ak.target_ctr, clk.target_ctr) as target_ctr,
        coalesce(ck.min_ctr,    ak.min_ctr)                    as min_ctr,
        case
            when ck.target_ctr  is not null then 'kampány'
            when ak.target_ctr  is not null then 'fiók'
            when clk.target_ctr is not null then 'kliens'
        end                                                    as honnan_target_ctr,
        case
            when ck.min_ctr is not null then 'kampány'
            when ak.min_ctr is not null then 'fiók'
        end                                                    as honnan_min_ctr
    from active_campaigns ac
    left join ad_accounts aa on aa.id = ac.ad_account_id
    left join clients     cl on cl.id = aa.client_id
    left join ck  on ck.campaign_id   = ac.id
    left join ak  on ak.ad_account_id = ac.ad_account_id
    left join clk on clk.client_id    = aa.client_id
)
select
    id as kampany_id, name as kampany, ugyfel, platform, account_name,
    target_ctr, honnan_target_ctr,
    min_ctr,    honnan_min_ctr
from eff
where target_ctr is not null
  and min_ctr    is not null
order by honnan_target_ctr, honnan_min_ctr, ugyfel, name
limit 200;


-- =============================================================================
-- 3. IRREÁLISAN MAGAS CTR CÉL (> 5%)
-- =============================================================================
-- Meta/Google kampányon a tipikus CTR 0,5–2%. Az 5% feletti cél szinte biztosan
-- elgépelés vagy mértékegység-tévesztés (pl. 0,15 helyett 15 lett beírva).
--
-- A `szint` oszlop mutatja, hol kell javítani. Egy fiók- vagy kliens-szintű
-- rossz érték egyetlen javítással sok kampányt rendbe tesz — ezt a
-- `hany_kampanyt_erint` számolja.
select 'kampány' as szint,
       ck.campaign_id::text                as entitas_id,
       c.name                              as entitas,
       ck.target_ctr,
       1                                   as hany_kampanyt_erint
from (
    select distinct on (campaign_id) campaign_id, target_ctr
    from campaign_kpis where is_active is true
    order by campaign_id, created_at desc
) ck
join campaigns c on c.id = ck.campaign_id
where ck.target_ctr > 5
  and c.is_monitored is true
  and coalesce(c.lifecycle_state, 'new') not in ('paused', 'ended')

union all

select 'fiók',
       ak.ad_account_id::text,
       coalesce(aa.account_name, aa.external_account_id),
       ak.target_ctr,
       (select count(*) from campaigns c2
         where c2.ad_account_id = ak.ad_account_id
           and c2.is_monitored is true
           and coalesce(c2.lifecycle_state, 'new') not in ('paused', 'ended'))
from ad_account_kpis ak
join ad_accounts aa on aa.id = ak.ad_account_id
where ak.is_active is true and ak.target_ctr > 5

union all

select 'kliens',
       clk.client_id::text,
       cl.name,
       clk.target_ctr,
       (select count(*) from campaigns c3
         join ad_accounts aa3 on aa3.id = c3.ad_account_id
        where aa3.client_id = clk.client_id
          and c3.is_monitored is true
          and coalesce(c3.lifecycle_state, 'new') not in ('paused', 'ended'))
from client_kpis clk
join clients cl on cl.id = clk.client_id
where clk.is_active is true and clk.target_ctr > 5

order by target_ctr desc, hany_kampanyt_erint desc;


-- =============================================================================
-- 4. KONTROLL: ténylegesen keletkezett-e dupla CTR riasztás?
-- =============================================================================
-- Az 1–2. lekérdezés a KONFIGURÁCIÓT nézi ("elvileg dupla riasztást kapna").
-- Ez a TÉNYT: hány kampány kapott ugyanazon a napon `ctr_low` ÉS `ctr_drop`
-- riasztást is. A napot a dedup-kulcs utolsó 10 karakteréből vesszük (ISO
-- dátum) — pontosan az a nap, ami szerint az `insert_alert` deduplikált,
-- időzóna-találgatás nélkül.
--
-- Megjegyzés: az `alerts` tábla nem évül el, tehát itt nem korlátoz a
-- `campaign_insights` 7 napos retenciója — az ablak szabadon tágítható.
select
    right(a.dedup_key, 10)::date              as nap,
    count(distinct a.campaign_id)             as kampany_ket_ctr_riasztassal,
    count(*)                                  as ctr_riasztas_osszesen
from alerts a
where a.metric in ('ctr_low', 'ctr_drop')
  and a.dedup_key is not null
  and a.detected_at >= now() - interval '30 days'
  and exists (
      select 1 from alerts b
      where b.campaign_id = a.campaign_id
        and b.metric      = case a.metric when 'ctr_low' then 'ctr_drop' else 'ctr_low' end
        and right(b.dedup_key, 10) = right(a.dedup_key, 10)
  )
group by 1
order by 1 desc;
