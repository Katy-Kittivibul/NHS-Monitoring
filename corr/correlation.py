"""Stage 3 correlation engine — the rolling-window Pearson correlation both
Tier 1 (region) and Tier 2 (trust) share.

"Month-over-month correlation between trend A and trend B" is implemented as
a trailing 6-calendar-month rolling Pearson correlation over raw monthly
levels (matches cp/'s existing MIN_POINTS=6 convention): for each candidate
month, all 6 required consecutive calendar months (month, month-1, ...,
month-5) must be present with paired data in both series, or that month is
skipped entirely — no padding or interpolation across gaps.

Tier 1/2 feed this *first differences* of the monthly series (see
to_first_difference), not levels. Two short monthly series that both trend, or
share seasonality, correlate strongly by construction and their Pearson
p-values are badly over-confident (the points are autocorrelated, so the
effective sample size is far below n). Differencing removes the shared trend;
the six differences in a window therefore need seven consecutive months of
levels. Each result also carries the 95% Fisher-z confidence interval for r,
which with n=6 is very wide — that width is the honest message, so it is
reported next to the point estimate rather than hidden behind a p-value.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np
import pandas as pd
from scipy.stats import norm, pearsonr

WINDOW_MONTHS = 6
CI_LEVEL = 0.95


def fisher_ci(r: float, n: int, level: float = CI_LEVEL) -> tuple[float | None, float | None]:
    """Fisher-z confidence interval for a Pearson r from n pairs (None when n <= 3 or |r| == 1)."""
    if n <= 3 or abs(r) >= 1.0:
        return None, None
    half = norm.ppf(0.5 + level / 2) / np.sqrt(n - 3)
    centre = np.arctanh(r)
    return float(np.tanh(centre - half)), float(np.tanh(centre + half))


def to_first_difference(series: pd.DataFrame) -> pd.DataFrame:
    """[key, month_date, value] -> same shape, value = this month minus the preceding *calendar*
    month. A month whose predecessor is missing is dropped (no interpolation across gaps)."""
    out = []
    for key, group in series.groupby("key"):
        values = group.drop_duplicates("month_date").set_index("month_date")["value"].sort_index()
        previous = values.copy()
        previous.index = previous.index + pd.DateOffset(months=1)
        joined = pd.concat([values.rename("curr"), previous.rename("prev")], axis=1, join="inner")
        for month, row in joined.iterrows():
            out.append({"key": key, "month_date": month, "value": float(row["curr"] - row["prev"])})
    return pd.DataFrame(out, columns=["key", "month_date", "value"])


def rolling_correlation(
    series_a: pd.DataFrame, series_b: pd.DataFrame, window: int = WINDOW_MONTHS
) -> list[dict]:
    """Correlate two [key, month_date, value] series on a trailing `window`-month
    calendar window per key. Returns a list of
    {key, month_date, correlation_metric, p_value, n_obs, ci_low, ci_high, detail} dicts, one per (key, month)
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
            ci_low, ci_high = fisher_ci(float(r), window)
            results.append({
                "key": key,
                "month_date": month,
                "correlation_metric": float(r),
                "p_value": float(p),
                "n_obs": window,
                "ci_low": ci_low,
                "ci_high": ci_high,
                "detail": f"r={r:.3f}, p={p:.3f}, n={window}",
            })

    return results
