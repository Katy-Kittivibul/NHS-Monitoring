"""Phase 2: multiple-testing control, first-difference correlation, peer-relative changepoints.

Everything here is hermetic: synthetic data only, fixed seeds.
"""
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest
from scipy.stats import pearsonr

sys.path.insert(0, str(Path(__file__).parent.parent))

from corr.correlation import fisher_ci, rolling_correlation, to_first_difference
from corr.inference import ALPHA, annotate_fdr, benjamini_hochberg
from corr.signals import SIGNAL_COLUMNS, ensure_corr_schema_exists, signal_row, write_corr_signals
from corr.tier3_signal_driven import get_data_anchor
from cp.changepoints import detect_changepoints
from cp.signals import MIN_MONTHLY_VOLUME, get_series
from etl.common import ensure_schema_exists


def _months(n, start="2024-01-01"):
    return list(pd.date_range(start, periods=n, freq="MS"))


# --- Benjamini-Hochberg ---------------------------------------------------------------

class TestBenjaminiHochberg:
    def test_matches_a_hand_computed_example(self):
        # sorted p: .005 .01 .03 .04 (m=4) -> p*m/rank = .02 .02 .04 .04; the running minimum
        # from the top leaves .02 .02 .04 .04
        q = benjamini_hochberg([0.01, 0.04, 0.03, 0.005])
        assert q == pytest.approx([0.02, 0.04, 0.04, 0.02])

    def test_running_minimum_enforces_monotonicity(self):
        # p*m/rank = .04 .03 .04 .04 .. a later, smaller adjusted value pulls earlier ones down
        q = benjamini_hochberg([0.01, 0.015, 0.03, 0.04])
        assert q == pytest.approx([0.03, 0.03, 0.04, 0.04])

    def test_q_is_never_below_p_and_stays_in_unit_interval(self):
        rng = np.random.RandomState(1)
        p = rng.uniform(size=500)
        q = benjamini_hochberg(p)
        assert np.all(q >= p - 1e-12)
        assert np.all((q >= 0) & (q <= 1))

    def test_order_of_input_does_not_matter(self):
        p = np.array([0.2, 0.001, 0.5, 0.03, 0.04])
        shuffled = np.array([0.04, 0.5, 0.03, 0.001, 0.2])
        q_by_p = dict(zip(p, benjamini_hochberg(p)))
        for value, q in zip(shuffled, benjamini_hochberg(shuffled)):
            assert q == pytest.approx(q_by_p[value])

    def test_nan_is_not_counted_in_the_family_size(self):
        q = benjamini_hochberg([0.01, np.nan, 0.04])
        assert np.isnan(q[1])
        assert q[0] == pytest.approx(0.02)  # m = 2, not 3

    def test_empty_and_all_nan(self):
        assert benjamini_hochberg([]).size == 0
        assert np.isnan(benjamini_hochberg([np.nan, np.nan])).all()

    def test_annotate_fdr_uses_one_family_per_key(self):
        rows = [{"month": 1, "p_value": 0.01}, {"month": 1, "p_value": 0.04},
                {"month": 2, "p_value": 0.01}, {"p_value": None, "month": 2}]
        annotate_fdr(rows, family_keys=("month",))
        assert rows[0]["q_value"] == pytest.approx(0.02)   # family of 2
        assert rows[2]["q_value"] == pytest.approx(0.01)   # family of 1: month 2's NaN is not counted
        assert rows[3]["q_value"] is None and rows[3]["is_significant_fdr"] is False
        assert rows[0]["is_significant_fdr"] is True
        assert rows[1]["is_significant_fdr"] == (rows[1]["q_value"] <= ALPHA)


class TestNullBehaviour:
    """Independent, trending, autocorrelated series: no real relationship exists, so any
    'significant' result is a false positive."""

    @staticmethod
    def _null_family(rng, n_geographies=100, months=7):
        def series():
            noise = np.zeros((n_geographies, months))
            for t in range(1, months):
                noise[:, t] = 0.8 * noise[:, t - 1] + rng.normal(size=n_geographies)
            return noise + rng.uniform(-1, 1, size=(n_geographies, 1)) * np.arange(months)  # a trend each
        return series(), series()

    def test_fdr_on_first_differences_holds_the_false_discovery_rate(self):
        rng = np.random.RandomState(20260101)
        families, with_discovery = 300, 0
        for _ in range(families):
            a, b = self._null_family(rng)
            p = [pearsonr(np.diff(x), np.diff(y))[1] for x, y in zip(a, b)]
            with_discovery += bool((benjamini_hochberg(p) <= ALPHA).any())
        # Under the complete null BH's FDR equals P(at least one discovery); allow binomial noise
        # (SE ~ 0.013 at 300 families) rather than asserting exactly 0.05.
        assert with_discovery / families <= 0.08

    def test_naive_level_correlation_is_badly_over_confident(self):
        rng = np.random.RandomState(20260101)
        hits = total = 0
        for _ in range(30):
            a, b = self._null_family(rng, months=6)
            for x, y in zip(a, b):
                hits += pearsonr(x, y)[1] < ALPHA
                total += 1
        assert hits / total > 0.20  # four times the nominal 5%: why levels are not used


# --- first differences and intervals ------------------------------------------------------

class TestFirstDifference:
    def test_differences_the_preceding_calendar_month(self):
        s = pd.DataFrame({"key": "A", "month_date": _months(4), "value": [1.0, 3.0, 2.0, 7.0]})
        out = to_first_difference(s)
        assert out["value"].tolist() == [2.0, -1.0, 5.0]
        assert out["month_date"].tolist() == _months(4)[1:]

    def test_a_gap_drops_the_month_after_it_rather_than_bridging(self):
        months = _months(5)
        s = pd.DataFrame({"key": "A", "month_date": [months[0], months[1], months[3], months[4]],
                          "value": [1.0, 2.0, 10.0, 11.0]})
        out = to_first_difference(s)
        assert out["month_date"].tolist() == [months[1], months[4]]

    def test_keys_are_independent_and_a_shared_trend_cancels(self):
        months = _months(8)
        trend_a = pd.DataFrame({"key": "A", "month_date": months, "value": np.arange(8) * 2.0})
        trend_b = pd.DataFrame({"key": "A", "month_date": months, "value": np.arange(8) * 5.0 + 3})
        # as levels these correlate perfectly; their changes are constant, so no correlation exists
        levels = rolling_correlation(trend_a, trend_b, window=6)
        assert levels and levels[0]["correlation_metric"] == pytest.approx(1.0)
        assert rolling_correlation(to_first_difference(trend_a), to_first_difference(trend_b)) == []

    def test_result_carries_inference_fields(self):
        months = _months(8)
        rng = np.random.RandomState(3)
        a = pd.DataFrame({"key": "A", "month_date": months, "value": rng.normal(size=8)})
        b = pd.DataFrame({"key": "A", "month_date": months, "value": rng.normal(size=8)})
        for row in rolling_correlation(to_first_difference(a), to_first_difference(b)):
            assert 0 <= row["p_value"] <= 1 and row["n_obs"] == 6
            assert row["ci_low"] <= row["correlation_metric"] <= row["ci_high"]
            assert f"p={row['p_value']:.3f}" in row["detail"]


class TestFisherCi:
    def test_known_value(self):
        # r = 0.5, n = 30: z = 0.5493, se = 1/sqrt(27) -> (0.1696, 0.7291)
        low, high = fisher_ci(0.5, 30)
        assert (low, high) == pytest.approx((0.1696, 0.7291), abs=1e-3)

    def test_small_n_gives_a_very_wide_interval(self):
        low, high = fisher_ci(0.6, 6)
        assert high - low > 1.2

    def test_undefined_cases(self):
        assert fisher_ci(0.5, 3) == (None, None)
        assert fisher_ci(1.0, 10) == (None, None)


# --- storage ---------------------------------------------------------------------------

class TestInferenceColumnsAreStored:
    def test_old_table_is_migrated_and_new_rows_round_trip(self):
        conn = duckdb.connect(":memory:")
        conn.execute("""CREATE TABLE corr_signals (
            tier VARCHAR, dataset_pair VARCHAR, geography_level VARCHAR, geography_code VARCHAR,
            month_date DATE, correlation_metric DOUBLE, detail VARCHAR,
            PRIMARY KEY (tier, dataset_pair, geography_code, month_date))""")
        ensure_corr_schema_exists(conn)  # must ADD the missing columns, not fail
        cols = [r[0] for r in conn.execute("DESCRIBE corr_signals").fetchall()]
        assert cols == SIGNAL_COLUMNS

        result = {"key": "R1", "month_date": pd.Timestamp("2025-01-01"), "correlation_metric": 0.4,
                  "detail": "r=0.400, p=0.5, n=6", "p_value": 0.5, "n_obs": 6, "ci_low": -0.7,
                  "ci_high": 0.9}
        row = signal_row("tier2_trust", "ae_rtt_trust", "trust", result)
        annotate_fdr([row], family_keys=("tier", "dataset_pair", "month_date"))
        write_corr_signals(conn, "tier2_trust", "ae_rtt_trust", [row])
        stored = conn.execute(
            "SELECT p_value, n_obs, q_value, is_significant_fdr FROM corr_signals").fetchone()
        assert stored == (0.5, 6, 0.5, False)


# --- Tier 3 anchor ---------------------------------------------------------------------

def test_data_anchor_follows_the_loaded_data():
    conn = duckdb.connect(":memory:")
    ensure_schema_exists(conn)
    with pytest.raises(RuntimeError):
        get_data_anchor(conn)  # nothing loaded
    conn.execute("INSERT INTO fact_ae_attendances (month_date, org_code) VALUES ('2025-03-01', 'R1')")
    assert get_data_anchor(conn) == pd.Timestamp("2025-03-01")
    conn.execute("INSERT INTO fact_ae_attendances (month_date, org_code) VALUES ('2026-08-01', 'R1')")
    assert get_data_anchor(conn) == pd.Timestamp("2026-08-01")


# --- peer-relative changepoints -----------------------------------------------------------

def _ae_conn(step_trust=None, n_trusts=20, months=36, seed=7, tiny_trust=False):
    """Every trust shares a seasonal wave and a national step at month 25; optionally one trust steps up."""
    rng = np.random.RandomState(seed)
    dates = _months(months)
    season = 1.5 * np.sin(2 * np.pi * np.arange(months) / 12) + 5 * (np.arange(months) > 24)
    conn = duckdb.connect(":memory:")
    ensure_schema_exists(conn)
    rows = []
    for i in range(n_trusts):
        code = f"R{i:02d}"
        level = 20 + rng.uniform(-3, 3)
        for t, month in enumerate(dates):
            attendances = 100 if (tiny_trust and i == 0) else 10_000
            pct = level + season[t] + rng.normal(scale=0.6)
            if code == step_trust and t >= 18:
                pct += 9
            rows.append((month.date(), code, attendances, round(attendances * pct / 100), pct))
    frame = pd.DataFrame(rows, columns=["month_date", "org_code", "total_attendances",
                                        "total_breaches_4hr", "breach_pct"])
    conn.register("frame", frame)
    conn.execute("""INSERT INTO fact_ae_attendances (month_date, org_code, total_attendances,
                    total_breaches_4hr, breach_pct) SELECT * FROM frame""")
    return conn


def _changepoints(df):
    return {code: detect_changepoints(g.sort_values("month_date")["value"].to_numpy(dtype=float))
            for code, g in df.groupby("org_code")}


class TestPeerRelativeSeries:
    def test_a_shared_national_shift_is_not_a_trust_changepoint_once_peers_are_removed(self):
        conn = _ae_conn()
        raw = _changepoints(get_series(conn, "ae_attendances", peer=False))
        peer = _changepoints(get_series(conn, "ae_attendances", peer=True))
        raw_hits = sum(bool(v) for v in raw.values())
        peer_hits = sum(bool(v) for v in peer.values())
        assert raw_hits >= 15  # the national step looks like a trust-level shift in almost every raw series
        assert peer_hits <= 6  # and cancels against the national value (noise alone allows a few)
        assert peer_hits < raw_hits / 2

    def test_a_real_step_survives_peer_adjustment(self):
        conn = _ae_conn(step_trust="R05")
        peer = _changepoints(get_series(conn, "ae_attendances", peer=True))
        assert any(abs(idx - 18) <= 1 for idx in peer["R05"])

    def test_peer_values_are_percentage_points_around_zero(self):
        conn = _ae_conn()
        peer = get_series(conn, "ae_attendances", peer=True)
        assert abs(peer["value"].mean()) < 0.5
        assert peer.groupby("month_date")["value"].mean().abs().max() < 0.5

    def test_low_volume_trusts_are_not_monitored(self):
        conn = _ae_conn(tiny_trust=True)
        for peer in (False, True):
            assert "R00" not in set(get_series(conn, "ae_attendances", peer)["org_code"])
        assert MIN_MONTHLY_VOLUME["ae_attendances"] > 100

    def test_rtt_series_uses_c999_over_18_weeks(self):
        conn = duckdb.connect(":memory:")
        ensure_schema_exists(conn)
        conn.execute("""INSERT INTO fact_rtt_waits (month_date, org_code, specialty_code, waiting_list_size,
                        over_18wk_count) VALUES
            ('2025-01-01', 'R1', 'C_999', 1000, 300), ('2025-01-01', 'R1', 'C_100', 500, 100),
            ('2025-01-01', 'R2', 'C_999', 1000, 100)""")
        raw = get_series(conn, "rtt_waits", peer=False).set_index("org_code")["value"]
        assert raw.to_dict() == {"R1": pytest.approx(30.0), "R2": pytest.approx(10.0)}
        peer = get_series(conn, "rtt_waits", peer=True).set_index("org_code")["value"]
        assert peer.to_dict() == {"R1": pytest.approx(10.0), "R2": pytest.approx(-10.0)}  # national = 20%
