-- Purpose: one row per trust/provider with region, ICB, reporting span and active flag, as the organisation dimension for all trust-level views.
-- Grain: one row per org_code that reports A&E or RTT.
-- Note: Membership is by reporting in the A&E or RTT facts (not dim_organisation.org_type: databases loaded before the org-type precedence fix had the prescribing load overwrite it to 'practice' for 5 A&E walk-in centres, and fact membership stays correct either way); ICB comes from reporting.trust_icb_current (RE8 partner ICB in force in the trust's last reporting month); Unmapped and Multiple ICBs are labelled, never dropped.
DROP VIEW IF EXISTS reporting.vw_dim_trust CASCADE;
CREATE VIEW reporting.vw_dim_trust AS
WITH region_map (region_code, region_name, ae_label) AS (
    VALUES ('Y56', 'London',                   'NHS ENGLAND LONDON'),
           ('Y58', 'South West',               'NHS ENGLAND SOUTH WEST'),
           ('Y59', 'South East',               'NHS ENGLAND SOUTH EAST'),
           ('Y60', 'Midlands',                 'NHS ENGLAND MIDLANDS'),
           ('Y61', 'East of England',          'NHS ENGLAND EAST OF ENGLAND'),
           ('Y62', 'North West',               'NHS ENGLAND NORTH WEST'),
           ('Y63', 'North East and Yorkshire', 'NHS ENGLAND NORTH EAST AND YORKSHIRE')
),
obs AS (
    SELECT org_code, month_date, 'ae' AS src, org_name, UPPER(TRIM(parent_org)) AS region_ref
    FROM reporting.fact_ae_attendances
    UNION ALL
    SELECT org_code, month_date, 'rtt' AS src, org_name, region_code AS region_ref
    FROM reporting.fact_rtt_waits
    WHERE specialty_code = 'C_999'
),
latest_obs AS (
    SELECT DISTINCT ON (org_code) org_code, org_name, region_ref
    FROM obs
    ORDER BY org_code, month_date DESC, src
),
coverage AS (
    SELECT org_code,
           MIN(month_date)            AS first_month,
           MAX(month_date)            AS last_month,
           COUNT(DISTINCT month_date) AS months_reported,
           BOOL_OR(src = 'ae')        AS reports_ae,
           BOOL_OR(src = 'rtt')       AS reports_rtt
    FROM obs
    GROUP BY org_code
),
latest_ae AS (
    SELECT DISTINCT ON (org_code) org_code, attendances_type1
    FROM reporting.fact_ae_attendances
    ORDER BY org_code, month_date DESC
),
latest_month AS (
    SELECT MAX(month_date) AS latest_month FROM obs
),
icb AS (
    SELECT trust_code,
           MAX(n_icbs)   AS n_icbs,
           MIN(icb_code) AS icb_code,
           MIN(icb_name) AS ods_icb_name
    FROM reporting.trust_icb_current
    GROUP BY trust_code
),
-- Fallback region from the trust's ICBs: only when every ICB is known and they all share one region
-- (so a Multiple ICBs trust keeps its region if its ICBs agree); otherwise NULL.
icb_region AS (
    SELECT t.trust_code,
           MIN(r.region_code) AS region_code,
           MIN(r.region_name) AS region_name
    FROM reporting.trust_icb_current t
    LEFT JOIN reporting.icb_to_region r ON r.icb_code = t.icb_code
    WHERE t.icb_code IS NOT NULL
    GROUP BY t.trust_code
    HAVING COUNT(*) = COUNT(r.region_code) AND COUNT(DISTINCT r.region_code) = 1
)
SELECT
    c.org_code,
    l.org_name                                          AS trust_name,
    CASE WHEN c.org_code ~ '^R[A-Z0-9]{2}$' THEN 'NHS trust'
         ELSE 'Other provider' END AS provider_type,
    COALESCE(rm.region_code, ir.region_code, 'UNKNOWN') AS region_code,
    COALESCE(rm.region_name, ir.region_name, 'Unknown') AS region_name,
    CASE WHEN COALESCE(i.n_icbs, 0) = 1 THEN i.icb_code
         WHEN COALESCE(i.n_icbs, 0) > 1 THEN 'MULTI'
         ELSE 'UNMAPPED' END                            AS icb_code,
    CASE WHEN COALESCE(i.n_icbs, 0) = 1
              THEN COALESCE(r.icb_name, REPLACE(INITCAP(i.ods_icb_name), 'Nhs ', 'NHS '))
         WHEN COALESCE(i.n_icbs, 0) > 1 THEN 'Multiple ICBs'
         ELSE 'Unmapped' END                            AS icb_name,
    CASE WHEN COALESCE(i.n_icbs, 0) = 1 THEN 'Resolved'
         WHEN COALESCE(i.n_icbs, 0) > 1 THEN 'Multiple ICBs'
         ELSE 'Unmapped' END                            AS icb_mapping_status,
    c.reports_ae,
    c.reports_rtt,
    (COALESCE(la.attendances_type1, 0) > 0)             AS has_type1_ae,
    c.first_month,
    c.last_month,
    c.months_reported,
    (c.last_month = lm.latest_month)                    AS is_active,
    COALESCE(o.specialist_no_ae, FALSE)                 AS specialist_no_ae
FROM coverage c
LEFT JOIN reporting.dim_organisation o ON o.org_code = c.org_code
JOIN latest_obs l ON l.org_code = c.org_code
LEFT JOIN latest_ae la ON la.org_code = c.org_code
LEFT JOIN region_map rm ON rm.ae_label = l.region_ref OR rm.region_code = l.region_ref
LEFT JOIN icb i ON i.trust_code = c.org_code
LEFT JOIN icb_region ir ON ir.trust_code = c.org_code
LEFT JOIN reporting.icb_to_region r ON r.icb_code = i.icb_code
CROSS JOIN latest_month lm;
