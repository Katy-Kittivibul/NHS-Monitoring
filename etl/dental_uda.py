import pandas as pd
from pathlib import Path
import re
from datetime import datetime
from common import get_connection, ensure_schema_exists


def parse_dental_month(year_month_str: str) -> datetime:
    """Parse YYYYMM format to date (first day of month)."""
    try:
        year = int(str(year_month_str)[:4])
        month = int(str(year_month_str)[4:6])
        return datetime(year, month, 1).date()
    except (ValueError, IndexError):
        raise ValueError(f"Cannot parse month: {year_month_str}")


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
    df = pd.read_csv(csv_path)

    # Strip whitespace from column names
    df.columns = df.columns.str.strip()

    # Parse month date
    df["month_date"] = df["YEAR_MONTH"].apply(parse_dental_month)

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
        df["band_2_delivered"] = df["BAND_2_DELIVERED"].apply(clean_dental_value)
    elif all(col in df.columns for col in ["BAND_2A_DELIVERED", "BAND_2B_DELIVERED", "BAND_2C_DELIVERED"]):
        df["band_2_delivered"] = (
            df["BAND_2A_DELIVERED"].apply(clean_dental_value) +
            df["BAND_2B_DELIVERED"].apply(clean_dental_value) +
            df["BAND_2C_DELIVERED"].apply(clean_dental_value)
        )
    else:
        df["band_2_delivered"] = 0.0

    # Clean numeric columns
    numeric_cols = ["uda_target", "uda_delivered", "band_1_delivered",
                    "band_3_delivered", "band_urgent_delivered", "band_2_delivered"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = df[col].apply(clean_dental_value)
        else:
            df[col] = 0.0

    # Select required columns in order
    result = df[[
        "month_date", "icb_code", "icb_name", "contract_number",
        "provider_name", "uda_target", "uda_delivered", "band_1_delivered",
        "band_2_delivered", "band_3_delivered", "band_urgent_delivered"
    ]].copy()

    return result


def etl_dental_uda(db_path: str = "data/processed/nhs.duckdb"):
    """ETL pipeline for dental UDA data."""
    conn = get_connection(db_path)
    try:
        ensure_schema_exists(conn)

        # Process all available dental UDA CSV files
        csv_files = sorted(Path("data/raw").glob("dental_uda_*.csv"))

        all_data = []
        for csv_path in csv_files:
            print(f"Loading {csv_path.name}...")
            df = load_dental_uda_file(str(csv_path))
            all_data.append(df)


        if not all_data:
            print("⚠ No dental UDA data files found")
            return

        df = pd.concat(all_data, ignore_index=True)

        # Upsert into fact table (clear old data first for idempotency)
        for month_date in df["month_date"].unique():
            conn.execute(
                "DELETE FROM fact_dental_uda WHERE month_date = ?",
                [month_date]
            )

        conn.execute("""
            INSERT INTO fact_dental_uda
            SELECT * FROM df
        """)

        # Update dim_organisation with ICBs from this dataset
        org_rows = df[["icb_code", "icb_name"]].drop_duplicates()
        org_rows["org_type"] = "icb"
        org_rows["parent_org_code"] = None
        org_rows["region_name"] = None
        org_rows = org_rows.rename(columns={"icb_code": "org_code", "icb_name": "org_name"})

        conn.execute("""
            INSERT INTO dim_organisation (org_code, org_name, org_type, parent_org_code, region_name)
            SELECT org_code, org_name, org_type, parent_org_code, region_name FROM org_rows
            ON CONFLICT (org_code) DO UPDATE SET
                org_name = EXCLUDED.org_name,
                org_type = EXCLUDED.org_type
        """)

        print(f"[OK] Loaded dental UDA: {len(df)} rows into fact_dental_uda")

    finally:
        conn.close()


if __name__ == "__main__":
    etl_dental_uda()

