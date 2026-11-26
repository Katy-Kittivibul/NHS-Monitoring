-- Purpose: data quality flags summarised by dataset, month, check and status, with a severity order, so Power BI can show pipeline health beside the performance views.
-- Grain: one row per dataset x month_date x check_name x status.
DROP VIEW IF EXISTS reporting.vw_data_quality CASCADE;
CREATE VIEW reporting.vw_data_quality AS
SELECT
    dataset,
    month_date,
    check_name,
    status,
    CASE status WHEN 'FAIL' THEN 1 WHEN 'WARN' THEN 2 ELSE 3 END AS severity_order,
    (status <> 'OK')                                              AS is_issue,
    COUNT(*)                                                      AS flag_count,
    LEFT(STRING_AGG(detail, ' | ' ORDER BY detail), 500)          AS detail
FROM reporting.dq_flags
GROUP BY dataset, month_date, check_name, status;
