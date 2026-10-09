-- Purpose: each active NHS trust against its region and England per month for A&E 4-hour and RTT 18-week performance, with regional and national ranks.
-- Note: region_pct and national_pct are weighted (sum of numerators / sum of denominators) over ALL providers reporting that month, so national equals vw_ae_performance / vw_rtt_performance totals; only rows and ranks (1 = best) are limited to active NHS trusts, and region 'Unknown' is not ranked.
DROP VIEW IF EXISTS reporting.vw_benchmark CASCADE;
CREATE VIEW reporting.vw_benchmark AS
WITH vals AS (
    SELECT 'AE_4HR_PERFORMANCE' AS measure, a.org_code, a.month_date,
           a.within_4hr AS numerator, a.attendances AS denominator
    FROM reporting.vw_ae_performance a
    WHERE a.attendances > 0
    UNION ALL
    SELECT 'RTT_18WK_PERFORMANCE' AS measure, r.org_code, r.month_date,
           r.within_18wk AS numerator, r.waiting_list_size AS denominator
    FROM reporting.vw_rtt_performance r
    WHERE r.waiting_list_size > 0
),
all_providers AS (
    SELECT v.*, d.region_name, d.provider_type, d.is_active
    FROM vals v
    JOIN reporting.vw_dim_trust d ON d.org_code = v.org_code
),
national AS (
    SELECT measure, month_date,
           100.0 * SUM(numerator) / SUM(denominator) AS national_pct
    FROM all_providers
    GROUP BY measure, month_date
),
regional AS (
    SELECT measure, month_date, region_name,
           100.0 * SUM(numerator) / SUM(denominator) AS region_pct
    FROM all_providers
    GROUP BY measure, month_date, region_name
),
ranked_rows AS (
    SELECT p.measure, p.org_code, p.month_date, p.region_name, p.numerator, p.denominator,
           100.0 * p.numerator / p.denominator AS trust_pct
    FROM all_providers p
    WHERE p.provider_type = 'NHS trust' AND p.is_active
)
SELECT
    k.measure,
    k.org_code,
    k.month_date,
    k.region_name,
    k.numerator,
    k.denominator,
    k.trust_pct,
    g.region_pct,
    n.national_pct,
    k.trust_pct - g.region_pct AS vs_region_pp,
    k.trust_pct - n.national_pct AS vs_national_pp,
    CASE WHEN k.region_name <> 'Unknown'
         THEN RANK() OVER (PARTITION BY k.measure, k.month_date, k.region_name ORDER BY k.trust_pct DESC) END AS rank_in_region,
    CASE WHEN k.region_name <> 'Unknown'
         THEN COUNT(*) OVER (PARTITION BY k.measure, k.month_date, k.region_name) END AS trusts_in_region,
    RANK() OVER (PARTITION BY k.measure, k.month_date ORDER BY k.trust_pct DESC) AS rank_national,
    COUNT(*) OVER (PARTITION BY k.measure, k.month_date) AS trusts_nationally
FROM ranked_rows k
JOIN national n ON n.measure = k.measure AND n.month_date = k.month_date
JOIN regional g ON g.measure = k.measure AND g.month_date = k.month_date AND g.region_name = k.region_name;
