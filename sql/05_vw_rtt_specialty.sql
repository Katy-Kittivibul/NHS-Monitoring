-- Purpose: trust x month x treatment function RTT performance from specialty rows only (C_999 total excluded), for specialty drill-down.
-- Grain: one row per org_code x month_date x specialty_code.
-- Note: Never add this view to vw_rtt_performance: both describe the same pathways.
DROP VIEW IF EXISTS reporting.vw_rtt_specialty CASCADE;
CREATE VIEW reporting.vw_rtt_specialty AS
WITH base AS (
    SELECT org_code,
           specialty_code,
           specialty_name,
           month_date,
           waiting_list_size,
           waiting_list_size - over_18wk_count AS within_18wk,
           over_18wk_count                     AS over_18wk,
           over_52wk_count                     AS over_52wk,
           CASE WHEN waiting_list_size > 0
                THEN 100.0 * (waiting_list_size - over_18wk_count) / waiting_list_size END AS pct_within_18wk
    FROM reporting.fact_rtt_waits
    WHERE specialty_code <> 'C_999'
),
rolling AS (
    SELECT b.*,
           SUM(within_18wk)       OVER w AS w_within,
           SUM(waiting_list_size) OVER w AS w_list,
           COUNT(*)               OVER w AS w_months
    FROM base b
    WINDOW w AS (PARTITION BY org_code, specialty_code ORDER BY month_date
                 RANGE BETWEEN INTERVAL '2 months' PRECEDING AND CURRENT ROW)
)
SELECT
    r.org_code,
    r.specialty_code,
    r.specialty_name,
    r.month_date,
    r.waiting_list_size,
    r.within_18wk,
    r.over_18wk,
    r.pct_within_18wk,
    92.0                                               AS standard_pct,
    (r.pct_within_18wk >= 92.0)                        AS met_standard,
    r.over_52wk                                        AS waiters_52wk_plus,
    CASE WHEN r.w_months = 3 AND r.w_list > 0
         THEN 100.0 * r.w_within / r.w_list END        AS rolling_3m_pct_within_18wk,
    r.pct_within_18wk - p.pct_within_18wk              AS yoy_change_pp,
    CASE WHEN p.waiting_list_size > 0
         THEN 100.0 * (r.waiting_list_size - p.waiting_list_size) / p.waiting_list_size END AS waiting_list_yoy_pct,
    CASE WHEN p.over_52wk > 0
         THEN 100.0 * (r.over_52wk - p.over_52wk) / p.over_52wk END AS waiters_52wk_yoy_pct
FROM rolling r
LEFT JOIN base p ON p.org_code = r.org_code
                AND p.specialty_code = r.specialty_code
                AND p.month_date = (r.month_date - INTERVAL '1 year')::date;
