-- Purpose: pipeline run history with each run's duration, a failure flag and the latest data month per dataset, so a report can show "data as of" and whether the last ingest worked.
-- Grain: one row per pipeline run (run_id), as recorded by etl/common.py track_run; latest_run flags the most recent run per stage x dataset.
-- Note: rows_processed and detail (JSON) are the loader's own counts. A status other than 'success' or 'skipped' means that run failed and the data may be stale. data_loaded_through is the latest month in the matching fact table, NULL for datasets without one.
DROP VIEW IF EXISTS reporting.vw_pipeline_health CASCADE;
CREATE VIEW reporting.vw_pipeline_health AS
WITH freshness (dataset, data_loaded_through) AS (
    SELECT 'ae_attendances', MAX(month_date) FROM reporting.fact_ae_attendances
    UNION ALL SELECT 'rtt_waits',   MAX(month_date) FROM reporting.fact_rtt_waits
    UNION ALL SELECT 'prescribing', MAX(month_date) FROM reporting.fact_prescribing
    UNION ALL SELECT 'dental_uda',  MAX(month_date) FROM reporting.fact_dental_uda
)
SELECT
    r.run_id,
    r.stage,
    r.dataset,
    r.started_at,
    r.finished_at,
    EXTRACT(EPOCH FROM (r.finished_at - r.started_at))::double precision AS duration_seconds,
    r.status,
    (r.status NOT IN ('success', 'skipped'))                            AS is_failure,
    r.rows_processed,
    r.detail,
    (r.started_at = MAX(r.started_at) OVER (PARTITION BY r.stage, r.dataset)) AS latest_run,
    f.data_loaded_through
FROM reporting.pipeline_runs r
LEFT JOIN freshness f ON f.dataset = r.dataset;
