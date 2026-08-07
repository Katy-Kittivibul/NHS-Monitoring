"""Month-over-month delta transform for Stage 3 Tier 3's Question A.

Tier 1/2 correlate raw monthly *levels* (see corr/correlation.py). Question A
correlates *changes* instead — MoM % delta of a trust's breach_pct against
MoM % delta of its ICB's prescribing/dental metric — since the question
being asked is "did the ICB move when the trust's changepoint happened",
which is about co-movement, not shared level/trend.
"""
import pandas as pd


def to_mom_delta(series: pd.DataFrame) -> pd.DataFrame:
    """[key, month_date, value] -> [key, month_date, value], value replaced by
    percentage change vs the immediately preceding *calendar* month for that
    key. Drops the first month of each key (no prior month to diff against),
    any month whose preceding calendar month is missing from the series, and
    any month whose preceding value is zero (percentage change undefined).
    """
    out = []
    for key, group in series.groupby("key"):
        group = group.set_index("month_date").sort_index()
        for month in group.index:
            prev_month = month - pd.DateOffset(months=1)
            if prev_month not in group.index:
                continue
            prev_value = group.loc[prev_month, "value"]
            if prev_value == 0:
                continue
            curr_value = group.loc[month, "value"]
            delta_pct = (curr_value - prev_value) / abs(prev_value) * 100.0
            out.append({"key": key, "month_date": month, "value": delta_pct})
    return pd.DataFrame(out, columns=["key", "month_date", "value"])
