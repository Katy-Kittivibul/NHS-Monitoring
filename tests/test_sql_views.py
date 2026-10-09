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
