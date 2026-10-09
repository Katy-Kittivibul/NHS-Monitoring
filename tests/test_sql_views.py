"""Tests for the reporting views in sql/.

The structural tests need nothing. The data tests build the views into a throw-away
schema copied from a loaded "reporting" schema in PostgreSQL, and skip if no server or
no loaded data is available (as in CI).
"""
import re
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from reporting.apply_views import SQL_DIR, apply_views, view_files
from reporting.load_postgres import load_env

SOURCE_TABLES = ["fact_ae_attendances", "fact_rtt_waits", "dim_organisation", "dq_flags",
                 "trust_icb_current", "icb_to_region"]


def test_view_files_are_numbered_and_documented():
    files = view_files()
    assert [p.name[:2] for p in files] == [f"{i:02d}" for i in range(1, 9)]
    assert {p.name for p in files} == {p.name for p in SQL_DIR.glob("0*.sql")}
    for p in files:
        text = p.read_text(encoding="utf-8")
        assert text.startswith("-- Purpose:"), p.name
        assert re.search(r"CREATE VIEW reporting\.", text), p.name
        # every table reference is schema-qualified so the views build in any schema
        assert not re.search(r"\b(FROM|JOIN)\s+(?!reporting\.)(fact_|dim_|dq_|cp_)", text), p.name


@pytest.fixture(scope="module")
def pg_schema():
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        s = load_env()
        conn = psycopg2.connect(host=s["PGHOST"], port=s["PGPORT"], dbname=s["PGDATABASE"],
                                user=s["PGUSER"], password=s["PGPASSWORD"], connect_timeout=3)
    except (RuntimeError, psycopg2.OperationalError) as e:
        pytest.skip(f"PostgreSQL not available: {e}")
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('reporting.fact_ae_attendances') IS NOT NULL")
    if not cur.fetchone()[0]:
        conn.close()
        pytest.skip("reporting schema not loaded (run `make reporting`)")

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
