import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import re
from datetime import datetime

import pandas as pd

from etl.common import (  # noqa: E402 — after the sys.path shim above
    ensure_schema_exists,
    get_connection,
    ingest_files,
    normalise_region,
    replace_months,
    safe_pct,
    to_float_series,
    to_int_frame,
    to_int_series,
    upsert_organisations,
)


def parse_rtt_filename_date(filename: str) -> datetime:
    """Extract date from RTT filename like 'rtt_incomplete_provider_202403.xlsx'."""
    match = re.search(r"(\d{6})", filename)
    if match:
        date_str = match.group(1)
        year = int(date_str[:4])
        month = int(date_str[4:6])
        return datetime(year, month, 1).date()
    raise ValueError(f"Cannot parse date from filename: {filename}")


def clean_rtt_value(val) -> int:
    """Convert RTT column values to numbers, handling dashes and NaN."""
    if pd.isna(val) or val == "-" or val == "":
        return 0
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


def load_rtt_waits(xlsx_path: str = "data/raw/rtt_incomplete_provider_202403.xlsx"):
    """Load and transform RTT waiting times data."""
    # Headers start at row 13 (0-indexed line 13) in Provider sheet
    df = pd.read_excel(xlsx_path, sheet_name=0, header=13, engine="calamine")


    # Drop unnamed columns (e.g. margin columns)
    df = df.loc[:, ~df.columns.astype(str).str.contains('^Unnamed')].copy()

    # Strip whitespace from column names
    df.columns = df.columns.astype(str).str.strip()

    # Rename key columns for consistency
    df = df.rename(columns={
        "Provider Code": "org_code",
        "Provider Name": "org_name",
        "Region Code": "region_code",
        "Treatment Function Code": "specialty_code",
        "Treatment Function": "specialty_name"
    })

    # Filter out empty rows or title rows
    df = df.dropna(subset=["org_code", "specialty_code"]).copy()
    df = df[df["org_code"] != "Provider Code"].copy()

    # Extract month date from filename
    filename = Path(xlsx_path).name
    month_date = parse_rtt_filename_date(filename)
    df["month_date"] = month_date

    # Calculate waiting_list_size from Total column (if exists) or sum of week bins
    total_cols = [c for c in df.columns if "Total number of incomplete pathways" in c or c == "Total"]
    if total_cols:
        df["waiting_list_size"] = to_int_series(df[total_cols[0]])
    else:
        week_cols = [c for c in df.columns if c not in [
            "month_date", "org_code", "org_name", "region_code",
            "specialty_code", "specialty_name"
        ]]
        df["waiting_list_size"] = to_int_frame(df[week_cols]).sum(axis=1).astype("int64")

    # Calculate over_18wk_count (sum of bins >18 weeks)
    over_18wk_cols = []
    for c in df.columns:
        if c.startswith(">"):
            try:
                start_wk = int(c.split("-")[0][1:])
                if start_wk >= 18:
                    over_18wk_cols.append(c)
            except ValueError:
                pass
        elif "104 plus" in c:
            over_18wk_cols.append(c)

    if over_18wk_cols:
        df["over_18wk_count"] = to_int_frame(df[over_18wk_cols]).sum(axis=1).astype("int64")
    else:
        df["over_18wk_count"] = 0

    # Calculate over_18wk_pct (avoid division by zero)
    df["over_18wk_pct"] = safe_pct(df["over_18wk_count"], df["waiting_list_size"])

    # Pathways waiting 52+ weeks: the workbook's own "Total 52 plus weeks" column, else the
    # sum of the >52-53 ... 104 plus bins.
    if "Total 52 plus weeks" in df.columns:
        df["over_52wk_count"] = to_int_series(df["Total 52 plus weeks"])
    else:
        over_52_cols = [c for c in over_18wk_cols if "104 plus" in c or int(c.split("-")[0][1:]) >= 52]
        if over_52_cols:
            df["over_52wk_count"] = to_int_frame(df[over_52_cols]).sum(axis=1).astype("int64")
        else:
            df["over_52wk_count"] = 0

    # Median wait weeks
    median_cols = [c for c in df.columns if "median" in c.lower() or "50th" in c.lower()]
    if median_cols:
        df["median_wait_weeks"] = to_float_series(df[median_cols[0]])
    else:
        df["median_wait_weeks"] = 0.0

    # Select and order columns
    result = df[[
        "month_date", "org_code", "org_name", "region_code",
        "specialty_code", "specialty_name", "waiting_list_size",
        "over_18wk_count", "over_18wk_pct", "median_wait_weeks",
        "over_52wk_count"
    ]].copy()

    return result


def _write_rtt(conn, df):
    replace_months(conn, "fact_rtt_waits", df)

    # Update dim_organisation with providers from this dataset
    org_rows = df[["org_code", "org_name", "region_code"]].drop_duplicates("org_code", keep="last").copy()
    raw_region = org_rows["region_code"]
    regions = raw_region.map(normalise_region)
    org_rows["region_code"] = regions.map(lambda pair: pair[0])
    # Unrecognised values are kept (stripped) in region_name rather than dropped silently.
    org_rows["region_name"] = [
        pair[1] or (str(raw).strip() if pd.notna(raw) else None)
        for pair, raw in zip(regions, raw_region)
    ]
    org_rows["org_type"] = "trust"
    upsert_organisations(conn, org_rows, ["org_name", "region_name", "region_code"])


def etl_rtt_waits(db_path: str = "data/processed/nhs.duckdb", force: bool = False):
    """ETL pipeline for RTT waiting times."""
    conn = get_connection(db_path)
    try:
        ensure_schema_exists(conn)
        run = ingest_files(
            conn, dataset="rtt_waits",
            paths=sorted(Path("data/raw").glob("rtt_incomplete_provider_*.xlsx")),
            load_fn=load_rtt_waits, write_fn=_write_rtt, force=force,
        )
        return run
    finally:
        conn.close()


if __name__ == "__main__":
    etl_rtt_waits()


