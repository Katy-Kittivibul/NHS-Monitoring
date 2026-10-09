-- Purpose: trust x month A&E 4-hour performance (all types) against the national target, with 3-month rolling and year-on-year comparison.
-- Sources: Target by financial year, applied to every month of that year (the guidance sets it "by/in March"); the constitutional standard is 95% throughout.
--   FY2023/24 76%: NHS England, "Delivery plan for recovering urgent and emergency care services" (Jan 2023): "76% performance in A&E wait times, measured through the 4-hour target" - https://www.england.nhs.uk/long-read/delivery-plan-for-recovering-urgent-and-emergency-care-services/
--   FY2024/25 78%: NHS England, "2024/25 priorities and operational planning guidance": "a minimum of 78% of patients seen within 4 hours in March 2025" - https://www.england.nhs.uk/long-read/2024-25-priorities-and-operational-planning-guidance/
--   FY2025/26 78%: NHS England, "Urgent and emergency care plan 2025/26": "a minimum of 78% of patients who attend A&E ... admitted, transferred or discharged within 4 hours" - https://www.england.nhs.uk/long-read/urgent-and-emergency-care-plan-2025-26/
--   FY2026/27 82%: NHS England, "Medium Term Planning Framework 2026/27 to 2028/29": "Every trust to maintain or improve to 82% by March 2027" - https://www.england.nhs.uk/long-read/medium-term-planning-framework-delivering-change-together-2026-27-to-2028-29/
DROP VIEW IF EXISTS reporting.vw_ae_performance CASCADE;
CREATE VIEW reporting.vw_ae_performance AS
WITH standards (fy_start_year, target_pct) AS (
    VALUES (2023, 76.0), (2024, 78.0), (2025, 78.0), (2026, 82.0)
),
base AS (
    SELECT org_code,
           month_date,
           total_attendances                          AS attendances,
           total_breaches_4hr                         AS breaches_4hr,
           total_attendances - total_breaches_4hr     AS within_4hr,
           CASE WHEN total_attendances > 0
                THEN 100.0 * (total_attendances - total_breaches_4hr) / total_attendances END AS performance_pct,
           CASE WHEN EXTRACT(MONTH FROM month_date) >= 4
                THEN EXTRACT(YEAR FROM month_date)::int
                ELSE EXTRACT(YEAR FROM month_date)::int - 1 END AS fy_start_year
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
    (r.performance_pct >= s.target_pct)                AS met_target,
    (r.performance_pct >= 95.0)                        AS met_constitutional_standard,
    CASE WHEN r.w_months = 3 AND r.w_attendances > 0
         THEN 100.0 * r.w_within / r.w_attendances END AS rolling_3m_performance_pct,
    r.performance_pct - p.performance_pct              AS yoy_change_pp,
    CASE WHEN p.attendances > 0
         THEN 100.0 * (r.attendances - p.attendances) / p.attendances END AS attendances_yoy_pct
FROM rolling r
LEFT JOIN standards s ON s.fy_start_year = r.fy_start_year
LEFT JOIN base p ON p.org_code = r.org_code
                AND p.month_date = (r.month_date - INTERVAL '1 year')::date;
