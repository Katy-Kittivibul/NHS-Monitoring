-- Applies every reporting view in dependency order. Run from anywhere with:
--   psql -f sql/run_all.sql      or      make views
-- (\ir paths are relative to this file; reporting/apply_views.py understands them too.)
\ir 00_ae_targets.sql
\ir 01_dim_date.sql
\ir 02_dim_trust.sql
\ir 03_vw_ae_performance.sql
\ir 04_vw_rtt_performance.sql
\ir 05_vw_rtt_specialty.sql
\ir 06_vw_spc_xmr.sql
\ir 07_vw_benchmark.sql
\ir 08_vw_data_quality.sql
\ir 09_vw_changepoints.sql
\ir 10_vw_correlations.sql
\ir 11_vw_primary_care_icb.sql
\ir 12_vw_pipeline_health.sql
