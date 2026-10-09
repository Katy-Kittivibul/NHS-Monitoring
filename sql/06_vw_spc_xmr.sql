-- Purpose: XmR (individuals and moving-range) control chart per trust and measure, flagging points outside limits and runs of 7+ on one side of the mean.
-- Note: Limits are mean +/- 2.66 x average moving range over the trust's full history, clipped to 0-100 (they are percentages); only trusts with 15+ monthly points per measure are included.
DROP VIEW IF EXISTS reporting.vw_spc_xmr CASCADE;
CREATE VIEW reporting.vw_spc_xmr AS
WITH series AS (
    SELECT org_code, 'AE_4HR_PERFORMANCE' AS measure, month_date, performance_pct AS value
    FROM reporting.vw_ae_performance
    WHERE performance_pct IS NOT NULL
    UNION ALL
    SELECT org_code, 'RTT_18WK_PERFORMANCE' AS measure, month_date, pct_within_18wk AS value
    FROM reporting.vw_rtt_performance
    WHERE pct_within_18wk IS NOT NULL
),
eligible AS (
    SELECT org_code, measure
    FROM series
    GROUP BY org_code, measure
    HAVING COUNT(*) >= 15
),
points AS (
    SELECT s.org_code, s.measure, s.month_date, s.value,
           ROW_NUMBER() OVER (PARTITION BY s.org_code, s.measure ORDER BY s.month_date) AS rn,
           ABS(s.value - LAG(s.value) OVER (PARTITION BY s.org_code, s.measure ORDER BY s.month_date)) AS moving_range
    FROM series s
    JOIN eligible e ON e.org_code = s.org_code AND e.measure = s.measure
),
limits AS (
    SELECT p.*,
           AVG(value)        OVER (PARTITION BY org_code, measure) AS mean_value,
           AVG(moving_range) OVER (PARTITION BY org_code, measure) AS avg_moving_range
    FROM points p
),
sided AS (
    SELECT l.*,
           LEAST(100.0,    l.mean_value + 2.66 * l.avg_moving_range) AS upper_limit,
           GREATEST(0.0,   l.mean_value - 2.66 * l.avg_moving_range) AS lower_limit,
           CASE WHEN l.value > l.mean_value THEN 1
                WHEN l.value < l.mean_value THEN -1 ELSE 0 END AS side
    FROM limits l
),
islands AS (
    SELECT s.*,
           s.rn - ROW_NUMBER() OVER (PARTITION BY org_code, measure, side ORDER BY rn) AS island
    FROM sided s
),
runs AS (
    SELECT i.*,
           COUNT(*) OVER (PARTITION BY org_code, measure, side, island) AS run_length
    FROM islands i
)
SELECT
    org_code,
    measure,
    month_date,
    value,
    mean_value,
    avg_moving_range,
    upper_limit,
    lower_limit,
    (value > upper_limit OR value < lower_limit)              AS outside_limits,
    (side <> 0 AND run_length >= 7)                           AS run_of_7_plus,
    CASE WHEN side <> 0 AND run_length >= 7 THEN run_length END AS run_length
FROM runs;
