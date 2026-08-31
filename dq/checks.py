"""Shared primitives for Stage 1 data quality drift checks.

Each check compares a fact table's month-over-month behaviour against the
prior month present in that table (not necessarily the prior calendar
month — some datasets have gaps) and returns a list of flag dicts ready
for `write_flags`. A bad value produces a FAIL flag, not an exception.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd

from etl.common import get_connection  # noqa: E402 — reuse the existing connection factory

__all__ = [
    "get_connection",
    "ensure_dq_schema_exists",
    "write_flags",
    "check_row_count_jump",
    "check_null_rate_jump",
    "check_out_of_range",
    "check_org_missing_vs_prior_month",
    "check_org_never_reported",
]


def ensure_dq_schema_exists(conn):
    """Create the dq_flags table if it doesn't already exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dq_flags (
            dataset VARCHAR,
            month_date DATE,
            check_name VARCHAR,
            status VARCHAR,
            detail VARCHAR,
            PRIMARY KEY (dataset, month_date, check_name)
        )
    """)


def write_flags(conn, flags: list[dict]):
    """Idempotently write a batch of flags for a single dataset.

    Replaces all existing dq_flags rows for that dataset, since each dq run
    recomputes checks for every month currently present in the fact table.
    """
    if not flags:
        return
    df = pd.DataFrame(flags)
    dataset = df["dataset"].iloc[0]
    conn.execute("DELETE FROM dq_flags WHERE dataset = ?", [dataset])
    conn.execute("INSERT INTO dq_flags SELECT * FROM df")


def _month_gap(prev_month, month) -> int:
    """Number of calendar months between two month-start dates."""
    return (month.year - prev_month.year) * 12 + (month.month - prev_month.month)


def check_row_count_jump(conn, dataset, table, month_col="month_date",
                          warn_threshold_pct=15.0, fail_threshold_pct=30.0):
    """Flag months where row count jumps sharply vs the prior month present."""
    counts = conn.execute(f"""
        SELECT {month_col} AS month_date, COUNT(*) AS row_count
        FROM {table}
        GROUP BY {month_col}
        ORDER BY {month_col}
    """).df()

    flags = []
    prev_count = None
    prev_month = None
    for _, row in counts.iterrows():
        month, count = row["month_date"], int(row["row_count"])
        if prev_count is None:
            status, detail = "OK", f"row_count={count} (no prior month to compare)"
        else:
            pct_change = ((count - prev_count) / prev_count * 100.0) if prev_count else 0.0
            if abs(pct_change) > fail_threshold_pct:
                status = "FAIL"
            elif abs(pct_change) > warn_threshold_pct:
                status = "WARN"
            else:
                status = "OK"
            detail = f"row_count={count}, prior_month={prev_count}, change={pct_change:+.1f}%"
            gap = _month_gap(prev_month, month)
            if gap > 1:
                detail += f" [gap: {gap} months]"
        flags.append({"dataset": dataset, "month_date": month, "check_name": "row_count_jump",
                       "status": status, "detail": detail})
        prev_count, prev_month = count, month
    return flags


def check_null_rate_jump(conn, dataset, table, columns, month_col="month_date",
                          warn_threshold_pp=5.0, fail_threshold_pp=15.0):
    """Flag months where a column's null rate jumps sharply vs the prior month."""
    flags = []
    for col in columns:
        rates = conn.execute(f"""
            SELECT {month_col} AS month_date,
                   COUNT(*) AS total_rows,
                   SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) AS null_rows
            FROM {table}
            GROUP BY {month_col}
            ORDER BY {month_col}
        """).df()

        check_name = f"null_rate_jump_{col}"
        prev_rate = None
        prev_month = None
        for _, row in rates.iterrows():
            month, total, nulls = row["month_date"], int(row["total_rows"]), int(row["null_rows"])
            rate = (nulls / total * 100.0) if total else 0.0
            if prev_rate is None:
                status, detail = "OK", f"null_rate={rate:.1f}% (no prior month to compare)"
            else:
                delta = rate - prev_rate
                if abs(delta) > fail_threshold_pp:
                    status = "FAIL"
                elif abs(delta) > warn_threshold_pp:
                    status = "WARN"
                else:
                    status = "OK"
                detail = f"null_rate={rate:.1f}%, prior_month={prev_rate:.1f}%, delta={delta:+.1f}pp"
                gap = _month_gap(prev_month, month)
                if gap > 1:
                    detail += f" [gap: {gap} months]"
            flags.append({"dataset": dataset, "month_date": month, "check_name": check_name,
                           "status": status, "detail": detail})
            prev_rate, prev_month = rate, month
    return flags


def check_out_of_range(conn, dataset, table, column, min_val=None, max_val=None, month_col="month_date"):
    """Flag months where any row has `column` outside [min_val, max_val]."""
    if min_val is None and max_val is None:
        raise ValueError("check_out_of_range requires at least one of min_val/max_val")

    conditions = []
    if min_val is not None:
        conditions.append(f"{column} < {min_val}")
    if max_val is not None:
        conditions.append(f"{column} > {max_val}")
    out_of_range_expr = " OR ".join(conditions)

    counts = conn.execute(f"""
        SELECT {month_col} AS month_date,
               COUNT(*) AS total_rows,
               SUM(CASE WHEN {out_of_range_expr} THEN 1 ELSE 0 END) AS bad_rows
        FROM {table}
        GROUP BY {month_col}
        ORDER BY {month_col}
    """).df()

    check_name = f"out_of_range_{column}"
    bounds = f"[{min_val if min_val is not None else '-inf'}, {max_val if max_val is not None else '+inf'}]"
    flags = []
    for _, row in counts.iterrows():
        month, total = row["month_date"], int(row["total_rows"])
        bad = int(row["bad_rows"]) if pd.notna(row["bad_rows"]) else 0
        status = "FAIL" if bad > 0 else "OK"
        detail = f"{bad}/{total} rows outside {bounds}"
        flags.append({"dataset": dataset, "month_date": month, "check_name": check_name,
                       "status": status, "detail": detail})
    return flags


def check_org_missing_vs_prior_month(conn, dataset, table, org_col, month_col="month_date"):
    """Flag any organisation present in the prior month's roster but absent
    this month.

    check_row_count_jump only sees the aggregate row count, so one
    organisation's rows silently disappearing reads as noise against
    everyone else's (e.g. a single trust missing from ~200 is a ~0.5%
    change, nowhere near either threshold). This check compares rosters
    month-to-month directly instead, so a single organisation dropping out
    is always visible regardless of how many others are still reporting.
    """
    rosters = conn.execute(f"""
        SELECT DISTINCT {org_col} AS org_code, {month_col} AS month_date
        FROM {table}
        WHERE {org_col} IS NOT NULL
    """).df()

    flags = []
    prev_roster = None
    for month, group in rosters.groupby("month_date"):
        roster = set(group["org_code"])
        if prev_roster is None:
            status, detail = "OK", f"{len(roster)} orgs (no prior month to compare)"
        else:
            missing = sorted(prev_roster - roster)
            if missing:
                status = "FAIL"
                shown = ", ".join(missing[:10]) + ("..." if len(missing) > 10 else "")
                detail = f"{len(missing)} org(s) present last month missing this month: {shown}"
            else:
                status, detail = "OK", f"{len(roster)} orgs, none missing vs prior month"
        flags.append({"dataset": dataset, "month_date": month, "check_name": "org_missing_vs_prior_month",
                       "status": status, "detail": detail})
        prev_roster = roster
    return flags


def check_org_never_reported(conn, dataset, table, org_col, dim_org_type, month_col="month_date"):
    """Flag organisations that are in dim_organisation (of dim_org_type) but
    have zero rows in `table` across its entire date range.

    A complete no-show, distinct from check_org_missing_vs_prior_month's
    transient single-month gaps. Not a per-month comparison, so the flag is
    anchored to the table's most recent month.
    """
    missing = conn.execute(f"""
        SELECT org_code
        FROM dim_organisation
        WHERE org_type = ?
          AND org_code NOT IN (SELECT DISTINCT {org_col} FROM {table} WHERE {org_col} IS NOT NULL)
        ORDER BY org_code
    """, [dim_org_type]).df()

    latest_month = conn.execute(f"SELECT MAX({month_col}) FROM {table}").fetchall()[0][0]
    if latest_month is None:
        return []

    if missing.empty:
        status = "OK"
        detail = f"all dim_organisation org_type='{dim_org_type}' orgs have appeared in {table}"
    else:
        status = "FAIL"
        codes = missing["org_code"].tolist()
        shown = ", ".join(codes[:10]) + ("..." if len(codes) > 10 else "")
        detail = (
            f"{len(codes)} org(s) in dim_organisation (org_type='{dim_org_type}') "
            f"never appear in {table}: {shown}"
        )

    return [{"dataset": dataset, "month_date": latest_month, "check_name": "org_never_reported",
             "status": status, "detail": detail}]
