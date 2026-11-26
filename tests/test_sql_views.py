"""Tests for the reporting views in sql/.

The structural tests need nothing. The data tests build the views into a throw-away
schema copied from a loaded "reporting" schema in PostgreSQL.

Locally they skip when no server / no loaded data is available. In CI, REQUIRE_POSTGRES=1
turns that skip into a failure (so the suite can never go green by silently skipping), and
the data is the synthetic tests/fixtures/build_fixture_db.py database (NHS_FIXTURE_DATA=1),
so the two tests that pin figures from the real NHS extracts skip with an explicit reason.
"""
import os
import re
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from reporting.apply_views import SQL_DIR, apply_views, view_files
from reporting.load_postgres import CSV_TABLES, TABLES, load_env

# Every table the loader publishes: the views are tested against the same set they run on.
SOURCE_TABLES = [spec["name"] for spec in TABLES + CSV_TABLES]


def test_view_files_are_numbered_and_documented():
    files = view_files()
    assert [p.name[:2] for p in files] == [f"{i:02d}" for i in range(0, 13)]
    assert {p.name for p in files} == {p.name for p in SQL_DIR.glob("[0-9][0-9]_*.sql")}
    for p in files:
        text = p.read_text(encoding="utf-8")
        assert text.startswith("-- Purpose:"), p.name
        assert re.search(r"CREATE VIEW reporting\.", text), p.name
        assert re.search(r"^-- Grain:", text, re.M), f"{p.name} must state its grain"
        # every table reference is schema-qualified so the views build in any schema
        unqualified = r"\b(FROM|JOIN)\s+(?!reporting\.)(fact_|dim_|dq_|cp_|corr_|pipeline_)"
        assert not re.search(unqualified, text), p.name


REQUIRE_POSTGRES = os.environ.get("REQUIRE_POSTGRES") == "1"
FIXTURE_DATA = os.environ.get("NHS_FIXTURE_DATA") == "1"
needs_real_data = pytest.mark.skipif(
    FIXTURE_DATA, reason="pinned real-data figure: needs the real NHS extracts, not the CI fixture"
)


def _unavailable(reason: str):
    """Skip locally; fail when CI has declared Postgres mandatory."""
    if REQUIRE_POSTGRES:
        pytest.fail(f"REQUIRE_POSTGRES=1 but {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def pg_schema():
    if REQUIRE_POSTGRES:
        import psycopg2
    else:
        psycopg2 = pytest.importorskip("psycopg2")
    try:
        s = load_env()
        conn = psycopg2.connect(host=s["PGHOST"], port=s["PGPORT"], dbname=s["PGDATABASE"],
                                user=s["PGUSER"], password=s["PGPASSWORD"], connect_timeout=3)
    except (RuntimeError, psycopg2.OperationalError) as e:
        _unavailable(f"PostgreSQL not available: {e}")
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('reporting.fact_ae_attendances') IS NOT NULL")
    if not cur.fetchone()[0]:
        conn.close()
        _unavailable("reporting schema not loaded (run `make reporting`)")

    schema = f"reporting_test_{uuid.uuid4().hex[:8]}"
    try:
        cur.execute(f"CREATE SCHEMA {schema}")
        for t in SOURCE_TABLES:
            cur.execute(f"CREATE TABLE {schema}.{t} AS SELECT * FROM reporting.{t}")
        apply_views(conn, schema)
        conn.commit()
        yield conn, schema
    finally:
        conn.rollback()
        conn.cursor().execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.commit()
        conn.close()


def _one(pg, sql):
    conn, schema = pg
    cur = conn.cursor()
    cur.execute(sql.replace("{s}", schema))
    return cur.fetchone()[0]


def test_no_duplicate_trust_months(pg_schema):
    for view in ("vw_ae_performance", "vw_rtt_performance"):
        dupes = _one(pg_schema, f"SELECT COUNT(*) FROM (SELECT org_code, month_date FROM {{s}}.{view} "
                                "GROUP BY 1, 2 HAVING COUNT(*) > 1) d")
        assert dupes == 0, view


def test_performance_percentages_within_0_100(pg_schema):
    assert _one(pg_schema, "SELECT COUNT(*) FROM {s}.vw_ae_performance "
                           "WHERE performance_pct < 0 OR performance_pct > 100") == 0
    assert _one(pg_schema, "SELECT COUNT(*) FROM {s}.vw_rtt_performance "
                           "WHERE pct_within_18wk < 0 OR pct_within_18wk > 100") == 0


def test_rtt_national_total_equals_c999_sum(pg_schema):
    """The view's national list must equal the C_999 rows alone, month by month (no double count)."""
    mismatched = _one(pg_schema, """
        SELECT COUNT(*) FROM (
            SELECT v.month_date, v.total AS view_total, f.total AS c999_total
            FROM (SELECT month_date, SUM(waiting_list_size) AS total
                  FROM {s}.vw_rtt_performance GROUP BY 1) v
            FULL JOIN (SELECT month_date, SUM(waiting_list_size) AS total
                       FROM {s}.fact_rtt_waits WHERE specialty_code = 'C_999' GROUP BY 1) f
              USING (month_date)
        ) x WHERE view_total IS DISTINCT FROM c999_total""")
    assert mismatched == 0


@needs_real_data
def test_pinned_rtt_national_total_2024_03(pg_schema):
    assert _one(pg_schema, "SELECT SUM(waiting_list_size) FROM {s}.vw_rtt_performance "
                           "WHERE month_date = '2024-03-01'") == 7155769


def test_benchmark_national_equals_weighted_national(pg_schema):
    """National figures must be weighted over all providers, matching the performance views."""
    ae = _one(pg_schema, """
        SELECT COUNT(*) FROM (
            SELECT b.month_date, b.national_pct,
                   (SELECT 100.0 * SUM(within_4hr) / SUM(attendances) FROM {s}.vw_ae_performance a
                    WHERE a.month_date = b.month_date) AS expected
            FROM (SELECT DISTINCT month_date, national_pct FROM {s}.vw_benchmark
                  WHERE measure = 'AE_4HR_PERFORMANCE') b
        ) x WHERE ABS(national_pct - expected) > 1e-9""")
    assert ae == 0
    rtt = _one(pg_schema, """
        SELECT COUNT(*) FROM (
            SELECT b.national_pct,
                   (SELECT 100.0 * SUM(within_18wk) / SUM(waiting_list_size) FROM {s}.vw_rtt_performance r
                    WHERE r.month_date = b.month_date) AS expected
            FROM (SELECT DISTINCT month_date, national_pct FROM {s}.vw_benchmark
                  WHERE measure = 'RTT_18WK_PERFORMANCE') b
        ) x WHERE ABS(national_pct - expected) > 1e-9""")
    assert rtt == 0


@needs_real_data
def test_pinned_ae_national_performance_2024_12(pg_schema):
    dec_2024 = _one(pg_schema, "SELECT ROUND(MAX(national_pct), 2) FROM {s}.vw_benchmark "
                               "WHERE measure = 'AE_4HR_PERFORMANCE' AND month_date = '2024-12-01'")
    assert float(dec_2024) == 70.46


def test_benchmark_contains_only_active_nhs_trusts(pg_schema):
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.vw_benchmark b JOIN {s}.vw_dim_trust d USING (org_code)
        WHERE d.provider_type <> 'NHS trust' OR NOT d.is_active""") == 0
    assert _one(pg_schema, "SELECT COUNT(*) FROM {s}.vw_benchmark WHERE rank_in_region IS NOT NULL "
                           "AND region_name = 'Unknown'") == 0
    assert _one(pg_schema, "SELECT COUNT(DISTINCT provider_type) FROM {s}.vw_dim_trust") == 2


def test_spc_limits_within_0_100(pg_schema):
    assert _one(pg_schema, "SELECT COUNT(*) FROM {s}.vw_spc_xmr "
                           "WHERE lower_limit < 0 OR upper_limit > 100 OR lower_limit > upper_limit") == 0


def test_type1_attendances_never_exceed_total(pg_schema):
    assert _one(pg_schema, "SELECT COUNT(*) FROM {s}.vw_ae_performance "
                           "WHERE type1_attendances > attendances "
                           "OR type1_breaches_4hr > type1_attendances") == 0
    assert _one(pg_schema, "SELECT COUNT(*) FROM {s}.vw_ae_performance "
                           "WHERE type1_attendances = 0 AND type1_performance_pct IS NOT NULL") == 0


def test_no_trust_without_type1_is_ranked_for_ae(pg_schema):
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.vw_benchmark b JOIN {s}.vw_dim_trust d USING (org_code)
        WHERE b.measure IN ('AE_4HR_PERFORMANCE', 'AE_4HR_TYPE1') AND NOT d.has_type1_ae
          AND (b.rank_in_region IS NOT NULL OR b.rank_national IS NOT NULL)""") == 0
    # the Type 1 measure only covers trusts with Type 1 activity, and its national figure is weighted
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.vw_benchmark b JOIN {s}.vw_dim_trust d USING (org_code)
        WHERE b.measure = 'AE_4HR_TYPE1' AND NOT d.has_type1_ae""") == 0
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT b.month_date, b.national_pct,
                   (SELECT 100.0 * SUM(type1_attendances - type1_breaches_4hr) / SUM(type1_attendances)
                    FROM {s}.vw_ae_performance a WHERE a.month_date = b.month_date) AS expected
            FROM {s}.vw_benchmark b WHERE b.measure = 'AE_4HR_TYPE1'
        ) x WHERE ABS(national_pct - expected) > 1e-9""") == 0


# --- Phase 3 views (targets, changepoints, correlations, primary care, pipeline health) ---

def test_ae_targets_do_not_overlap_and_cover_every_month(pg_schema):
    overlaps = _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.vw_ae_targets a JOIN {s}.vw_ae_targets b
          ON a.fy_label < b.fy_label AND a.valid_to >= b.valid_from""")
    assert overlaps == 0
    uncovered = _one(pg_schema,
                     "SELECT COUNT(*) FROM {s}.vw_ae_performance WHERE national_target_pct IS NULL")
    assert uncovered == 0


def test_ae_excess_breaches_and_admissions_are_consistent(pg_schema):
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.vw_ae_performance
        WHERE excess_breaches_vs_target < 0 OR excess_breaches_vs_target > breaches_4hr + 1e-9
           OR admissions < 0 OR admission_conversion_pct NOT BETWEEN 0 AND 100""") == 0
    # meeting the target means no excess breaches; missing it means some
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.vw_ae_performance
        WHERE (met_target AND excess_breaches_vs_target > 1e-9)
           OR (NOT met_target AND excess_breaches_vs_target <= 0)""") == 0


def test_every_fact_org_is_in_the_trust_dimension(pg_schema):
    for table in ("fact_ae_attendances", "fact_rtt_waits"):
        orphans = _one(pg_schema, f"""
            SELECT COUNT(DISTINCT f.org_code) FROM {{s}}.{table} f
            LEFT JOIN {{s}}.vw_dim_trust d ON d.org_code = f.org_code WHERE d.org_code IS NULL""")
        assert orphans == 0, table


def test_no_negative_counts_in_facts(pg_schema):
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.fact_ae_attendances
        WHERE total_attendances < 0 OR total_breaches_4hr < 0 OR admissions_via_ae < 0""") == 0
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.fact_rtt_waits
        WHERE waiting_list_size < 0 OR over_18wk_count < 0 OR over_52wk_count < 0""") == 0


def test_changepoints_keep_every_signal_once(pg_schema):
    assert _one(pg_schema, "SELECT COUNT(*) FROM {s}.vw_changepoints") == \
        _one(pg_schema, "SELECT COUNT(*) FROM {s}.cp_signals")
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.vw_changepoints
        WHERE direction NOT IN ('Above', 'Below', 'Up', 'Down', 'Unspecified')
           OR (signal_type = 'changepoint' AND (mean_before IS NULL OR mean_after IS NULL))""") == 0
    # a shift too small to show at the 2 decimals stored in the detail text has no direction
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.vw_changepoints
        WHERE direction = 'Unspecified' AND mean_before IS DISTINCT FROM mean_after""") == 0


def test_correlations_are_valid_and_flagged_exploratory(pg_schema):
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.vw_correlations
        WHERE correlation_metric NOT BETWEEN -1 AND 1 OR p_value NOT BETWEEN 0 AND 1 OR n_obs <= 0
           OR q_value NOT BETWEEN 0 AND 1
           OR ci_low > correlation_metric OR ci_high < correlation_metric""") == 0
    # BH adjustment can only raise a p-value, and significance means q <= 0.05
    assert _one(pg_schema, "SELECT COUNT(*) FROM {s}.vw_correlations WHERE q_value < p_value - 1e-12") == 0
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.vw_correlations
        WHERE (finding_category = 'Significant after FDR') <> COALESCE(q_value <= 0.05, FALSE)
          AND tier NOT LIKE 'tier3b%'""") == 0
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.vw_correlations
        WHERE finding_category NOT IN ('Significant after FDR', 'Not significant', 'ICB changepoint',
                                       'No ICB changepoint', 'Not assessed')""") == 0
    # r, p and q travel together: no correlation row has r but no p or q
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.vw_correlations
        WHERE correlation_metric IS NOT NULL AND (p_value IS NULL OR q_value IS NULL)
          AND tier NOT LIKE 'tier3%'""") == 0
    # every window is 6 months, so nothing may be presented as confirmatory
    assert _one(pg_schema, "SELECT COUNT(*) FROM {s}.vw_correlations "
                           "WHERE n_obs IS NOT NULL AND n_obs < 12 AND NOT is_exploratory") == 0
    # the view keeps every row of its three sources
    assert _one(pg_schema, "SELECT COUNT(*) FROM {s}.vw_correlations") == _one(pg_schema, """
        SELECT (SELECT COUNT(*) FROM {s}.corr_signals) + (SELECT COUNT(*) FROM {s}.corr_signals_tier3a)
             + (SELECT COUNT(*) FROM {s}.corr_signals_tier3b)""")


def test_changepoints_robustness_requires_a_peer_changepoint_nearby(pg_schema):
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.vw_changepoints
        WHERE (is_robust IS NOT NULL) <> (signal_type = 'changepoint' AND series = 'Raw')""") == 0
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM {s}.vw_changepoints r
        WHERE r.is_robust AND NOT EXISTS (
            SELECT 1 FROM {s}.vw_changepoints p
            WHERE p.series = 'Peer-relative' AND p.org_code = r.org_code
              AND p.measure_dataset = r.measure_dataset
              AND p.signal_type = 'changepoint'
              AND ABS(p.month_date - r.month_date) <= 31)""") == 0


def test_tier3b_changepoint_means_are_not_mistaken_for_correlations(pg_schema):
    """'mean_after=6.34' contains 'r=': a naive regex would report r=6.34 for a changepoint."""
    assert _one(pg_schema, "SELECT COUNT(*) FROM {s}.vw_correlations "
                           "WHERE tier = 'tier3b_icb_changepoint' AND correlation_metric IS NOT NULL") == 0


def test_primary_care_grain_and_totals(pg_schema):
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM (SELECT icb_code, month_date FROM {s}.vw_primary_care_icb
                              GROUP BY 1, 2 HAVING COUNT(*) > 1) d""") == 0
    assert _one(pg_schema, "SELECT COUNT(*) FROM {s}.vw_primary_care_icb "
                           "WHERE pct_uda_achieved < 0 OR prescribing_items < 0") == 0
    # rolling practices and contracts up to ICBs must not lose or double count anything
    assert _one(pg_schema, "SELECT COALESCE(SUM(prescribing_items), 0) FROM {s}.vw_primary_care_icb") == \
        _one(pg_schema, """SELECT COALESCE(SUM(p.items), 0) FROM {s}.fact_prescribing p
                           JOIN {s}.dim_sub_icb s ON s.sub_icb_code = p.sub_icb_code""")
    assert _one(pg_schema, "SELECT COALESCE(SUM(uda_delivered), 0)::bigint FROM {s}.vw_primary_care_icb") == \
        _one(pg_schema, "SELECT COALESCE(SUM(uda_delivered), 0)::bigint FROM {s}.fact_dental_uda")


def test_every_prescribing_sub_icb_is_mapped(pg_schema):
    assert _one(pg_schema, """
        SELECT COUNT(DISTINCT p.sub_icb_code) FROM {s}.fact_prescribing p
        LEFT JOIN {s}.dim_sub_icb s ON s.sub_icb_code = p.sub_icb_code WHERE s.sub_icb_code IS NULL""") == 0


def test_pipeline_health_has_one_latest_run_per_stage_and_dataset(pg_schema):
    assert _one(pg_schema, "SELECT COUNT(*) FROM {s}.vw_pipeline_health") == \
        _one(pg_schema, "SELECT COUNT(*) FROM {s}.pipeline_runs")
    assert _one(pg_schema, """
        SELECT COUNT(*) FROM (SELECT stage, dataset FROM {s}.vw_pipeline_health WHERE latest_run
                              GROUP BY 1, 2 HAVING COUNT(*) > 1) d""") == 0
    assert _one(pg_schema, "SELECT COUNT(*) FROM {s}.vw_pipeline_health "
                           "WHERE is_failure <> (status NOT IN ('success', 'skipped'))") == 0
