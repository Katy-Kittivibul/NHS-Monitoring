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
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import ruptures as rpt

from cp.signals import (
    ensure_cp_schema_exists,
    get_ae_breach_pct_series,
    get_connection,
    write_signals,
)

DATASET = "ae_attendances"
METHOD = "pelt_changepoint"

MIN_POINTS = 6         # PELT needs enough points either side of a candidate breakpoint to be meaningful
MIN_SEGMENT_SIZE = 3   # minimum months between changepoints

COST_MODEL = "l2"
DEFAULT_PENALTY = None  # None => compute the BIC penalty per-series via bic_penalty()


def bic_penalty(values: np.ndarray) -> float:
    """BIC penalty for l2-cost PELT: var(signal) * log(n)."""
    return float(np.var(values) * np.log(len(values)))


def detect_changepoints(values: np.ndarray, penalty: float | None = None) -> list[int]:
    """Return breakpoint indices (0-based start of each new segment)."""
    pen = penalty if penalty is not None else bic_penalty(values)
    algo = rpt.Pelt(model=COST_MODEL, min_size=MIN_SEGMENT_SIZE, jump=1).fit(values.reshape(-1, 1))
    breakpoints = algo.predict(pen=pen)
    return breakpoints[:-1]  # ruptures always appends len(values) as a final marker, not a real breakpoint


def run_changepoints(db_path: str = "data/processed/nhs.duckdb", penalty: float | None = None) -> list[dict]:
    """Run PELT changepoint detection for every trust and write signals to cp_signals."""
    conn = get_connection(db_path)
    try:
        ensure_cp_schema_exists(conn)
        df = get_ae_breach_pct_series(conn)

        signals = []
        for org_code, g in df.groupby("org_code"):
            g = g.sort_values("month_date")
            dates = g["month_date"].tolist()
            values = g["breach_pct"].to_numpy(dtype=float)
            if len(values) < MIN_POINTS:
                continue

            for idx in detect_changepoints(values, penalty=penalty):
                before_mean = float(np.mean(values[:idx]))
                after_mean = float(np.mean(values[idx:]))
                signals.append({
                    "dataset": DATASET, "org_code": org_code, "month_date": dates[idx],
                    "method": METHOD, "signal_type": "changepoint",
                    "detail": f"mean_before={before_mean:.2f}, mean_after={after_mean:.2f}, "
                              f"n_before={idx}, n_after={len(values) - idx}",
                })

        write_signals(conn, DATASET, METHOD, signals)
        print(f"[OK] Changepoint detection complete: {len(signals)} signals")
        return signals
    finally:
        conn.close()


if __name__ == "__main__":
    run_changepoints()
