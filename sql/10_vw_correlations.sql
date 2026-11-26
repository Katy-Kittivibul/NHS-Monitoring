-- Purpose: one unified view of the Stage 3 cross-dataset findings (Tier 1 region, Tier 2 trust, Tier 3 ICB-attributed) with the correlation r and its 95% interval, the raw p-value, the Benjamini-Hochberg q-value, a plain-English finding_category driven by FDR significance, and an explicit exploratory flag.
-- Grain: one row per tier x dataset_pair x geography_code x month_date (Tier 1/2), or per trust x ICB x month_date x finding (Tier 3).
-- Note: Tier 1/2 correlate month-on-month CHANGES (first differences) over 6 paired changes, and Tier 3 correlates trust vs ICB percentage changes over 6 months; levels are not used because trending series correlate spuriously. q_value is the Benjamini-Hochberg adjusted p-value within a test family (tier x dataset pair x month for Tier 1/2; metric for Tier 3A; see corr/inference.py) and finding_category 'Significant after FDR' means q <= 0.05. With n = 6 the confidence interval is very wide (ci_low..ci_high) and is_exploratory is TRUE whenever n_obs < 12; treat every row as an association worth checking, never as proof of cause. is_strong_effect is |r| >= 0.5 regardless of significance. Tier 3 rows that were skipped or had no full window are kept as 'Not assessed' (the reason is in finding) so coverage stays visible.
DROP VIEW IF EXISTS reporting.vw_correlations CASCADE;
CREATE VIEW reporting.vw_correlations AS
WITH tier12 AS (
    SELECT c.tier,
           c.dataset_pair,
           c.geography_level,
           c.geography_code,
           c.month_date,
           NULL::text                                                     AS icb_code,
           c.correlation_metric                                           AS r_value,
           c.p_value,
           c.n_obs,
           c.ci_low,
           c.ci_high,
           c.q_value,
           CASE WHEN c.correlation_metric IS NULL OR c.q_value IS NULL THEN 'Not assessed'
                WHEN c.is_significant_fdr THEN 'Significant after FDR'
                ELSE 'Not significant' END                                AS finding_category,
           NULL::text                                                     AS finding,
           c.detail
    FROM reporting.corr_signals c
),
tier3 AS (
    SELECT 'tier3a_trust_changepoint' AS tier, trust_code, icb_code, month_date, finding, detail,
           p_value, n_obs, ci_low, ci_high, q_value, is_significant_fdr
    FROM reporting.corr_signals_tier3a
    UNION ALL
    SELECT 'tier3b_icb_changepoint' AS tier, trust_code, icb_code, month_date, finding, detail,
           NULL::double precision, NULL::integer, NULL::double precision, NULL::double precision,
           NULL::double precision, NULL::boolean
    FROM reporting.corr_signals_tier3b
),
tier3_parsed AS (
    SELECT tier,
           CASE WHEN finding LIKE 'dental%' THEN 'ae_dental_icb' ELSE 'ae_prescribing_icb' END AS dataset_pair,
           'trust'                                                       AS geography_level,
           trust_code                                                    AS geography_code,
           month_date,
           icb_code,
           (regexp_match(detail, '\mr=(-?[0-9.]+)'))[1]::double precision  AS r_value,
           p_value,
           n_obs,
           ci_low,
           ci_high,
           q_value,
           CASE WHEN q_value IS NOT NULL AND is_significant_fdr          THEN 'Significant after FDR'
                WHEN q_value IS NOT NULL                                 THEN 'Not significant'
                WHEN finding LIKE '%\_no\_changepoint'                   THEN 'No ICB changepoint'
                WHEN finding LIKE '%\_changepoint\_detected'             THEN 'ICB changepoint'
                ELSE 'Not assessed' END                                  AS finding_category,
           finding,
           detail
    FROM tier3
)
SELECT u.tier,
       u.dataset_pair,
       u.geography_level,
       u.geography_code,
       COALESCE(d.trust_name, rg.region_name, u.geography_code) AS geography_name,
       u.month_date,
       u.icb_code,
       u.r_value                                                AS correlation_metric,
       u.ci_low,
       u.ci_high,
       u.p_value,
       u.q_value,
       u.n_obs,
       (u.n_obs IS NULL OR u.n_obs < 12)                        AS is_exploratory,
       (ABS(u.r_value) >= 0.5)                                  AS is_strong_effect,
       u.finding_category,
       u.finding,
       u.detail
FROM (
    SELECT * FROM tier12
    UNION ALL
    SELECT * FROM tier3_parsed
) u
LEFT JOIN reporting.vw_dim_trust d ON u.geography_level = 'trust' AND d.org_code = u.geography_code
LEFT JOIN (SELECT DISTINCT region_code, region_name FROM reporting.icb_to_region) rg
       ON u.geography_level = 'region' AND rg.region_code = u.geography_code;
