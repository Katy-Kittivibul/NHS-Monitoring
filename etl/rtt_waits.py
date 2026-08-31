import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import re
from datetime import datetime

import pandas as pd

from etl.common import ensure_schema_exists, get_connection  # noqa: E402 — after the sys.path shim above


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
        df["waiting_list_size"] = df[total_cols[0]].apply(clean_rtt_value)
    else:
        week_cols = [c for c in df.columns if c not in [
            "month_date", "org_code", "org_name", "region_code",
            "specialty_code", "specialty_name"
        ]]
        df["waiting_list_size"] = df[week_cols].apply(
            lambda row: sum(clean_rtt_value(v) for v in row), axis=1
        )

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
        df["over_18wk_count"] = df[over_18wk_cols].apply(
            lambda row: sum(clean_rtt_value(v) for v in row), axis=1
        )
    else:
        df["over_18wk_count"] = 0

    # Calculate over_18wk_pct (avoid division by zero)
    df["over_18wk_pct"] = df.apply(
        lambda row: (row["over_18wk_count"] / row["waiting_list_size"] * 100.0)
        if row["waiting_list_size"] > 0 else 0.0,
        axis=1
    )

    # Median wait weeks
    median_cols = [c for c in df.columns if "median" in c.lower() or "50th" in c.lower()]
    if median_cols:
        df["median_wait_weeks"] = df[median_cols[0]].apply(
            lambda x: float(x) if pd.notna(x) and x != "-" and x != "" else 0.0
        )
    else:
        df["median_wait_weeks"] = 0.0

    # Select and order columns
    result = df[[
        "month_date", "org_code", "org_name", "region_code",
        "specialty_code", "specialty_name", "waiting_list_size",
        "over_18wk_count", "over_18wk_pct", "median_wait_weeks"
    ]].copy()

    return result


def etl_rtt_waits(db_path: str = "data/processed/nhs.duckdb"):
    """ETL pipeline for RTT waiting times."""
    conn = get_connection(db_path)
    try:
        ensure_schema_exists(conn)

        xlsx_files = sorted(Path("data/raw").glob("rtt_incomplete_provider_*.xlsx"))
        if not xlsx_files:
            print("⚠ Skipping RTT: No rtt_incomplete_provider_*.xlsx files found in data/raw/")
            return

        for xlsx_path in xlsx_files:
            print(f"Loading RTT waiting times: {xlsx_path.name}...")
            df = load_rtt_waits(str(xlsx_path))
            if df.empty:
                continue

            # Upsert into fact table (clear old data first for idempotency)
            month_date = df["month_date"].iloc[0]
            conn.execute(
                "DELETE FROM fact_rtt_waits WHERE month_date = ?",
                [month_date]
            )

            conn.execute("""
                INSERT INTO fact_rtt_waits
                SELECT * FROM df
            """)

            # Update dim_organisation with providers from this dataset
            org_rows = df[["org_code", "org_name", "region_code"]].drop_duplicates()
            org_rows["org_type"] = "trust"
            org_rows["parent_org_code"] = None
            org_rows = org_rows.rename(columns={"region_code": "region_name"})

            conn.execute("""
                INSERT INTO dim_organisation (org_code, org_name, org_type, parent_org_code, region_name)
                SELECT org_code, org_name, org_type, parent_org_code, region_name FROM org_rows
                ON CONFLICT (org_code) DO UPDATE SET
                    org_name = EXCLUDED.org_name,
                    org_type = EXCLUDED.org_type,
                    region_name = EXCLUDED.region_name
            """)

            print(f"[OK] Loaded RTT ({xlsx_path.name}): {len(df)} rows into fact_rtt_waits")

    finally:
        conn.close()


if __name__ == "__main__":
    etl_rtt_waits()


