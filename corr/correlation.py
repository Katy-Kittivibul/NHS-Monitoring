"""Stage 3 correlation engine — the rolling-window Pearson correlation both
Tier 1 (region) and Tier 2 (trust) share.

"Month-over-month correlation between trend A and trend B" is implemented as
a trailing 6-calendar-month rolling Pearson correlation over raw monthly
levels (matches cp/'s existing MIN_POINTS=6 convention): for each candidate
month, all 6 required consecutive calendar months (month, month-1, ...,
month-5) must be present with paired data in both series, or that month is
skipped entirely — no padding or interpolation across gaps.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np
import pandas as pd
from scipy.stats import pearsonr

WINDOW_MONTHS = 6


def rolling_correlation(
    series_a: pd.DataFrame, series_b: pd.DataFrame, window: int = WINDOW_MONTHS
) -> list[dict]:
    """Correlate two [key, month_date, value] series on a trailing `window`-month
    calendar window per key. Returns a list of
    {key, month_date, correlation_metric, detail} dicts, one per (key, month)
    where a full window of paired data exists.
    """
    merged = pd.merge(series_a, series_b, on=["key", "month_date"], suffixes=("_a", "_b"))
    if merged.empty:
        return []

    results = []
    for key, group in merged.groupby("key"):
        group = group.set_index("month_date").sort_index()
        available_months = set(group.index)

        for month in group.index:
            required_months = [month - pd.DateOffset(months=i) for i in range(window)]
            if not all(m in available_months for m in required_months):
                continue

            window_df = group.loc[required_months]
            values_a = window_df["value_a"].to_numpy(dtype=float)
            values_b = window_df["value_b"].to_numpy(dtype=float)

            if np.std(values_a) == 0 or np.std(values_b) == 0:
                continue  # a constant series has no defined correlation — skip rather than emit NaN

            r, p = pearsonr(values_a, values_b)
            results.append({
                "key": key,
                "month_date": month,
                "correlation_metric": float(r),
                "detail": f"r={r:.3f}, p={p:.3f}, n={window}",
            })

    return results
