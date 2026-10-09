-- Purpose: month-grain date dimension (April-start financial year) spanning the loaded data, for Power BI time intelligence.
-- Note: Named vw_dim_date because reporting.dim_date is already the raw table copied by reporting/load_postgres.py.
DROP VIEW IF EXISTS reporting.vw_dim_date CASCADE;
CREATE VIEW reporting.vw_dim_date AS
WITH data_range AS (
    SELECT MIN(month_date) AS first_month, MAX(month_date) AS last_month
    FROM (
        SELECT month_date FROM reporting.fact_ae_attendances
        UNION ALL
        SELECT month_date FROM reporting.fact_rtt_waits
    ) m
),
months AS (
    SELECT gs::date AS month_date, r.last_month
    FROM data_range r,
         LATERAL generate_series(r.first_month, r.last_month, INTERVAL '1 month') AS gs
),
fy AS (
    SELECT month_date, last_month,
           EXTRACT(YEAR FROM month_date)::int  AS calendar_year,
           EXTRACT(MONTH FROM month_date)::int AS month_number,
           CASE WHEN EXTRACT(MONTH FROM month_date) >= 4
                THEN EXTRACT(YEAR FROM month_date)::int
                ELSE EXTRACT(YEAR FROM month_date)::int - 1 END AS fy_start_year
    FROM months
)
SELECT
    month_date,
    calendar_year,
    month_number,
    TRIM(TO_CHAR(month_date, 'Month'))                        AS month_name,
    TO_CHAR(month_date, 'Mon YYYY')                           AS month_label,
    calendar_year * 100 + month_number                        AS year_month,
    fy_start_year,
    fy_start_year || '/' || LPAD(((fy_start_year + 1) % 100)::text, 2, '0') AS financial_year,
    ((month_number + 8) % 12) + 1                             AS fy_month_number,
    'Q' || (((month_number + 8) % 12) / 3 + 1)                AS fy_quarter,
    fy_start_year || '/' || LPAD(((fy_start_year + 1) % 100)::text, 2, '0')
        || ' Q' || (((month_number + 8) % 12) / 3 + 1)        AS fy_quarter_label,
    (month_date = last_month)                                 AS is_latest_month
FROM fy;
