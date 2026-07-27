-- ============================================================
-- 0002_default_alert_rules.sql
-- Alapértelmezett (globális) anomália-küszöbök
-- ============================================================
-- Ezek a kiindulási értékek. A CRITICAL/WARNING pontos küszöbei
-- még egyeztetés alatt vannak Campaign Monitor-szel — ezek ésszerű kezdőértékek,
-- amiket Discord paranccsal bármikor felül lehet írni.
--
-- Az öröklődés: global < campaign_type < campaign (a legspecifikusabb nyer).
-- ============================================================

-- CPA-ugrás: a CPA hány %-kal haladja meg a max_cpa célt
--   WARNING:  +25% felett
--   CRITICAL: +50% felett
insert into alert_rules (scope, metric, warning_threshold, critical_threshold, direction)
values ('global', 'cpa_spike', 25, 50, 'above');

-- ROAS-esés: a ROAS hány %-kal van a cél ALATT
--   WARNING:  -20% alatt
--   CRITICAL: -40% alatt
insert into alert_rules (scope, metric, warning_threshold, critical_threshold, direction)
values ('global', 'roas_drop', 20, 40, 'below');

-- Büdzsé-elfogyás: a havi büdzsé hány %-a fogyott el a hónap arányához képest
--   WARNING:  90% felett
--   CRITICAL: 100% felett (túlköltés)
insert into alert_rules (scope, metric, warning_threshold, critical_threshold, direction)
values ('global', 'budget_depleted', 90, 100, 'above');

-- CTR-alacsony: a CTR hány %-kal van a cél alatt
--   WARNING:  -30% alatt
--   CRITICAL: -50% alatt
insert into alert_rules (scope, metric, warning_threshold, critical_threshold, direction)
values ('global', 'ctr_low', 30, 50, 'below');

-- CPC-magas: a CPC hány %-kal haladja meg a max_cpc célt
--   WARNING:  +30% felett
--   CRITICAL: +60% felett
insert into alert_rules (scope, metric, warning_threshold, critical_threshold, direction)
values ('global', 'cpc_high', 30, 60, 'above');

-- ============================================================
-- Fiókszintű / műszaki anomáliák — ezek mindig CRITICAL-ek,
-- nincs fokozat (vagy van baj, vagy nincs).
-- A küszöb itt nem érték, hanem logikai feltétel a kódban;
-- a sor azért van, hogy a metrika regisztrálva legyen és
-- a súlyosság konfigurálható maradjon.
-- ============================================================

insert into alert_rules (scope, metric, critical_threshold, direction)
values ('global', 'account_disabled', 1, 'above');

insert into alert_rules (scope, metric, critical_threshold, direction)
values ('global', 'payment_error', 1, 'above');

-- Hirdetés-elutasítás: hány elutasított hirdetés felett CRITICAL
insert into alert_rules (scope, metric, warning_threshold, critical_threshold, direction)
values ('global', 'ad_disapproved', 1, 3, 'above');

-- Tanulási fázis befagyás (Meta): hány nap LIMITED állapot felett
insert into alert_rules (scope, metric, warning_threshold, critical_threshold, direction)
values ('global', 'learning_frozen', 5, 7, 'above');
