-- Purpose: primary-care context for each ICB and month: GP prescribing volume and cost (rolled up from practice level through the Sub ICB Location bridge) beside NHS dental activity delivered against contract targets, so Power BI can sit them next to A&E and RTT without touching the 80k+ row facts.
-- Grain: one row per icb_code x month_date. The two sources cover different months (prescribing currently ends before dental), so either block can be NULL for a month; neither is zero-filled.
-- Note: prescribing is whatever BNF section(s) fact_prescribing holds (currently 0205 only), GP-practice setting. pct_uda_achieved = delivered / target over the ICB's contracts that month. Dental ICB codes that are not in dim_sub_icb keep the name NHSBSA supplied.
DROP VIEW IF EXISTS reporting.vw_primary_care_icb CASCADE;
CREATE VIEW reporting.vw_primary_care_icb AS
WITH rx AS (
    SELECT s.icb_code,
           MAX(s.icb_name)                 AS icb_name,
           p.month_date,
           SUM(p.items)                    AS prescribing_items,
           SUM(p.actual_cost)              AS prescribing_cost,
           COUNT(DISTINCT p.practice_code) AS prescribing_practices
    FROM reporting.fact_prescribing p
    JOIN reporting.dim_sub_icb s ON s.sub_icb_code = p.sub_icb_code
    GROUP BY s.icb_code, p.month_date
),
dental AS (
    SELECT icb_code,
           MAX(icb_name)                   AS icb_name,
           month_date,
           COUNT(DISTINCT contract_number) AS dental_contracts,
           SUM(uda_target)                 AS uda_target,
           SUM(uda_delivered)              AS uda_delivered,
           SUM(band_1_delivered)           AS band_1_delivered,
           SUM(band_2_delivered)           AS band_2_delivered,
           SUM(band_3_delivered)           AS band_3_delivered,
           SUM(band_urgent_delivered)      AS band_urgent_delivered
    FROM reporting.fact_dental_uda
    GROUP BY icb_code, month_date
)
SELECT
    COALESCE(rx.icb_code, d.icb_code)     AS icb_code,
    COALESCE(rx.icb_name, d.icb_name)     AS icb_name,
    COALESCE(rx.month_date, d.month_date) AS month_date,
    rx.prescribing_items,
    rx.prescribing_cost,
    rx.prescribing_practices,
    CASE WHEN rx.prescribing_practices > 0
         THEN rx.prescribing_items::double precision / rx.prescribing_practices END AS items_per_practice,
    d.dental_contracts,
    d.uda_target,
    d.uda_delivered,
    CASE WHEN d.uda_target > 0 THEN 100.0 * d.uda_delivered / d.uda_target END      AS pct_uda_achieved,
    d.band_1_delivered,
    d.band_2_delivered,
    d.band_3_delivered,
    d.band_urgent_delivered
FROM rx
FULL OUTER JOIN dental d ON d.icb_code = rx.icb_code AND d.month_date = rx.month_date;
