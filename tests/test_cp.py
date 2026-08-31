"""Tests for Stage 2 SPC baseline rules and PELT changepoint detection."""
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cp.changepoints import COST_MODEL, DEFAULT_PENALTY, bic_penalty, detect_changepoints, run_changepoints
from cp.run_all import run_all_cp
from cp.signals import ensure_cp_schema_exists, get_ae_breach_pct_series, write_signals
from cp.spc_baseline import rule1_beyond_3sigma, rule2_run_of_7, run_spc_baseline, xmr_limits
from etl.common import get_connection

REAL_DB_PATH = "data/processed/nhs.duckdb"


def _dates(n, start="2024-01-01"):
    return list(pd.date_range(start, periods=n, freq="MS"))


# --- Part A: SPC baseline -----------------------------------------------------

class TestXmrLimits:
    def test_flat_series_has_zero_sigma(self):
        values = np.array([10.0] * 10)
        mean, lcl, ucl = xmr_limits(values)
        assert mean == pytest.approx(10.0)
        assert lcl == pytest.approx(10.0)
        assert ucl == pytest.approx(10.0)

    def test_limits_bracket_mean(self):
        values = np.array([10.0, 12.0, 9.0, 11.0, 10.5, 9.5, 10.2, 11.5])
        mean, lcl, ucl = xmr_limits(values)
        assert lcl < mean < ucl


class TestRule1Beyond3Sigma:
    def test_stable_series_has_no_signals(self):
        values = np.array([20.0, 21.0, 19.0, 20.5, 19.5, 20.2, 20.8, 19.7])
        dates = _dates(len(values))
        mean, lcl, ucl = xmr_limits(values)
        signals = rule1_beyond_3sigma(dates, values, mean, lcl, ucl)
        assert signals == []

    def test_single_spike_is_flagged(self):
        # Stable baseline around 20 with one enormous one-off spike.
        values = np.array([20.0, 20.2, 19.8, 20.1, 19.9, 20.0, 20.3, 95.0, 20.1, 19.9])
        dates = _dates(len(values))
        mean, lcl, ucl = xmr_limits(values)
        signals = rule1_beyond_3sigma(dates, values, mean, lcl, ucl)
        assert len(signals) == 1
        signal_date, signal_type, detail = signals[0]
        assert signal_date == dates[7]
        assert signal_type == "beyond_3sigma_above"

    def test_step_change_flags_both_regimes(self):
        # A clean step change: 8 points near 0.1% pooled with 5 much higher points.
        # The mean moving range is tiny within each regime, so every point ends
        # up >3 sigma from the *pooled* mean — this is correct XmR-chart behaviour
        # for a genuine special-cause shift, not a bug (see ARN03 in the real data).
        values = np.array([0.1] * 8 + [5.0] * 5)
        dates = _dates(len(values))
        mean, lcl, ucl = xmr_limits(values)
        signals = rule1_beyond_3sigma(dates, values, mean, lcl, ucl)
        signal_types = {s[1] for s in signals}
        assert "beyond_3sigma_below" in signal_types
        assert "beyond_3sigma_above" in signal_types


class TestRule2RunOf7:
    def test_alternating_series_has_no_signals(self):
        values = np.array([21.0, 19.0, 21.0, 19.0, 21.0, 19.0, 21.0, 19.0, 21.0, 19.0])
        dates = _dates(len(values))
        mean = float(np.mean(values))
        signals = rule2_run_of_7(dates, values, mean)
        assert signals == []

    def test_run_of_exactly_7_is_flagged_once(self):
        values = np.array([10.0, 10.0, 21, 22, 23, 21, 22, 23, 21])  # first 2 below mean, next 7 above
        mean = float(np.mean(values))
        dates = _dates(len(values))
        signals = rule2_run_of_7(dates, values, mean)
        assert len(signals) == 1
        signal_date, signal_type, detail = signals[0]
        assert signal_date == dates[8]  # 7th point of the run (0-indexed 8)
        assert signal_type == "run_above_mean"
        assert "run_length=7" in detail

    def test_run_of_6_is_not_flagged(self):
        # 6 consecutive points above the mean should not trigger the rule.
        values = np.array([5.0, 5.0, 5.0, 5.0, 8.0, 8.0, 8.0, 8.0, 8.0, 8.0])
        mean = float(np.mean(values))
        dates = _dates(len(values))
        signals = rule2_run_of_7(dates, values, mean)
        assert signals == []

    def test_longer_run_flags_every_point_from_7th_onward(self):
        values = np.array([1.0] * 3 + [9.0] * 9)  # 9-point run above mean
        mean = float(np.mean(values))
        dates = _dates(len(values))
        signals = rule2_run_of_7(dates, values, mean)
        # Run starts at index 3; rule triggers once length reaches 7 (index 9) and
        # stays triggered for the remaining points in the run (indices 9, 10, 11).
        assert len(signals) == 3
        assert [s[0] for s in signals] == [dates[9], dates[10], dates[11]]


# --- Part B: PELT changepoints -------------------------------------------------

class TestValidatedConfig:
    def test_cost_model_and_penalty_match_10_trust_comparison(self):
        # l2+BIC (var(signal) * log(n)) standardised on after comparing against
        # rbf+1.2 across 10 trusts (R1H, R0A, plus 8 more spanning low/high
        # variance): 3 wins / 4 ties / 0 losses for l2+BIC, with rbf+1.2 missing
        # real shifts on R0A, G0Q0L, and R1K.
        assert COST_MODEL == "l2"
        assert DEFAULT_PENALTY is None  # signals "compute BIC penalty per-series"

    def test_bic_penalty_is_variance_times_log_n(self):
        values = np.array([20.0, 20.5, 19.8, 20.2, 19.9, 20.1] + [40.0, 40.5, 39.8, 40.2, 39.9, 40.1])
        assert bic_penalty(values) == pytest.approx(float(np.var(values) * np.log(len(values))))


class TestDetectChangepoints:
    def test_flat_series_has_no_changepoints(self):
        values = np.array([20.0] * 12)
        assert detect_changepoints(values) == []

    def test_clean_step_change_is_detected(self):
        values = np.array([20.0, 20.5, 19.8, 20.2, 19.9, 20.1] + [40.0, 40.5, 39.8, 40.2, 39.9, 40.1])
        breakpoints = detect_changepoints(values)
        assert breakpoints == [6]

    def test_gradual_ramp_is_detected_with_default_penalty(self):
        rng = np.random.default_rng(42)
        low = rng.normal(20, 1.0, 6)
        high = rng.normal(35, 1.0, 6)
        values = np.concatenate([low, high])
        breakpoints = detect_changepoints(values)
        assert len(breakpoints) >= 1
        assert any(4 <= bp <= 8 for bp in breakpoints)

    def test_no_change_with_default_penalty_on_noisy_flat_series(self):
        rng = np.random.default_rng(7)
        values = rng.normal(20, 0.5, 12)
        assert detect_changepoints(values) == []

    def test_high_explicit_penalty_suppresses_detection(self):
        # Proves the penalty override still does something under the l2+BIC
        # default — a penalty far above the ~249 BIC penalty this series would
        # get by default should swallow a clean step.
        values = np.array([20.0, 20.5, 19.8, 20.2, 19.9, 20.1] + [40.0, 40.5, 39.8, 40.2, 39.9, 40.1])
        assert detect_changepoints(values, penalty=2000.0) == []


# --- write_signals idempotency --------------------------------------------------

class TestWriteSignalsIdempotent:
    @pytest.fixture
    def synthetic_conn(self):
        conn = duckdb.connect(":memory:")
        ensure_cp_schema_exists(conn)
        yield conn
        conn.close()

    def test_rerun_does_not_duplicate(self, synthetic_conn):
        signals = [
            {"dataset": "ae_attendances", "org_code": "ABC", "month_date": pd.Timestamp("2024-01-01").date(),
             "method": "spc_rule1_3sigma", "signal_type": "beyond_3sigma_above", "detail": "v1"},
        ]
        write_signals(synthetic_conn, "ae_attendances", "spc_rule1_3sigma", signals)
        write_signals(synthetic_conn, "ae_attendances", "spc_rule1_3sigma", signals)

        count = synthetic_conn.execute("SELECT COUNT(*) FROM cp_signals").fetchall()[0][0]
        assert count == 1

    def test_rerun_clears_stale_signals_when_now_empty(self, synthetic_conn):
        signals = [
            {"dataset": "ae_attendances", "org_code": "ABC", "month_date": pd.Timestamp("2024-01-01").date(),
             "method": "pelt_changepoint", "signal_type": "changepoint", "detail": "v1"},
        ]
        write_signals(synthetic_conn, "ae_attendances", "pelt_changepoint", signals)
        write_signals(synthetic_conn, "ae_attendances", "pelt_changepoint", [])  # trust no longer flags

        count = synthetic_conn.execute("SELECT COUNT(*) FROM cp_signals").fetchall()[0][0]
        assert count == 0

    def test_different_methods_do_not_clobber_each_other(self, synthetic_conn):
        rule1 = [
            {"dataset": "ae_attendances", "org_code": "ABC", "month_date": pd.Timestamp("2024-01-01").date(),
             "method": "spc_rule1_3sigma", "signal_type": "beyond_3sigma_above", "detail": "v1"},
        ]
        pelt = [
            {"dataset": "ae_attendances", "org_code": "ABC", "month_date": pd.Timestamp("2024-02-01").date(),
             "method": "pelt_changepoint", "signal_type": "changepoint", "detail": "v1"},
        ]
        write_signals(synthetic_conn, "ae_attendances", "spc_rule1_3sigma", rule1)
        write_signals(synthetic_conn, "ae_attendances", "pelt_changepoint", pelt)

        count = synthetic_conn.execute("SELECT COUNT(*) FROM cp_signals").fetchall()[0][0]
        assert count == 2


# --- Integration tests against the real backfilled DB -------------------------

class TestRealDataIntegration:
    @pytest.fixture(autouse=True)
    def skip_if_no_db(self):
        if not Path(REAL_DB_PATH).exists():
            pytest.skip(f"{REAL_DB_PATH} not found — run etl/run_all.py first")

    def test_spc_baseline_run_produces_valid_signals(self):
        signals = run_spc_baseline(REAL_DB_PATH)
        assert len(signals) > 0
        for s in signals:
            assert s["dataset"] == "ae_attendances"
            assert s["method"] in {"spc_rule1_3sigma", "spc_rule2_run7"}
            assert s["org_code"]
            assert s["month_date"] is not None

    def test_changepoints_run_produces_valid_signals(self):
        signals = run_changepoints(REAL_DB_PATH)
        assert len(signals) > 0
        for s in signals:
            assert s["dataset"] == "ae_attendances"
            assert s["method"] == "pelt_changepoint"
            assert s["signal_type"] == "changepoint"
            assert s["org_code"]

    def test_known_rising_trust_flagged_by_both_methods(self):
        """Trust RXC has a clear ~23% -> ~42% breach-rate rise across the year."""
        conn = get_connection(REAL_DB_PATH)
        try:
            n_trusts = conn.execute(
                "SELECT COUNT(*) FROM fact_ae_attendances WHERE org_code = 'RXC'"
            ).fetchall()[0][0]
        finally:
            conn.close()
        if n_trusts < 6:
            pytest.skip("RXC not present with enough months in this DB snapshot")

        spc_signals = run_spc_baseline(REAL_DB_PATH)
        cp_signals = run_changepoints(REAL_DB_PATH)

        assert any(s["org_code"] == "RXC" for s in spc_signals)
        assert any(s["org_code"] == "RXC" for s in cp_signals)

    def test_run_all_cp_is_idempotent_on_real_db(self):
        run_all_cp(REAL_DB_PATH)
        conn = get_connection(REAL_DB_PATH)
        try:
            first_count = conn.execute("SELECT COUNT(*) FROM cp_signals").fetchall()[0][0]
        finally:
            conn.close()

        run_all_cp(REAL_DB_PATH)
        conn = get_connection(REAL_DB_PATH)
        try:
            second_count = conn.execute("SELECT COUNT(*) FROM cp_signals").fetchall()[0][0]
        finally:
            conn.close()

        assert first_count == second_count
        assert first_count > 0

    def test_all_methods_present_after_full_run(self):
        run_all_cp(REAL_DB_PATH)
        conn = get_connection(REAL_DB_PATH)
        try:
            methods = {row[0] for row in conn.execute("SELECT DISTINCT method FROM cp_signals").fetchall()}
        finally:
            conn.close()
        assert methods == {"spc_rule1_3sigma", "spc_rule2_run7", "pelt_changepoint"}

    def test_get_ae_breach_pct_series_matches_fact_table(self):
        conn = get_connection(REAL_DB_PATH)
        try:
            df = get_ae_breach_pct_series(conn)
            fact_count = conn.execute("SELECT COUNT(*) FROM fact_ae_attendances").fetchall()[0][0]
        finally:
            conn.close()
        assert len(df) == fact_count
        assert list(df.columns) == ["org_code", "org_name", "month_date", "breach_pct"]
