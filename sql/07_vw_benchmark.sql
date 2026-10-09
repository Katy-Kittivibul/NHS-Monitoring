-- Purpose: each active trust against its region and England per month for A&E 4-hour and RTT 18-week performance, with regional and national ranks.
-- Note: Region and national values are weighted (sum of numerators / sum of denominators) over active trusts only, not averages of trust percentages; rank 1 = best.
DROP VIEW IF EXISTS reporting.vw_benchmark CASCADE;
CREATE VIEW reporting.vw_benchmark AS
WITH active AS (
    SELECT org_code, region_name
    FROM reporting.vw_dim_trust
    WHERE is_active
),
vals AS (
    SELECT 'AE_4HR_PERFORMANCE' AS measure, a.org_code, d.region_name, a.month_date,
           a.within_4hr AS numerator, a.attendances AS denominator
    FROM reporting.vw_ae_performance a
    JOIN active d ON d.org_code = a.org_code
    WHERE a.attendances > 0
    UNION ALL
    SELECT 'RTT_18WK_PERFORMANCE' AS measure, r.org_code, d.region_name, r.month_date,
           r.within_18wk AS numerator, r.waiting_list_size AS denominator
    FROM reporting.vw_rtt_performance r
    JOIN active d ON d.org_code = r.org_code
    WHERE r.waiting_list_size > 0
),
scored AS (
    SELECT v.*,
           100.0 * numerator / denominator AS trust_pct,
           100.0 * SUM(numerator)   OVER (PARTITION BY measure, month_date, region_name)
                 / SUM(denominator) OVER (PARTITION BY measure, month_date, region_name) AS region_pct,
           100.0 * SUM(numerator)   OVER (PARTITION BY measure, month_date)
                 / SUM(denominator) OVER (PARTITION BY measure, month_date)              AS national_pct,
           COUNT(*) OVER (PARTITION BY measure, month_date, region_name) AS trusts_in_region,
           COUNT(*) OVER (PARTITION BY measure, month_date)              AS trusts_nationally
    FROM vals v
)
SELECT
    measure,
    org_code,
    month_date,
    region_name,
    numerator,
    denominator,
    trust_pct,
    region_pct,
    national_pct,
    trust_pct - region_pct   AS vs_region_pp,
    trust_pct - national_pct AS vs_national_pp,
    RANK() OVER (PARTITION BY measure, month_date, region_name ORDER BY trust_pct DESC) AS rank_in_region,
    trusts_in_region,
    RANK() OVER (PARTITION BY measure, month_date ORDER BY trust_pct DESC)              AS rank_national,
    trusts_nationally
FROM scored;
