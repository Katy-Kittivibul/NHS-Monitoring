"""Tests for Stage 3 Tier 1 (region) and Tier 2 (trust) correlation."""
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from corr.correlation import rolling_correlation
from corr.geography import get_icb_region_map, get_trust_region_map
from corr.run_all import run_all_corr
from corr.signals import ensure_corr_schema_exists, write_corr_signals
from corr.tier1_region import run_tier1_region_correlations
from corr.tier2_trust import run_tier2_trust_correlations
from etl.common import get_connection

REAL_DB_PATH = "data/processed/nhs.duckdb"


def _dates(n, start="2024-01-01"):
    return list(pd.date_range(start, periods=n, freq="MS"))


# --- rolling_correlation ---------------------------------------------------

class TestRollingCorrelation:
    def test_perfectly_correlated_series_gives_r_near_1(self):
        dates = _dates(6)
        series_a = pd.DataFrame({"key": ["X"] * 6, "month_date": dates, "value": [1, 2, 3, 4, 5, 6]})
        series_b = pd.DataFrame({"key": ["X"] * 6, "month_date": dates, "value": [10, 20, 30, 40, 50, 60]})
        results = rolling_correlation(series_a, series_b)
        assert len(results) == 1
        assert results[0]["correlation_metric"] == pytest.approx(1.0)
        assert results[0]["key"] == "X"
        assert results[0]["month_date"] == dates[5]

    def test_anti_correlated_series_gives_r_near_neg1(self):
        dates = _dates(6)
        series_a = pd.DataFrame({"key": ["X"] * 6, "month_date": dates, "value": [1, 2, 3, 4, 5, 6]})
        series_b = pd.DataFrame({"key": ["X"] * 6, "month_date": dates, "value": [60, 50, 40, 30, 20, 10]})
        results = rolling_correlation(series_a, series_b)
        assert results[0]["correlation_metric"] == pytest.approx(-1.0)

    def test_fewer_than_window_months_produces_no_signal(self):
        dates = _dates(5)
        series_a = pd.DataFrame({"key": ["X"] * 5, "month_date": dates, "value": [1, 2, 3, 4, 5]})
        series_b = pd.DataFrame({"key": ["X"] * 5, "month_date": dates, "value": [1, 2, 3, 4, 5]})
        assert rolling_correlation(series_a, series_b) == []

    def test_gap_skips_affected_windows_but_not_later_ones(self):
        # 9 months in series_a; series_b is missing month index 2 entirely.
        # Every 6-month window that includes index 2 (windows ending at index
        # 5, 6, 7) must be skipped; only the window ending at index 8 (months
        # 3-8) is gap-free and should produce a result.
        dates = _dates(9)
        series_a = pd.DataFrame({"key": ["X"] * 9, "month_date": dates, "value": list(range(1, 10))})
        dates_b = dates[:2] + dates[3:]
        series_b = pd.DataFrame({"key": ["X"] * 8, "month_date": dates_b, "value": list(range(1, 9))})

        results = rolling_correlation(series_a, series_b)
        result_months = sorted(r["month_date"] for r in results)
        assert result_months == [dates[8]]

    def test_constant_series_is_skipped_not_nan(self):
        dates = _dates(6)
        series_a = pd.DataFrame({"key": ["X"] * 6, "month_date": dates, "value": [5, 5, 5, 5, 5, 5]})
        series_b = pd.DataFrame({"key": ["X"] * 6, "month_date": dates, "value": [1, 2, 3, 4, 5, 6]})
        assert rolling_correlation(series_a, series_b) == []

    def test_multiple_keys_are_independent(self):
        dates = _dates(6)
        series_a = pd.DataFrame({
            "key": ["X"] * 6 + ["Y"] * 6,
            "month_date": dates + dates,
            "value": [1, 2, 3, 4, 5, 6] + [1, 2, 3, 4, 5, 6],
        })
        series_b = pd.DataFrame({
            "key": ["X"] * 6 + ["Y"] * 6,
            "month_date": dates + dates,
            "value": [10, 20, 30, 40, 50, 60] + [60, 50, 40, 30, 20, 10],
        })
        results = rolling_correlation(series_a, series_b)
        by_key = {r["key"]: r["correlation_metric"] for r in results}
        assert by_key["X"] == pytest.approx(1.0)
        assert by_key["Y"] == pytest.approx(-1.0)


# --- geography resolution ---------------------------------------------------

class TestGetTrustRegionMap:
    def test_normalises_both_code_and_full_name_formats(self):
        conn = duckdb.connect(":memory:")
        conn.execute("""
            CREATE TABLE dim_organisation (
                org_code VARCHAR, org_name VARCHAR, org_type VARCHAR,
                parent_org_code VARCHAR, region_name VARCHAR
            )
        """)
        conn.execute("""
            INSERT INTO dim_organisation VALUES
                ('R0A', 'Manchester', 'trust', NULL, 'Y62'),
                ('RQX', 'Homerton', 'trust', NULL, 'NHS ENGLAND LONDON'),
                ('QGH', 'Some ICB', 'icb', NULL, NULL)
        """)
        result = get_trust_region_map(conn)
        mapping = dict(zip(result["org_code"], result["region_code"]))
        assert mapping == {"R0A": "Y62", "RQX": "Y56"}
        conn.close()

    def test_unrecognised_region_name_is_dropped(self):
        conn = duckdb.connect(":memory:")
        conn.execute("""
            CREATE TABLE dim_organisation (
                org_code VARCHAR, org_name VARCHAR, org_type VARCHAR,
                parent_org_code VARCHAR, region_name VARCHAR
            )
        """)
        conn.execute(
            "INSERT INTO dim_organisation VALUES ('RXX', 'Mystery Trust', 'trust', NULL, 'NOWHERESVILLE')"
        )
        result = get_trust_region_map(conn)
        assert result.empty
        conn.close()


class TestGetIcbRegionMap:
    def test_row_count_and_no_nulls(self):
        df = get_icb_region_map()
        assert len(df) == 43
        assert df.notna().all().all()

    def test_all_region_codes_valid(self):
        df = get_icb_region_map()
        valid_codes = {"Y56", "Y58", "Y59", "Y60", "Y61", "Y62", "Y63"}
        assert set(df["region_code"]) <= valid_codes

    def test_thames_valley_exception_present(self):
        df = get_icb_region_map()
        row = df[df["icb_code"] == "S0E4D"]
        assert len(row) == 1
        assert row.iloc[0]["region_code"] == "Y59"


# --- write_corr_signals idempotency -----------------------------------------

class TestWriteCorrSignalsIdempotent:
    @pytest.fixture
    def synthetic_conn(self):
        conn = duckdb.connect(":memory:")
        ensure_corr_schema_exists(conn)
        yield conn
        conn.close()

    def test_rerun_does_not_duplicate(self, synthetic_conn):
        signals = [{
            "tier": "tier1_region", "dataset_pair": "ae_prescribing_region", "geography_level": "region",
            "geography_code": "Y56", "month_date": pd.Timestamp("2024-09-01").date(),
            "correlation_metric": 0.5, "detail": "v1",
        }]
        write_corr_signals(synthetic_conn, "tier1_region", "ae_prescribing_region", signals)
        write_corr_signals(synthetic_conn, "tier1_region", "ae_prescribing_region", signals)

        count = synthetic_conn.execute("SELECT COUNT(*) FROM corr_signals").fetchall()[0][0]
        assert count == 1

    def test_rerun_clears_stale_signals_when_now_empty(self, synthetic_conn):
        signals = [{
            "tier": "tier2_trust", "dataset_pair": "ae_rtt_trust", "geography_level": "trust",
            "geography_code": "RD1", "month_date": pd.Timestamp("2024-09-01").date(),
            "correlation_metric": 0.5, "detail": "v1",
        }]
        write_corr_signals(synthetic_conn, "tier2_trust", "ae_rtt_trust", signals)
        # trust no longer has a full window
        write_corr_signals(synthetic_conn, "tier2_trust", "ae_rtt_trust", [])

        count = synthetic_conn.execute("SELECT COUNT(*) FROM corr_signals").fetchall()[0][0]
        assert count == 0

    def test_different_dataset_pairs_do_not_clobber_each_other(self, synthetic_conn):
        pair1 = [{
            "tier": "tier1_region", "dataset_pair": "ae_prescribing_region", "geography_level": "region",
            "geography_code": "Y56", "month_date": pd.Timestamp("2024-09-01").date(),
            "correlation_metric": 0.5, "detail": "v1",
        }]
        pair2 = [{
            "tier": "tier1_region", "dataset_pair": "ae_dental_region", "geography_level": "region",
            "geography_code": "Y56", "month_date": pd.Timestamp("2024-09-01").date(),
            "correlation_metric": -0.3, "detail": "v1",
        }]
        write_corr_signals(synthetic_conn, "tier1_region", "ae_prescribing_region", pair1)
        write_corr_signals(synthetic_conn, "tier1_region", "ae_dental_region", pair2)

        count = synthetic_conn.execute("SELECT COUNT(*) FROM corr_signals").fetchall()[0][0]
        assert count == 2


# --- Integration tests against the real backfilled DB ------------------------

class TestRealDataIntegration:
    @pytest.fixture(autouse=True)
    def skip_if_no_db(self):
        if not Path(REAL_DB_PATH).exists():
            pytest.skip(f"{REAL_DB_PATH} not found — run etl/run_all.py first")

    def test_tier1_produces_all_four_dataset_pairs(self):
        signals = run_tier1_region_correlations(REAL_DB_PATH)
        assert len(signals) > 0
        dataset_pairs = {s["dataset_pair"] for s in signals}
        assert dataset_pairs == {
            "ae_prescribing_region", "ae_dental_region",
            "rtt_prescribing_region", "rtt_dental_region",
        }
        for s in signals:
            assert s["geography_level"] == "region"
            assert -1.0 <= s["correlation_metric"] <= 1.0

    def test_tier2_produces_signals_from_shared_trust_codes(self):
        conn = get_connection(REAL_DB_PATH)
        try:
            ae_trusts = set(
                conn.execute("SELECT DISTINCT org_code FROM fact_ae_attendances").df()["org_code"]
            )
            rtt_trusts = set(conn.execute("SELECT DISTINCT org_code FROM fact_rtt_waits").df()["org_code"])
        finally:
            conn.close()
        shared_trusts = ae_trusts & rtt_trusts

        signals = run_tier2_trust_correlations(REAL_DB_PATH)
        assert len(signals) > 0
        for s in signals:
            assert s["geography_level"] == "trust"
            assert s["geography_code"] in shared_trusts
            assert -1.0 <= s["correlation_metric"] <= 1.0

    def test_run_all_corr_is_idempotent_on_real_db(self):
        run_all_corr(REAL_DB_PATH)
        conn = get_connection(REAL_DB_PATH)
        try:
            first_count = conn.execute("SELECT COUNT(*) FROM corr_signals").fetchall()[0][0]
        finally:
            conn.close()

        run_all_corr(REAL_DB_PATH)
        conn = get_connection(REAL_DB_PATH)
        try:
            second_count = conn.execute("SELECT COUNT(*) FROM corr_signals").fetchall()[0][0]
        finally:
            conn.close()

        assert first_count == second_count
        assert first_count > 0

    def test_all_tier_dataset_pairs_present_after_full_run(self):
        run_all_corr(REAL_DB_PATH)
        conn = get_connection(REAL_DB_PATH)
        try:
            pairs = {
                f"{row[0]}:{row[1]}"
                for row in conn.execute("SELECT DISTINCT tier, dataset_pair FROM corr_signals").fetchall()
            }
        finally:
            conn.close()
        assert pairs == {
            "tier1_region:ae_prescribing_region", "tier1_region:ae_dental_region",
            "tier1_region:rtt_prescribing_region", "tier1_region:rtt_dental_region",
            "tier2_trust:ae_rtt_trust",
        }
