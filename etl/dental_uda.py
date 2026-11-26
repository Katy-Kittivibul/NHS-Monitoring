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
    map_unique,
    replace_months,
    to_float_series,
    upsert_organisations,
)


def parse_dental_month(year_month_str: str) -> datetime:
    """Parse a dental period to a date (first day of month).

    NHSBSA publishes it as YYYYMM ("202503") in older files and as YYYY-MM ("2026-08") in
    newer ones, so both are accepted."""
    match = re.fullmatch(r"(\d{4})-?(\d{2})", str(year_month_str).strip())
    if not match:
        raise ValueError(f"Cannot parse month: {year_month_str}")
    try:
        return datetime(int(match.group(1)), int(match.group(2)), 1).date()
    except ValueError:
        raise ValueError(f"Cannot parse month: {year_month_str}")


def read_dental_csv(csv_path: str) -> pd.DataFrame:
    """Read an NHSBSA dental CSV. Most files are UTF-8, but some months (e.g. 2025-04,
    2025-05) were published as Windows-1252, which pandas' default UTF-8 read rejects."""
    try:
        return pd.read_csv(csv_path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(csv_path, encoding="cp1252")


def clean_dental_value(val) -> float:
    """Convert dental column values to floats, handling NaN."""
    if pd.isna(val):
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def load_dental_uda_file(csv_path: str) -> pd.DataFrame:
    """Load a single dental UDA CSV file."""
    df = read_dental_csv(csv_path)

    # Strip whitespace from column names
    df.columns = df.columns.str.strip()

    # Newer files (2026) name the urgent-treatment band "BAND_UNSCHEDULED_DELIVERED". Same
    # series (monthly totals are continuous across the rename), so keep one target column.
    if "BAND_URGENT_DELIVERED" not in df.columns and "BAND_UNSCHEDULED_DELIVERED" in df.columns:
        df = df.rename(columns={"BAND_UNSCHEDULED_DELIVERED": "BAND_URGENT_DELIVERED"})

    # Parse month date
    df["month_date"] = map_unique(df["YEAR_MONTH"], parse_dental_month)

    # Rename key columns
    df = df.rename(columns={
        "COMMISSIONER_CODE": "icb_code",
        "COMMISSIONER_NAME": "icb_name",
        "CONTRACT_NUMBER": "contract_number",
        "PROVIDER_NAME": "provider_name",
        "UDA_PERF_TARGET": "uda_target",
        "UDA_DELIVERED": "uda_delivered",
        "BAND_1_DELIVERED": "band_1_delivered",
        "BAND_3_DELIVERED": "band_3_delivered",
        "BAND_URGENT_DELIVERED": "band_urgent_delivered",
    })

    # Handle Band 2 harmonisation
    # Pre-Nov 2022: BAND_2_DELIVERED
    # Post-Nov 2022: BAND_2A_DELIVERED, BAND_2B_DELIVERED, BAND_2C_DELIVERED
    if "BAND_2_DELIVERED" in df.columns:
        df["band_2_delivered"] = to_float_series(df["BAND_2_DELIVERED"])
    elif all(col in df.columns for col in ["BAND_2A_DELIVERED", "BAND_2B_DELIVERED", "BAND_2C_DELIVERED"]):
        df["band_2_delivered"] = (
            to_float_series(df["BAND_2A_DELIVERED"])
            + to_float_series(df["BAND_2B_DELIVERED"])
            + to_float_series(df["BAND_2C_DELIVERED"])
        )
    else:
        df["band_2_delivered"] = 0.0

    # Clean numeric columns
    numeric_cols = ["uda_target", "uda_delivered", "band_1_delivered",
                    "band_3_delivered", "band_urgent_delivered", "band_2_delivered"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = to_float_series(df[col])
        else:
            df[col] = 0.0

    # Select required columns in order
    result = df[[
        "month_date", "icb_code", "icb_name", "contract_number",
        "provider_name", "uda_target", "uda_delivered", "band_1_delivered",
        "band_2_delivered", "band_3_delivered", "band_urgent_delivered"
    ]].copy()

    return result


def _write_dental(conn, df):
    replace_months(conn, "fact_dental_uda", df)

    # Update dim_organisation with ICBs from this dataset
    org_rows = df[["icb_code", "icb_name"]].drop_duplicates("icb_code", keep="last")
    org_rows = org_rows.rename(columns={"icb_code": "org_code", "icb_name": "org_name"})
    org_rows["org_type"] = "icb"
    upsert_organisations(conn, org_rows, ["org_name"])


def etl_dental_uda(db_path: str = "data/processed/nhs.duckdb", force: bool = False):
    """ETL pipeline for dental UDA data."""
    conn = get_connection(db_path)
    try:
        ensure_schema_exists(conn)
        run = ingest_files(
            conn, dataset="dental_uda",
            paths=sorted(Path("data/raw").glob("dental_uda_*.csv")),
            load_fn=load_dental_uda_file, write_fn=_write_dental, force=force,
        )
        return run
    finally:
        conn.close()


if __name__ == "__main__":
    etl_dental_uda()

