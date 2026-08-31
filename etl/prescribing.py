import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
from datetime import datetime

import pandas as pd

from etl.common import (  # noqa: E402 — after the sys.path shim above
    create_dim_sub_icb,
    ensure_schema_exists,
    get_connection,
)


def load_prescribing_data(json_path: str = "data/raw/openprescribing_spending_202310.json"):
    """Load and transform GP prescribing data from OpenPrescribing API JSON."""
    with open(json_path, "r") as f:
        data = json.load(f)

    # OpenPrescribing returns a list of records
    records = []
    if isinstance(data, dict):
        # Sometimes it's nested under 'results' key
        records = data.get("results", []) if "results" in data else [data]
    elif isinstance(data, list):
        records = data

    df = pd.DataFrame(records)

    # Filter for setting == 4 (GP practices only)
    if "setting" in df.columns:
        df = df[df["setting"] == 4].copy()

    # Parse date column
    if "date" in df.columns:
        df["month_date"] = pd.to_datetime(df["date"]).dt.date
    else:
        # Fallback: use today's date if not found
        df["month_date"] = datetime.now().date()

    # Rename columns to match schema
    df = df.rename(columns={
        "row_id": "practice_code",
        "row_name": "practice_name",
        "ccg": "sub_icb_code"
    })

    # Handle null CCG codes -> map to 'UNKNOWN'
    df["sub_icb_code"] = df["sub_icb_code"].fillna("UNKNOWN")

    # Ensure numeric columns
    df["items"] = pd.to_numeric(df.get("items", 0), errors="coerce").fillna(0).astype("int64")
    df["quantity"] = pd.to_numeric(df.get("quantity", 0), errors="coerce").fillna(0.0)
    df["actual_cost"] = pd.to_numeric(df.get("actual_cost", 0), errors="coerce").fillna(0.0)
    df["setting_code"] = 4

    # BNF section code should come from the API response or be inferred
    if "bnf_section_code" not in df.columns:
        df["bnf_section_code"] = "0205"  # Sample code (Hypertension & heart failure)

    # Select required columns in order
    result = df[[
        "month_date", "practice_code", "practice_name", "sub_icb_code",
        "bnf_section_code", "setting_code", "items", "quantity", "actual_cost"
    ]].copy()

    # Remove duplicates (in case of API quirks)
    result = result.drop_duplicates()

    return result


def etl_prescribing(db_path: str = "data/processed/nhs.duckdb"):
    """ETL pipeline for GP prescribing data."""
    conn = get_connection(db_path)
    try:
        ensure_schema_exists(conn)
        create_dim_sub_icb(conn)

        json_files = sorted(Path("data/raw").glob("openprescribing_spending_*.json"))
        if not json_files:
            print("⚠ Skipping prescribing: No openprescribing_spending_*.json files found in data/raw/")
            return

        for json_path in json_files:
            print(f"Loading GP prescribing: {json_path.name}...")
            df = load_prescribing_data(str(json_path))
            if df.empty:
                continue

            # Upsert into fact table (clear old data first for idempotency)
            month_date = df["month_date"].iloc[0]
            conn.execute(
                "DELETE FROM fact_prescribing WHERE month_date = ?",
                [month_date]
            )

            conn.execute("""
                INSERT INTO fact_prescribing
                SELECT * FROM df
            """)

            # Update dim_organisation with practices from this dataset
            org_rows = df[["practice_code", "practice_name", "sub_icb_code"]].drop_duplicates()
            org_rows["org_type"] = "practice"
            org_rows["parent_org_code"] = org_rows["sub_icb_code"]
            org_rows["region_name"] = None
            org_rows = org_rows.rename(columns={"practice_code": "org_code", "practice_name": "org_name"})

            conn.execute("""
                INSERT INTO dim_organisation (org_code, org_name, org_type, parent_org_code, region_name)
                SELECT org_code, org_name, org_type, parent_org_code, region_name FROM org_rows
                ON CONFLICT (org_code) DO UPDATE SET
                    org_name = EXCLUDED.org_name,
                    org_type = EXCLUDED.org_type,
                    parent_org_code = EXCLUDED.parent_org_code
            """)

            print(f"[OK] Loaded prescribing ({json_path.name}): {len(df)} rows into fact_prescribing")

    finally:
        conn.close()


if __name__ == "__main__":
    etl_prescribing()


