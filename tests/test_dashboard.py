"""Tests for the dashboard's query layer (dashboard_queries.py) — schema
checks against the real DB. dashboard.py itself is Streamlit UI and isn't
imported here (importing it would trigger st.set_page_config/st.navigation
outside a live Streamlit run); dashboard_queries.py is deliberately
Streamlit-free so it can be tested directly.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import dashboard_queries as q

REAL_DB_PATH = "data/processed/nhs.duckdb"
KNOWN_TRUST = "RXC"  # present in fact_ae_attendances/fact_rtt_waits/cp_signals throughout this project


@pytest.fixture
def conn():
    if not Path(REAL_DB_PATH).exists():
        pytest.skip(f"{REAL_DB_PATH} not found — run etl/run_all.py, cp/run_all.py, corr/run_all.py first")
    c = q.get_connection(REAL_DB_PATH, read_only=True)
    yield c
    c.close()


def _skip_if_missing_table(conn, table):
    exists = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchall()[0][0]
    if not exists:
        pytest.skip(f"{table} not found — run the relevant pipeline stage first")


class TestTrustList:
    def test_schema_and_non_empty(self, conn):
        df = q.get_trust_list(conn)
        assert list(df.columns) == ["org_code", "org_name"]
        assert len(df) > 0

    def test_known_trust_present(self, conn):
        df = q.get_trust_list(conn)
        assert KNOWN_TRUST in df["org_code"].values


class TestStage1Queries:
    def test_dq_flags_schema(self, conn):
        _skip_if_missing_table(conn, "dq_flags")
        df = q.get_dq_flags(conn)
        assert list(df.columns) == ["dataset", "month_date", "check_name", "status", "detail"]

    def test_dq_volume_trends_schema(self, conn):
        df = q.get_dq_volume_trends(conn)
        assert list(df.columns) == ["month_date", "dataset", "record_count"]
        assert len(df) > 0
        assert set(df["dataset"].unique()) <= {"A&E", "RTT", "Prescribing", "Dental"}


class TestStage2Queries:
    def test_cp_signals_schema(self, conn):
        _skip_if_missing_table(conn, "cp_signals")
        df = q.get_cp_signals(conn, KNOWN_TRUST)
        assert list(df.columns) == ["dataset", "month_date", "method", "signal_type", "detail"]
        assert len(df) > 0  # RXC has a known, documented rising breach rate — expect signals

    def test_cp_signals_unknown_trust_is_empty_not_error(self, conn):
        _skip_if_missing_table(conn, "cp_signals")
        df = q.get_cp_signals(conn, "ZZZ_NOT_A_TRUST")
        assert list(df.columns) == ["dataset", "month_date", "method", "signal_type", "detail"]
        assert len(df) == 0

    def test_ae_timeseries_schema(self, conn):
        df = q.get_ae_timeseries(conn, KNOWN_TRUST)
        assert list(df.columns) == ["month_date", "breach_pct", "total_attendances", "total_breaches_4hr"]
        assert len(df) > 0

    def test_rtt_timeseries_schema(self, conn):
        df = q.get_rtt_timeseries(conn, KNOWN_TRUST)
        assert list(df.columns) == ["month_date", "over_18wk_pct", "waiting_list_size"]


class TestStage3Queries:
    def test_tier1_correlations_schema(self, conn):
        _skip_if_missing_table(conn, "corr_signals_tier1")
        df = q.get_tier1_correlations(conn)
        expected = {"tier", "dataset_pair", "geography_level", "geography_code", "month_date",
                    "correlation_metric", "detail"}
        assert expected <= set(df.columns)

    def test_tier2_correlations_schema(self, conn):
        _skip_if_missing_table(conn, "corr_signals")
        df = q.get_tier2_correlations(conn, KNOWN_TRUST)
        expected = {"tier", "dataset_pair", "geography_level", "geography_code", "month_date",
                    "correlation_metric", "detail"}
        assert expected <= set(df.columns)
        if len(df) > 0:
            assert (df["tier"] == "tier2_trust").all()
            assert (df["geography_code"] == KNOWN_TRUST).all()

    def test_tier3a_findings_schema(self, conn):
        _skip_if_missing_table(conn, "corr_signals_tier3a")
        df = q.get_tier3a_findings(conn, KNOWN_TRUST)
        assert list(df.columns) == ["trust_code", "icb_code", "month_date", "finding", "detail"]
        if len(df) > 0:
            assert (df["trust_code"] == KNOWN_TRUST).all()

    def test_tier3b_findings_schema(self, conn):
        _skip_if_missing_table(conn, "corr_signals_tier3b")
        df = q.get_tier3b_findings(conn, KNOWN_TRUST)
        assert list(df.columns) == ["trust_code", "icb_code", "month_date", "finding", "detail"]

    def test_tier3a_national_summary_schema_and_filters(self, conn):
        _skip_if_missing_table(conn, "corr_signals_tier3a")
        df = q.get_tier3a_national_summary(conn)
        assert list(df.columns) == ["trust_code", "icb_code", "month_date", "finding", "detail"]
        if len(df) > 0:
            assert (df["icb_code"] != "UNRESOLVED").all()
            assert (~df["finding"].str.contains("no_correlated_shift")).all()
