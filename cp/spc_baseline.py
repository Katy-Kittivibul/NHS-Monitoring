"""Stage 2 Part A — NHS basic SPC rules per trust, on A&E breach_pct (and RTT % over 18 weeks).

run_spc_baseline() takes a dataset and a `peer` flag: peer=False monitors the trust's own
series, peer=True the trust minus the national weighted value for the month (see
cp/signals.get_series). Limits are estimated per series, so each dataset and each trust has
its own centre line and sigma; nothing is shared between A&E and RTT.

Uses an XmR (Individuals / Moving Range) chart, the standard NHS SPC
methodology for month-on-month metrics like this: the centre line is the
series mean, and sigma is estimated from the average moving range rather
than the naive stdev of the series (naive stdev is inflated by the very
shifts we're trying to detect, since it includes their variance).

Rule 1: a point beyond 3 sigma of the mean.
Rule 2: 7 or more consecutive points on the same side of the mean.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

from cp.signals import (
    MIN_MONTHLY_VOLUME,
    ensure_cp_schema_exists,
    get_connection,
    get_series,
    series_dataset,
    write_signals,
)

METHOD_RULE1 = "spc_rule1_3sigma"
METHOD_RULE2 = "spc_rule2_run7"

MIN_POINTS = 6           # minimum months needed to estimate a stable baseline
MOVING_RANGE_D2 = 1.128  # control-chart constant for n=2 subgroups (XmR chart)
RUN_LENGTH = 7           # NHS SPC "rule 2": 7+ consecutive points same side of mean


def xmr_limits(values: np.ndarray) -> tuple[float, float, float]:
    """Return (centre_line, lower_control_limit, upper_control_limit) for an XmR chart."""
    mean = float(np.mean(values))
    moving_ranges = np.abs(np.diff(values))
    mr_bar = float(np.mean(moving_ranges)) if len(moving_ranges) else 0.0
    sigma = mr_bar / MOVING_RANGE_D2
    return mean, mean - 3 * sigma, mean + 3 * sigma


def rule1_beyond_3sigma(dates, values, mean, lcl, ucl) -> list[tuple]:
    """Every point outside the [LCL, UCL] control limits."""
    signals = []
    for date, val in zip(dates, values):
        if val > ucl:
            signals.append((date, "beyond_3sigma_above",
                             f"value={val:.2f}, mean={mean:.2f}, UCL={ucl:.2f}"))
        elif val < lcl:
            signals.append((date, "beyond_3sigma_below",
                             f"value={val:.2f}, mean={mean:.2f}, LCL={lcl:.2f}"))
    return signals


def rule2_run_of_7(dates, values, mean) -> list[tuple]:
    """Every point from the 7th of a run of same-side-of-mean points onward.

    Points exactly on the mean break a run (standard Western Electric rule
    convention — they count towards neither side).
    """
    signals = []
    run_side = None
    run_start_idx = 0
    for i, val in enumerate(values):
        side = "above" if val > mean else ("below" if val < mean else None)
        if side is None or side != run_side:
            run_side, run_start_idx = side, i
        run_len = i - run_start_idx + 1
        if side is not None and run_len >= RUN_LENGTH:
            signals.append((dates[i], f"run_{side}_mean",
                             f"value={val:.2f}, mean={mean:.2f}, run_length={run_len}"))
    return signals


def run_spc_baseline(db_path: str = "data/processed/nhs.duckdb", dataset: str = "ae_attendances",
                     peer: bool = False) -> list[dict]:
    """Run both SPC rules for every monitored trust and write signals to cp_signals."""
    conn = get_connection(db_path)
    try:
        ensure_cp_schema_exists(conn)
        df = get_series(conn, dataset, peer)
        out_dataset = series_dataset(dataset, peer)
        floor_note = f", min_monthly_volume={MIN_MONTHLY_VOLUME[dataset]}"

        rule1_signals, rule2_signals = [], []
        for org_code, g in df.groupby("org_code"):
            g = g.sort_values("month_date")
            dates = g["month_date"].tolist()
            values = g["value"].to_numpy(dtype=float)
            if len(values) < MIN_POINTS:
                continue

            mean, lcl, ucl = xmr_limits(values)

            for date, signal_type, detail in rule1_beyond_3sigma(dates, values, mean, lcl, ucl):
                rule1_signals.append({
                    "dataset": out_dataset, "org_code": org_code, "month_date": date,
                    "method": METHOD_RULE1, "signal_type": signal_type, "detail": detail + floor_note,
                })
            for date, signal_type, detail in rule2_run_of_7(dates, values, mean):
                rule2_signals.append({
                    "dataset": out_dataset, "org_code": org_code, "month_date": date,
                    "method": METHOD_RULE2, "signal_type": signal_type, "detail": detail + floor_note,
                })

        write_signals(conn, out_dataset, METHOD_RULE1, rule1_signals)
        write_signals(conn, out_dataset, METHOD_RULE2, rule2_signals)

        print(f"[OK] SPC baseline ({out_dataset}): {len(rule1_signals)} rule-1 signals, "
              f"{len(rule2_signals)} rule-2 signals")
        return rule1_signals + rule2_signals
    finally:
        conn.close()


if __name__ == "__main__":
    run_spc_baseline()
