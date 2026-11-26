-- Purpose: every SPC rule breach and PELT changepoint from Stage 2 (cp_signals), for A&E and RTT, on both the trust's own series and its peer-relative series (trust minus the national value for the month), with the trust's name, region and ICB, a plain-English label, the numbers parsed out of the detail text and a robustness flag.
-- Grain: one row per dataset x org_code x month_date x method x signal_type (the cp_signals key).
-- Note: signal_value / centre_value are the monitored series' value and mean (SPC rules); mean_before / mean_after are the segment means either side of a PELT changepoint. Units are percent (A&E breach %, RTT % waiting over 18 weeks) on the Raw series and percentage points relative to the national value on the Peer-relative series. is_robust (changepoints on the Raw series only) is TRUE when the peer-relative series also has a changepoint for the same trust within one month, i.e. the shift is not just a national or seasonal pattern; it is NULL for other signal types. Trusts with a median monthly volume below the floor in the detail text (min_monthly_volume) are not monitored. org_code is not always in vw_dim_trust (e.g. non-trust providers), so trust attributes can be NULL.
DROP VIEW IF EXISTS reporting.vw_changepoints CASCADE;
CREATE VIEW reporting.vw_changepoints AS
SELECT
    s.dataset,
    REGEXP_REPLACE(s.dataset, '_peer$', '')                                         AS measure_dataset,
    CASE WHEN s.dataset LIKE '%\_peer' THEN 'Peer-relative' ELSE 'Raw' END           AS series,
    s.org_code,
    d.trust_name,
    d.region_code,
    d.region_name,
    d.icb_code,
    d.icb_name,
    s.month_date,
    s.method,
    s.signal_type,
    CASE WHEN s.method LIKE 'pelt%' THEN 'Changepoint (PELT)' ELSE 'Control-chart rule (SPC)' END AS signal_family,
    CASE s.signal_type
         WHEN 'changepoint'          THEN 'Level shift'
         WHEN 'beyond_3sigma_above'  THEN 'Single point above 3-sigma limit'
         WHEN 'beyond_3sigma_below'  THEN 'Single point below 3-sigma limit'
         WHEN 'run_above_mean'       THEN 'Run of 7+ above the mean'
         WHEN 'run_below_mean'       THEN 'Run of 7+ below the mean'
         ELSE s.signal_type END                                                   AS signal_label,
    CASE WHEN s.signal_type LIKE '%above%' THEN 'Above'
         WHEN s.signal_type LIKE '%below%' THEN 'Below'
         WHEN s.signal_type = 'changepoint' AND x.mean_after > x.mean_before THEN 'Up'
         WHEN s.signal_type = 'changepoint' AND x.mean_after < x.mean_before THEN 'Down'
         ELSE 'Unspecified' END                                                   AS direction,
    x.signal_value,
    x.centre_value,
    x.mean_before,
    x.mean_after,
    CASE WHEN s.signal_type = 'changepoint' AND s.dataset NOT LIKE '%\_peer'
         THEN EXISTS (SELECT 1 FROM reporting.cp_signals pr
                      WHERE pr.dataset = s.dataset || '_peer' AND pr.org_code = s.org_code
                        AND pr.signal_type = 'changepoint'
                        AND pr.month_date BETWEEN (s.month_date - INTERVAL '1 month')::date
                                              AND (s.month_date + INTERVAL '1 month')::date)
    END                                                                            AS is_robust,
    s.detail
FROM reporting.cp_signals s
CROSS JOIN LATERAL (
    SELECT (regexp_match(s.detail, '\mvalue=(-?[0-9.]+)'))[1]::double precision AS signal_value,
           (regexp_match(s.detail, '\mmean=(-?[0-9.]+)'))[1]::double precision  AS centre_value,
           (regexp_match(s.detail, 'mean_before=(-?[0-9.]+)'))[1]::double precision   AS mean_before,
           (regexp_match(s.detail, 'mean_after=(-?[0-9.]+)'))[1]::double precision    AS mean_after
) x
LEFT JOIN reporting.vw_dim_trust d ON d.org_code = s.org_code;
