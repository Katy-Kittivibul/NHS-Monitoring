-- Purpose: trust x month A&E 4-hour performance (all types headline, Type 1 reported separately) against the national target (reporting.vw_ae_targets), with 3-month rolling and year-on-year comparison, plus admissions and the breaches above the target allowance.
-- Grain: one row per org_code x month_date.
-- Note: excess_breaches_vs_target = breaches above the number the target allows (never negative; 0 when the trust met the target). admission_conversion_pct = admissions via A&E as a share of all attendances.
DROP VIEW IF EXISTS reporting.vw_ae_performance CASCADE;
CREATE VIEW reporting.vw_ae_performance AS
WITH base AS (
    SELECT org_code,
           month_date,
           total_attendances                          AS attendances,
           total_breaches_4hr                         AS breaches_4hr,
           total_attendances - total_breaches_4hr     AS within_4hr,
           admissions_via_ae                          AS admissions,
           attendances_type1                          AS type1_attendances,
           breaches_4hr_type1                         AS type1_breaches_4hr,
           CASE WHEN attendances_type1 > 0
                THEN 100.0 * (attendances_type1 - breaches_4hr_type1) / attendances_type1 END AS type1_performance_pct,
           CASE WHEN total_attendances > 0
                THEN 100.0 * (total_attendances - total_breaches_4hr) / total_attendances END AS performance_pct
    FROM reporting.fact_ae_attendances
),
rolling AS (
    SELECT b.*,
           SUM(within_4hr)  OVER w AS w_within,
           SUM(attendances) OVER w AS w_attendances,
           COUNT(*)         OVER w AS w_months
    FROM base b
    WINDOW w AS (PARTITION BY org_code ORDER BY month_date
                 RANGE BETWEEN INTERVAL '2 months' PRECEDING AND CURRENT ROW)
)
SELECT
    r.org_code,
    r.month_date,
    r.attendances,
    r.breaches_4hr,
    r.within_4hr,
    r.performance_pct,
    s.target_pct                                       AS national_target_pct,
    95.0                                               AS constitutional_standard_pct,
    GREATEST(r.breaches_4hr - r.attendances * (1 - s.target_pct / 100.0), 0) AS excess_breaches_vs_target,
    r.admissions,
    CASE WHEN r.attendances > 0
         THEN 100.0 * r.admissions / r.attendances END AS admission_conversion_pct,
    (r.performance_pct >= s.target_pct)                AS met_target,
    (r.performance_pct >= 95.0)                        AS met_constitutional_standard,
    CASE WHEN r.w_months = 3 AND r.w_attendances > 0
         THEN 100.0 * r.w_within / r.w_attendances END AS rolling_3m_performance_pct,
    r.performance_pct - p.performance_pct              AS yoy_change_pp,
    CASE WHEN p.attendances > 0
         THEN 100.0 * (r.attendances - p.attendances) / p.attendances END AS attendances_yoy_pct,
    r.type1_attendances,
    r.type1_breaches_4hr,
    r.type1_performance_pct
FROM rolling r
LEFT JOIN reporting.vw_ae_targets s ON r.month_date BETWEEN s.valid_from AND s.valid_to
LEFT JOIN base p ON p.org_code = r.org_code
                AND p.month_date = (r.month_date - INTERVAL '1 year')::date;
