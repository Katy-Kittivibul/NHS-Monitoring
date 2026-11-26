"""Stage 2 Part B — PELT changepoint detection (ruptures) on A&E breach_pct, per trust.

Uses ruptures' PELT (Pruned Exact Linear Time) search with an l2 cost model
and a BIC penalty (var(signal) * log(n)), selecting the number of
changepoints via that penalty rather than specifying it up front — the
right choice here since we don't know in advance whether or how many
regime shifts a given trust has had.

l2+BIC standardised on after comparing it against rbf+1.2 across 10 trusts
(R1H, R0A, plus 8 more spanning low/high variance): 3 wins / 4 ties / 0
losses for l2+BIC, with rbf+1.2 missing real shifts on R0A, G0Q0L, and
R1K. Reverted from the earlier rbf+1.2 configuration for that reason.

Series length is whatever is loaded (about 30 monthly points per trust for A&E and RTT
today); nothing below hard-codes it. Shorter series are the weak spot: the BIC penalty is
estimated from the same few points it regularises, and with MIN_SEGMENT_SIZE = 3 a series of n
points admits at most about n/3 changepoints, so a detected changepoint on a short series is
provisional and an undetected one may be a power problem. A changepoint on a trust's own series
can also just be shared seasonality or a national shock; cp/signals.get_series(peer=True) runs
the same detector on trust-minus-national, and vw_changepoints.is_robust marks the raw
changepoints the peer-relative series confirms.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import ruptures as rpt

from cp.signals import (
    MIN_MONTHLY_VOLUME,
    ensure_cp_schema_exists,
    get_connection,
    get_series,
    series_dataset,
    write_signals,
)

METHOD = "pelt_changepoint"

# Both are load-bearing on short series (see above): MIN_POINTS excludes trusts with fewer than
# 6 months, and MIN_SEGMENT_SIZE caps how many changepoints a series can hold. Neither has been
# tuned against a longer series.
MIN_POINTS = 6         # PELT needs enough points either side of a candidate breakpoint to be meaningful
MIN_SEGMENT_SIZE = 3   # minimum months between changepoints

COST_MODEL = "l2"
DEFAULT_PENALTY = None  # None => compute the BIC penalty per-series via bic_penalty()


# Calibration (seeded simulation, tests/test_inference.py::TestPenaltyCalibration): on pure white
# noise, a penalty of var * log(n) reports a spurious changepoint in ~50-60% of series at n = 13
# and n = 30; 2 * var * log(n) cuts that to ~8-9% while still finding every shift of 2+ standard
# deviations. The factor 2 is the textbook BIC form for a mean shift (a changepoint adds a new
# segment mean AND a new location), so it is used here. var(signal) over-estimates the noise
# variance when a real shift is present, which makes the rule slightly conservative, not liberal.
PENALTY_MULTIPLIER = 2.0


def bic_penalty(values: np.ndarray) -> float:
    """BIC penalty for l2-cost PELT: PENALTY_MULTIPLIER * var(signal) * log(n)."""
    return float(PENALTY_MULTIPLIER * np.var(values) * np.log(len(values)))


def detect_changepoints(values: np.ndarray, penalty: float | None = None) -> list[int]:
    """Return breakpoint indices (0-based start of each new segment)."""
    pen = penalty if penalty is not None else bic_penalty(values)
    algo = rpt.Pelt(model=COST_MODEL, min_size=MIN_SEGMENT_SIZE, jump=1).fit(values.reshape(-1, 1))
    breakpoints = algo.predict(pen=pen)
    return breakpoints[:-1]  # ruptures always appends len(values) as a final marker, not a real breakpoint


def run_changepoints(db_path: str = "data/processed/nhs.duckdb", penalty: float | None = None,
                     dataset: str = "ae_attendances", peer: bool = False) -> list[dict]:
    """Run PELT changepoint detection for every monitored trust and write signals to cp_signals.

    The BIC penalty comes from each series' own variance, so an RTT series (different level and
    spread to A&E) and a peer-relative series each get their own penalty."""
    conn = get_connection(db_path)
    try:
        ensure_cp_schema_exists(conn)
        df = get_series(conn, dataset, peer)
        out_dataset = series_dataset(dataset, peer)

        signals = []
        for org_code, g in df.groupby("org_code"):
            g = g.sort_values("month_date")
            dates = g["month_date"].tolist()
            values = g["value"].to_numpy(dtype=float)
            if len(values) < MIN_POINTS:
                continue

            for idx in detect_changepoints(values, penalty=penalty):
                before_mean = float(np.mean(values[:idx]))
                after_mean = float(np.mean(values[idx:]))
                signals.append({
                    "dataset": out_dataset, "org_code": org_code, "month_date": dates[idx],
                    "method": METHOD, "signal_type": "changepoint",
                    "detail": f"mean_before={before_mean:.2f}, mean_after={after_mean:.2f}, "
                              f"n_before={idx}, n_after={len(values) - idx}, "
                              f"min_monthly_volume={MIN_MONTHLY_VOLUME[dataset]}",
                })

        write_signals(conn, out_dataset, METHOD, signals)
        print(f"[OK] Changepoint detection ({out_dataset}): {len(signals)} signals")
        return signals
    finally:
        conn.close()


if __name__ == "__main__":
    run_changepoints()
