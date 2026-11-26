"""Tests for the dashboard's query layer (app/dashboard_queries.py) — schema
checks against the real DB. app/dashboard.py itself is Streamlit UI and isn't
imported here (importing it would trigger st.set_page_config/st.navigation
outside a live Streamlit run); dashboard_queries.py is deliberately
Streamlit-free so it can be tested directly.
"""
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import dashboard_queries as q

REAL_DB_PATH = "data/processed/nhs.duckdb"
KNOWN_TRUST = "RXC"  # present in fact_ae_attendances/fact_rtt_waits/cp_signals throughout this project


@pytest.fixture
def conn():
    if not Path(REAL_DB_PATH).exists():
        pytest.skip(f"{REAL_DB_PATH} not found — run etl/run_all.py, cp/run_all.py, corr/run_all.py first")
    try:
        c = q.get_connection(REAL_DB_PATH, read_only=True)
    except duckdb.IOException as exc:
        pytest.skip(f"{REAL_DB_PATH} locked by another process: {exc}")
    yield c
    c.close()


@pytest.fixture
def hermetic_conn():
    """Self-contained in-memory DuckDB seeded with synthetic A&E and RTT rows.

    Exercises C_999 vs specialty separation, weighted regional/national ratios,
    FY operational targets (78% in FY25/26 vs 82% in FY26/27), YoY deltas, and
    Type 1 acute vs Type 3 walk-in ranking rules — with zero dependency on data/.
    """
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE dim_organisation (
            org_code VARCHAR PRIMARY KEY,
            org_name VARCHAR,
            org_type VARCHAR,
            parent_org_code VARCHAR,
            region_name VARCHAR,
            specialist_no_ae BOOLEAN
        );
        CREATE TABLE fact_ae_attendances (
            month_date DATE,
            org_code VARCHAR,
            org_name VARCHAR,
            parent_org VARCHAR,
            attendances_type1 BIGINT,
            attendances_type2 BIGINT,
            attendances_other BIGINT,
            total_attendances BIGINT,
            breaches_4hr_type1 BIGINT,
            breaches_4hr_type2 BIGINT,
            breaches_4hr_other BIGINT,
            total_breaches_4hr BIGINT,
            breach_pct DOUBLE,
            admissions_via_ae BIGINT
        );
        CREATE TABLE fact_rtt_waits (
            month_date DATE,
            org_code VARCHAR,
            org_name VARCHAR,
            region_code VARCHAR,
            specialty_code VARCHAR,
            specialty_name VARCHAR,
            waiting_list_size BIGINT,
            over_18wk_count BIGINT,
            over_18wk_pct DOUBLE,
            median_wait_weeks DOUBLE,
            over_52wk_count BIGINT
        );
    """)
    c.execute("""
        INSERT INTO dim_organisation VALUES
            ('RXC', 'East Sussex Healthcare NHS Trust', 'trust', NULL, 'NHS ENGLAND SOUTH EAST', FALSE),
            ('R1H', 'Barts Health NHS Trust', 'trust', NULL, 'NHS ENGLAND LONDON', FALSE),
            ('AD913', 'Walk-In Urgent Care Centre', 'trust', NULL, 'NHS ENGLAND LONDON', FALSE);

        -- Prior year (2025-05-01, FY25/26 => 78% target, max allowed breach rate 22%)
        INSERT INTO fact_ae_attendances VALUES
            ('2025-05-01', 'RXC', 'East Sussex Healthcare NHS Trust', 'NHS ENGLAND SOUTH EAST',
             800, 0, 200, 1000, 250, 0, 50, 300, 30.0, 200),
            ('2025-05-01', 'R1H', 'Barts Health NHS Trust', 'NHS ENGLAND LONDON',
             1600, 0, 400, 2000, 300, 0, 100, 400, 20.0, 600);

        -- Current year (2026-05-01, FY26/27 => 82% target, max allowed breach rate 18%)
        -- RXC: 1000 att, 250 breaches => 75.0% within 4hr (+5.0 pp YoY vs 70.0%), excess = 250 - 180 = 70
        -- R1H: 3000 att, 300 breaches => 90.0% within 4hr (+10.0 pp YoY vs 80.0%), excess = 0 (300 < 540)
        -- AD913 (UTC, Type 3 only): 500 att, 10 breaches => 98.0% within 4hr, NOT rankable against Type 1
        INSERT INTO fact_ae_attendances VALUES
            ('2026-05-01', 'RXC', 'East Sussex Healthcare NHS Trust', 'NHS ENGLAND SOUTH EAST',
             800, 0, 200, 1000, 220, 0, 30, 250, 25.0, 250),
            ('2026-05-01', 'R1H', 'Barts Health NHS Trust', 'NHS ENGLAND LONDON',
             2500, 0, 500, 3000, 250, 0, 50, 300, 10.0, 900),
            ('2026-05-01', 'AD913', 'Walk-In Urgent Care Centre', 'NHS ENGLAND LONDON',
             0, 0, 500, 500, 0, 0, 10, 10, 2.0, 0);

        -- RTT rows for 2026-05-01: C_999 provider totals + two specialty breakdown rows
        INSERT INTO fact_rtt_waits VALUES
            ('2026-05-01', 'RXC', 'East Sussex Healthcare NHS Trust', 'Y59',
             'C_999', 'Total', 10000, 4000, 40.0, 14.0, 300),
            ('2026-05-01', 'RXC', 'East Sussex Healthcare NHS Trust', 'Y59',
             'C_110', 'Trauma & Orthopaedics', 6000, 2800, 46.67, 16.0, 240),
            ('2026-05-01', 'RXC', 'East Sussex Healthcare NHS Trust', 'Y59',
             'C_100', 'General Surgery', 4000, 1200, 30.0, 12.0, 60),
            ('2026-05-01', 'R1H', 'Barts Health NHS Trust', 'Y56',
             'C_999', 'Total', 30000, 6000, 20.0, 10.0, 200),
            ('2026-05-01', 'R1H', 'Barts Health NHS Trust', 'Y56',
             'C_110', 'Trauma & Orthopaedics', 20000, 4200, 21.0, 11.0, 160),
            ('2026-05-01', 'R1H', 'Barts Health NHS Trust', 'Y56',
             'C_100', 'General Surgery', 10000, 1800, 18.0, 9.0, 40);
    """)
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
        # inference columns (p_value, q_value, ...) follow the original five
        assert list(df.columns)[:5] == ["trust_code", "icb_code", "month_date", "finding", "detail"]
        if len(df) > 0:
            assert (df["trust_code"] == KNOWN_TRUST).all()

    def test_tier3b_findings_schema(self, conn):
        _skip_if_missing_table(conn, "corr_signals_tier3b")
        df = q.get_tier3b_findings(conn, KNOWN_TRUST)
        assert list(df.columns) == ["trust_code", "icb_code", "month_date", "finding", "detail"]

    def test_tier3a_national_summary_schema_and_filters(self, conn):
        _skip_if_missing_table(conn, "corr_signals_tier3a")
        df = q.get_tier3a_national_summary(conn)
        assert list(df.columns)[:5] == ["trust_code", "icb_code", "month_date", "finding", "detail"]
        if len(df) > 0:
            assert (df["icb_code"] != "UNRESOLVED").all()
            assert (~df["finding"].str.contains("no_correlated_shift")).all()


class TestExecutiveQueriesHermetic:
    """Hermetic tests for Page 0 Executive Overview queries (always run in CI)."""

    def test_national_summary_weighted_rates_targets_and_no_c999_double_count(self, hermetic_conn):
        df = q.get_national_monthly_summary(hermetic_conn)
        assert len(df) == 2

        m25 = df.iloc[0]
        assert str(m25["month_date"])[:10] == "2025-05-01"
        assert m25["ae_target_pct"] == pytest.approx(78.0)
        # 2025-05: 3000 att, 700 breaches => 2300 within => 76.6667%; allowed = round(3000*0.22)=660 => 40
        assert m25["ae_4hr_pct"] == pytest.approx(2300 / 3000 * 100.0)
        assert m25["excess_breaches_vs_target"] == 40

        m26 = df.iloc[1]
        assert str(m26["month_date"])[:10] == "2026-05-01"
        assert m26["ae_target_pct"] == pytest.approx(82.0)
        # 2026-05: 4500 att, 560 breaches => 3940 within => 87.5556%; 560 < round(4500*0.18)=810 => 0 excess
        assert m26["total_attendances"] == 4500
        assert m26["ae_4hr_pct"] == pytest.approx(3940 / 4500 * 100.0)
        assert m26["excess_breaches_vs_target"] == 0
        # Admissions: (250 + 900 + 0) / 4500
        assert m26["admission_conversion_pct"] == pytest.approx(1150 / 4500 * 100.0)
        # RTT waiting list MUST equal C_999 sum (10,000 + 30,000 = 40,000), never 80,000!
        assert m26["waiting_list_size"] == 40000
        assert m26["rtt_18wk_pct"] == pytest.approx(30000 / 40000 * 100.0)
        assert m26["over_52wk_count"] == 500

    def test_trust_league_ranks_yoy_and_graceful_without_cp_signals(self, hermetic_conn):
        df = q.get_trust_benchmark_league(hermetic_conn)
        assert len(df) == 3
        by_org = df.set_index("org_code")

        # R1H (90%) ranks #1, RXC (75%) ranks #2; AD913 (walk-in UTC, no Type 1) is unranked
        assert by_org.loc["R1H", "ae_national_rank"] == 1
        assert by_org.loc["RXC", "ae_national_rank"] == 2
        assert by_org["ae_national_rank"].isna().loc["AD913"]

        # YoY deltas: RXC went 70% -> 75% (+5.0 pp); R1H went 80% -> 90% (+10.0 pp)
        assert by_org.loc["RXC", "ae_yoy_pp"] == pytest.approx(5.0)
        assert by_org.loc["R1H", "ae_yoy_pp"] == pytest.approx(10.0)
        assert by_org.loc["RXC", "excess_breaches_vs_target"] == 70
        assert by_org.loc["R1H", "excess_breaches_vs_target"] == 0
        assert (df["active_signals"] == 0).all()

        # Now add cp_signals and verify signal count surfaces cleanly
        hermetic_conn.execute("""
            CREATE TABLE cp_signals (
                dataset VARCHAR, org_code VARCHAR, month_date DATE,
                method VARCHAR, signal_type VARCHAR, detail VARCHAR
            );
            INSERT INTO cp_signals VALUES
                ('ae_attendances', 'RXC', '2026-05-01', 'pelt_changepoint', 'changepoint', 'shift'),
                ('ae_attendances', 'RXC', '2026-05-01', 'spc_rule1_3sigma', 'beyond_3sigma_above', 'spike');
        """)
        df_with_cp = q.get_trust_benchmark_league(hermetic_conn).set_index("org_code")
        assert df_with_cp.loc["RXC", "active_signals"] == 2
        assert df_with_cp.loc["R1H", "active_signals"] == 0

    def test_regional_summary_uses_volume_weighting_not_simple_average(self, hermetic_conn):
        df = q.get_regional_summary(hermetic_conn).set_index("region_name")
        assert set(df.index) == {"London", "South East"}
        # London has R1H (3000 att, 2700 within = 90%) + AD913 (500 att, 490 within = 98%)
        # Weighted = 3190 / 3500 = 91.1429% (NOT unweighted average 94.0%!)
        assert df.loc["London", "ae_4hr_pct"] == pytest.approx(3190 / 3500 * 100.0)
        assert df.loc["South East", "ae_4hr_pct"] == pytest.approx(75.0)

    def test_rtt_specialty_backlog_excludes_c999_and_shares_sum_to_100(self, hermetic_conn):
        df = q.get_rtt_specialty_backlog(hermetic_conn)
        assert "C_999" not in df["specialty_code"].values
        assert list(df["specialty_code"]) == ["C_110", "C_100"]
        assert df["waiting_list_size"].sum() == 40000
        assert df["share_of_over18wk_pct"].sum() == pytest.approx(100.0)
        assert df["share_of_52wk_pct"].sum() == pytest.approx(100.0)


class TestExecutiveQueriesRealDB:
    def test_executive_queries_against_loaded_db(self, conn):
        nat = q.get_national_monthly_summary(conn)
        assert len(nat) > 0
        assert (nat["ae_4hr_pct"].dropna().between(0.0, 100.0)).all()

        league = q.get_trust_benchmark_league(conn)
        assert len(league) > 0
        assert KNOWN_TRUST in league["org_code"].values

        reg = q.get_regional_summary(conn)
        assert len(reg) > 0

        spec = q.get_rtt_specialty_backlog(conn)
        assert len(spec) > 0
        assert "C_999" not in spec["specialty_code"].values


def test_db_path_can_be_overridden_for_the_demo(monkeypatch):
    """`make demo` points the dashboard at the synthetic fixture via NHS_DB_PATH."""
    import importlib

    import app.dashboard_queries as dq
    monkeypatch.setenv("NHS_DB_PATH", "somewhere/else.duckdb")
    assert importlib.reload(dq).DB_PATH == "somewhere/else.duckdb"
    monkeypatch.delenv("NHS_DB_PATH")
    assert importlib.reload(dq).DB_PATH.endswith("nhs.duckdb")
