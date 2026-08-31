"""Tests for Stage 1 data quality drift checks."""
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from dq.ae_attendances import run_ae_attendances_dq
from dq.checks import (
    check_null_rate_jump,
    check_org_missing_vs_prior_month,
    check_org_never_reported,
    check_out_of_range,
    check_row_count_jump,
    ensure_dq_schema_exists,
    write_flags,
)
from dq.dental_uda import run_dental_uda_dq
from dq.prescribing import run_prescribing_dq
from dq.rtt_waits import run_rtt_waits_dq
from dq.run_all import run_all_dq
from etl.common import get_connection

REAL_DB_PATH = "data/processed/nhs.duckdb"
VALID_STATUSES = {"OK", "WARN", "FAIL"}


# --- Synthetic fixtures -----------------------------------------------------

@pytest.fixture
def synthetic_conn():
    """In-memory DuckDB connection with a small hand-crafted fact table."""
    conn = duckdb.connect(":memory:")
    yield conn
    conn.close()


def _make_table(conn, table, df):
    """Build a synthetic fact table with month_date cast to DATE, matching the real schema."""
    conn.register("df_view", df)
    conn.execute(f"""
        CREATE TABLE {table} AS
        SELECT CAST(month_date AS DATE) AS month_date, * EXCLUDE (month_date)
        FROM df_view
    """)
    conn.unregister("df_view")


# --- check_row_count_jump ----------------------------------------------------

class TestRowCountJump:
    def test_no_jump_is_ok(self, synthetic_conn):
        rows = (
            [pd.Timestamp("2024-01-01")] * 100
            + [pd.Timestamp("2024-02-01")] * 102
            + [pd.Timestamp("2024-03-01")] * 101
        )
        df = pd.DataFrame({"month_date": rows, "id": range(len(rows))})
        _make_table(synthetic_conn, "fact_test", df)

        flags = check_row_count_jump(synthetic_conn, "test_ds", "fact_test")
        assert len(flags) == 3
        assert flags[0]["status"] == "OK"  # no prior month baseline
        assert all(f["status"] == "OK" for f in flags), "small changes should not trigger flags"

    def test_large_jump_is_fail(self, synthetic_conn):
        rows = [pd.Timestamp("2024-01-01")] * 100 + [pd.Timestamp("2024-02-01")] * 300
        df = pd.DataFrame({"month_date": rows, "id": range(len(rows))})
        _make_table(synthetic_conn, "fact_test", df)

        flags = check_row_count_jump(synthetic_conn, "test_ds", "fact_test")
        by_month = {f["month_date"]: f for f in flags}
        feb = by_month[pd.Timestamp("2024-02-01")]
        assert feb["status"] == "FAIL"
        assert "row_count=300" in feb["detail"]

    def test_moderate_jump_is_warn(self, synthetic_conn):
        rows = [pd.Timestamp("2024-01-01")] * 100 + [pd.Timestamp("2024-02-01")] * 120
        df = pd.DataFrame({"month_date": rows, "id": range(len(rows))})
        _make_table(synthetic_conn, "fact_test", df)

        flags = check_row_count_jump(synthetic_conn, "test_ds", "fact_test")
        by_month = {f["month_date"]: f for f in flags}
        feb = by_month[pd.Timestamp("2024-02-01")]
        assert feb["status"] == "WARN"

    def test_gap_is_noted_in_detail(self, synthetic_conn):
        rows = [pd.Timestamp("2024-01-01")] * 100 + [pd.Timestamp("2024-06-01")] * 105
        df = pd.DataFrame({"month_date": rows, "id": range(len(rows))})
        _make_table(synthetic_conn, "fact_test", df)

        flags = check_row_count_jump(synthetic_conn, "test_ds", "fact_test")
        by_month = {f["month_date"]: f for f in flags}
        june = by_month[pd.Timestamp("2024-06-01")]
        assert "gap: 5 months" in june["detail"]


# --- check_null_rate_jump ----------------------------------------------------

class TestNullRateJump:
    def test_stable_null_rate_is_ok(self, synthetic_conn):
        df = pd.DataFrame({
            "month_date": [pd.Timestamp("2024-01-01")] * 10 + [pd.Timestamp("2024-02-01")] * 10,
            "val": ([1] * 9 + [None]) + ([1] * 9 + [None]),
        })
        _make_table(synthetic_conn, "fact_test", df)

        flags = check_null_rate_jump(synthetic_conn, "test_ds", "fact_test", columns=["val"])
        assert all(f["status"] == "OK" for f in flags)

    def test_null_rate_spike_is_fail(self, synthetic_conn):
        df = pd.DataFrame({
            "month_date": [pd.Timestamp("2024-01-01")] * 10 + [pd.Timestamp("2024-02-01")] * 10,
            "val": ([1] * 10) + ([None] * 5 + [1] * 5),
        })
        _make_table(synthetic_conn, "fact_test", df)

        flags = check_null_rate_jump(synthetic_conn, "test_ds", "fact_test", columns=["val"])
        by_month = {f["month_date"]: f for f in flags}
        feb = by_month[pd.Timestamp("2024-02-01")]
        assert feb["status"] == "FAIL"
        assert "null_rate=50.0%" in feb["detail"]

    def test_multiple_columns_produce_separate_checks(self, synthetic_conn):
        df = pd.DataFrame({
            "month_date": [pd.Timestamp("2024-01-01")] * 5,
            "a": [1, 1, None, 1, 1],
            "b": [1, 1, 1, 1, 1],
        })
        _make_table(synthetic_conn, "fact_test", df)

        flags = check_null_rate_jump(synthetic_conn, "test_ds", "fact_test", columns=["a", "b"])
        check_names = {f["check_name"] for f in flags}
        assert check_names == {"null_rate_jump_a", "null_rate_jump_b"}


# --- check_out_of_range -------------------------------------------------------

class TestOutOfRange:
    def test_all_in_range_is_ok(self, synthetic_conn):
        df = pd.DataFrame({
            "month_date": [pd.Timestamp("2024-01-01")] * 5,
            "pct": [0.0, 25.0, 50.0, 75.0, 100.0],
        })
        _make_table(synthetic_conn, "fact_test", df)

        flags = check_out_of_range(synthetic_conn, "test_ds", "fact_test", "pct", min_val=0, max_val=100)
        assert len(flags) == 1
        assert flags[0]["status"] == "OK"

    def test_values_above_max_fail(self, synthetic_conn):
        df = pd.DataFrame({
            "month_date": [pd.Timestamp("2024-01-01")] * 5,
            "pct": [0.0, 25.0, 50.0, 75.0, 150.0],
        })
        _make_table(synthetic_conn, "fact_test", df)

        flags = check_out_of_range(synthetic_conn, "test_ds", "fact_test", "pct", min_val=0, max_val=100)
        assert flags[0]["status"] == "FAIL"
        assert "1/5 rows outside" in flags[0]["detail"]

    def test_negative_values_fail_min_only_check(self, synthetic_conn):
        df = pd.DataFrame({
            "month_date": [pd.Timestamp("2024-01-01")] * 3,
            "cost": [10.0, -5.0, 20.0],
        })
        _make_table(synthetic_conn, "fact_test", df)

        flags = check_out_of_range(synthetic_conn, "test_ds", "fact_test", "cost", min_val=0)
        assert flags[0]["status"] == "FAIL"
        assert "1/3 rows outside" in flags[0]["detail"]

    def test_requires_at_least_one_bound(self, synthetic_conn):
        with pytest.raises(ValueError):
            check_out_of_range(synthetic_conn, "test_ds", "fact_test", "cost")


# --- check_org_missing_vs_prior_month -----------------------------------------

class TestOrgMissingVsPriorMonth:
    def test_stable_roster_is_ok(self, synthetic_conn):
        df = pd.DataFrame({
            "month_date": [pd.Timestamp("2024-01-01")] * 3 + [pd.Timestamp("2024-02-01")] * 3,
            "org_code": ["A", "B", "C"] + ["A", "B", "C"],
        })
        _make_table(synthetic_conn, "fact_test", df)

        flags = check_org_missing_vs_prior_month(synthetic_conn, "test_ds", "fact_test", org_col="org_code")
        assert all(f["status"] == "OK" for f in flags)

    def test_org_disappearing_is_flagged(self, synthetic_conn):
        # RET's case: present in month 1, gone in month 2, back in month 3.
        df = pd.DataFrame({
            "month_date": [pd.Timestamp("2024-01-01")] * 3 + [pd.Timestamp("2024-02-01")] * 2
                          + [pd.Timestamp("2024-03-01")] * 3,
            "org_code": ["A", "B", "RET"] + ["A", "B"] + ["A", "B", "RET"],
        })
        _make_table(synthetic_conn, "fact_test", df)

        flags = check_org_missing_vs_prior_month(synthetic_conn, "test_ds", "fact_test", org_col="org_code")
        by_month = {f["month_date"]: f for f in flags}

        feb = by_month[pd.Timestamp("2024-02-01")]
        assert feb["status"] == "FAIL"
        assert "RET" in feb["detail"]

        # RET reappearing in March isn't itself a "missing" event.
        mar = by_month[pd.Timestamp("2024-03-01")]
        assert mar["status"] == "OK"

    def test_first_month_has_no_prior_baseline(self, synthetic_conn):
        df = pd.DataFrame({
            "month_date": [pd.Timestamp("2024-01-01")] * 2,
            "org_code": ["A", "B"],
        })
        _make_table(synthetic_conn, "fact_test", df)

        flags = check_org_missing_vs_prior_month(synthetic_conn, "test_ds", "fact_test", org_col="org_code")
        assert len(flags) == 1
        assert flags[0]["status"] == "OK"
        assert "no prior month" in flags[0]["detail"]

    def test_a_dataset_wide_gap_does_not_hide_a_single_org_gap(self, synthetic_conn):
        """The scenario check_row_count_jump can't see: ~200 orgs report every
        month, one (RET) drops out for a single month, and the aggregate row
        count barely moves."""
        months = [pd.Timestamp(f"2024-{m:02d}-01") for m in range(1, 4)]
        orgs = [f"T{i:03d}" for i in range(200)]

        rows_month_date, rows_org_code = [], []
        for month in months:
            present = orgs if month != months[1] else [o for o in orgs if o != "T099"]
            rows_month_date += [month] * len(present)
            rows_org_code += present
        df = pd.DataFrame({"month_date": rows_month_date, "org_code": rows_org_code})
        _make_table(synthetic_conn, "fact_test", df)

        row_count_flags = check_row_count_jump(synthetic_conn, "test_ds", "fact_test")
        by_month_rc = {f["month_date"]: f for f in row_count_flags}
        assert by_month_rc[months[1]]["status"] == "OK"  # invisible to the aggregate check

        org_flags = check_org_missing_vs_prior_month(synthetic_conn, "test_ds", "fact_test", org_col="org_code")
        by_month_org = {f["month_date"]: f for f in org_flags}
        assert by_month_org[months[1]]["status"] == "FAIL"  # caught by the per-org check
        assert "T099" in by_month_org[months[1]]["detail"]


# --- check_org_never_reported --------------------------------------------------

class TestOrgNeverReported:
    @pytest.fixture
    def conn_with_dim_org(self, synthetic_conn):
        synthetic_conn.execute("""
            CREATE TABLE dim_organisation (
                org_code VARCHAR, org_name VARCHAR, org_type VARCHAR,
                parent_org_code VARCHAR, region_name VARCHAR
            )
        """)
        synthetic_conn.execute("""
            INSERT INTO dim_organisation VALUES
                ('A', 'Trust A', 'trust', NULL, NULL),
                ('B', 'Trust B', 'trust', NULL, NULL),
                ('GHOST', 'Ghost Trust', 'trust', NULL, NULL),
                ('Q01', 'Some ICB', 'icb', NULL, NULL)
        """)
        return synthetic_conn

    def test_all_orgs_present_is_ok(self, conn_with_dim_org):
        # conn_with_dim_org's org_type='trust' roster is A, B, GHOST — all three
        # must appear in fact_test for this to be a genuine "nothing missing" case.
        df = pd.DataFrame({
            "month_date": [pd.Timestamp("2024-01-01")] * 3,
            "org_code": ["A", "B", "GHOST"],
        })
        _make_table(conn_with_dim_org, "fact_test", df)

        flags = check_org_never_reported(conn_with_dim_org, "test_ds", "fact_test",
                                          org_col="org_code", dim_org_type="trust")
        assert len(flags) == 1
        assert flags[0]["status"] == "OK"

    def test_org_never_in_table_is_flagged(self, conn_with_dim_org):
        df = pd.DataFrame({
            "month_date": [pd.Timestamp("2024-01-01")] * 2,
            "org_code": ["A", "B"],
        })
        _make_table(conn_with_dim_org, "fact_test", df)

        flags = check_org_never_reported(conn_with_dim_org, "test_ds", "fact_test",
                                          org_col="org_code", dim_org_type="trust")
        assert flags[0]["status"] == "FAIL"
        assert "GHOST" in flags[0]["detail"]

    def test_only_checks_matching_org_type(self, conn_with_dim_org):
        # Q01 is an 'icb', not a 'trust' — a trust-scoped check must not flag it
        # even though it never appears in fact_test either.
        df = pd.DataFrame({
            "month_date": [pd.Timestamp("2024-01-01")] * 2,
            "org_code": ["A", "B"],
        })
        _make_table(conn_with_dim_org, "fact_test", df)

        flags = check_org_never_reported(conn_with_dim_org, "test_ds", "fact_test",
                                          org_col="org_code", dim_org_type="trust")
        assert "Q01" not in flags[0]["detail"]

    def test_empty_table_produces_no_flag(self, conn_with_dim_org):
        # Built directly with explicit types — an empty pandas DataFrame's dtypes
        # aren't reliably inferred as VARCHAR/DATE by DuckDB's CREATE TABLE AS.
        conn_with_dim_org.execute("CREATE TABLE fact_test (month_date DATE, org_code VARCHAR)")

        flags = check_org_never_reported(conn_with_dim_org, "test_ds", "fact_test",
                                          org_col="org_code", dim_org_type="trust")
        assert flags == []


# --- write_flags idempotency --------------------------------------------------

class TestWriteFlagsIdempotent:
    def test_rerun_does_not_duplicate(self, synthetic_conn):
        ensure_dq_schema_exists(synthetic_conn)
        flags = [
            {"dataset": "test_ds", "month_date": pd.Timestamp("2024-01-01").date(),
             "check_name": "row_count_jump", "status": "OK", "detail": "row_count=10"},
        ]
        write_flags(synthetic_conn, flags)
        write_flags(synthetic_conn, flags)

        count = synthetic_conn.execute("SELECT COUNT(*) FROM dq_flags WHERE dataset = 'test_ds'").fetchall()[0][0]
        assert count == 1

    def test_rerun_replaces_stale_flags_for_dataset(self, synthetic_conn):
        ensure_dq_schema_exists(synthetic_conn)
        first_run = [
            {"dataset": "test_ds", "month_date": pd.Timestamp("2024-01-01").date(),
             "check_name": "row_count_jump", "status": "OK", "detail": "v1"},
            {"dataset": "test_ds", "month_date": pd.Timestamp("2024-02-01").date(),
             "check_name": "row_count_jump", "status": "OK", "detail": "v1"},
        ]
        write_flags(synthetic_conn, first_run)

        # Second run only recomputed one month (e.g. earlier month fell out of the table)
        second_run = [
            {"dataset": "test_ds", "month_date": pd.Timestamp("2024-02-01").date(),
             "check_name": "row_count_jump", "status": "OK", "detail": "v2"},
        ]
        write_flags(synthetic_conn, second_run)

        rows = synthetic_conn.execute(
            "SELECT month_date, detail FROM dq_flags WHERE dataset = 'test_ds'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "v2"


# --- Integration tests against the real backfilled DB -------------------------

class TestRealDataIntegration:
    @pytest.fixture(autouse=True)
    def skip_if_no_db(self):
        if not Path(REAL_DB_PATH).exists():
            pytest.skip(f"{REAL_DB_PATH} not found — run etl/run_all.py first")

    @pytest.mark.parametrize("dataset,run_fn", [
        ("ae_attendances", run_ae_attendances_dq),
        ("rtt_waits", run_rtt_waits_dq),
        ("prescribing", run_prescribing_dq),
        ("dental_uda", run_dental_uda_dq),
    ])
    def test_dataset_dq_run_produces_valid_flags(self, dataset, run_fn):
        flags = run_fn(REAL_DB_PATH)
        assert len(flags) > 0, f"{dataset} DQ run should produce flags"
        for f in flags:
            assert f["dataset"] == dataset
            assert f["status"] in VALID_STATUSES
            assert f["month_date"] is not None
            assert f["check_name"]

    def test_run_all_dq_is_idempotent_on_real_db(self):
        run_all_dq(REAL_DB_PATH)
        conn = get_connection(REAL_DB_PATH)
        try:
            first_count = conn.execute("SELECT COUNT(*) FROM dq_flags").fetchall()[0][0]
        finally:
            conn.close()

        run_all_dq(REAL_DB_PATH)
        conn = get_connection(REAL_DB_PATH)
        try:
            second_count = conn.execute("SELECT COUNT(*) FROM dq_flags").fetchall()[0][0]
        finally:
            conn.close()

        assert first_count == second_count

    def test_backfilled_data_has_no_out_of_range_values(self):
        """Regression guard: the known-clean backfilled data should never FAIL a range check."""
        run_all_dq(REAL_DB_PATH)
        conn = get_connection(REAL_DB_PATH)
        try:
            bad = conn.execute("""
                SELECT dataset, month_date, check_name, detail
                FROM dq_flags
                WHERE check_name LIKE 'out_of_range_%' AND status = 'FAIL'
            """).fetchall()
        finally:
            conn.close()
        assert bad == [], f"Unexpected out-of-range values in backfilled data: {bad}"

    def test_ret_september_gap_is_caught_by_org_level_check(self):
        """Regression guard for the confirmed blind spot: check_row_count_jump
        never flagged RET's missing 2024-09 row (198->197 trusts, well under
        threshold); check_org_missing_vs_prior_month must."""
        run_all_dq(REAL_DB_PATH)
        conn = get_connection(REAL_DB_PATH)
        try:
            row = conn.execute("""
                SELECT status, detail FROM dq_flags
                WHERE dataset = 'ae_attendances' AND month_date = '2024-09-01'
                  AND check_name = 'org_missing_vs_prior_month'
            """).fetchall()
        finally:
            conn.close()
        assert len(row) == 1
        status, detail = row[0]
        assert status == "FAIL"
        assert "RET" in detail

    def test_all_datasets_present_in_dq_flags(self):
        run_all_dq(REAL_DB_PATH)
        conn = get_connection(REAL_DB_PATH)
        try:
            datasets = {row[0] for row in conn.execute("SELECT DISTINCT dataset FROM dq_flags").fetchall()}
        finally:
            conn.close()
        assert datasets == {"ae_attendances", "rtt_waits", "prescribing", "dental_uda"}
