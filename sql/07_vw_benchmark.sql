-- Purpose: each active NHS trust against its region and England per month for A&E 4-hour (all types and Type 1) and RTT 18-week performance, with regional and national ranks.
-- Note: region_pct and national_pct are weighted (sum of numerators / sum of denominators) over ALL providers reporting that month, so national equals the vw_ae_performance / vw_rtt_performance totals; rows are active NHS trusts only, ranks (1 = best) exclude region 'Unknown', and A&E ranks (all types and Type 1) are given only to trusts with Type 1 A&E (has_type1_ae), so Type 3-only units are not ranked against full emergency departments.
DROP VIEW IF EXISTS reporting.vw_benchmark CASCADE;
CREATE VIEW reporting.vw_benchmark AS
WITH vals AS (
    SELECT 'AE_4HR_PERFORMANCE' AS measure, a.org_code, a.month_date,
           a.within_4hr AS numerator, a.attendances AS denominator
    FROM reporting.vw_ae_performance a
    WHERE a.attendances > 0
    UNION ALL
    SELECT 'AE_4HR_TYPE1' AS measure, a.org_code, a.month_date,
           a.type1_attendances - a.type1_breaches_4hr AS numerator, a.type1_attendances AS denominator
    FROM reporting.vw_ae_performance a
    WHERE a.type1_attendances > 0
    UNION ALL
    SELECT 'RTT_18WK_PERFORMANCE' AS measure, r.org_code, r.month_date,
           r.within_18wk AS numerator, r.waiting_list_size AS denominator
    FROM reporting.vw_rtt_performance r
    WHERE r.waiting_list_size > 0
),
all_providers AS (
    SELECT v.*, d.region_name, d.provider_type, d.is_active, d.has_type1_ae
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
output_rows AS (
    SELECT p.measure, p.org_code, p.month_date, p.region_name, p.numerator, p.denominator,
           100.0 * p.numerator / p.denominator AS trust_pct,
           (p.region_name <> 'Unknown'
            AND (p.measure = 'RTT_18WK_PERFORMANCE' OR p.has_type1_ae)) AS rankable_region,
           (p.measure = 'RTT_18WK_PERFORMANCE' OR p.has_type1_ae)       AS rankable_national
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
    k.trust_pct - g.region_pct   AS vs_region_pp,
    k.trust_pct - n.national_pct AS vs_national_pp,
    CASE WHEN k.rankable_region
         THEN RANK() OVER (PARTITION BY k.measure, k.month_date, k.region_name, k.rankable_region
                           ORDER BY k.trust_pct DESC) END AS rank_in_region,
    CASE WHEN k.rankable_region
         THEN COUNT(*) OVER (PARTITION BY k.measure, k.month_date, k.region_name, k.rankable_region) END AS trusts_in_region,
    CASE WHEN k.rankable_national
         THEN RANK() OVER (PARTITION BY k.measure, k.month_date, k.rankable_national
                           ORDER BY k.trust_pct DESC) END AS rank_national,
    CASE WHEN k.rankable_national
         THEN COUNT(*) OVER (PARTITION BY k.measure, k.month_date, k.rankable_national) END AS trusts_nationally
FROM output_rows k
JOIN national n ON n.measure = k.measure AND n.month_date = k.month_date
JOIN regional g ON g.measure = k.measure AND g.month_date = k.month_date AND g.region_name = k.region_name;
