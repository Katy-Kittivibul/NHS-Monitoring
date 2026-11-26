import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import re
from datetime import date

import pandas as pd

from etl.common import (  # noqa: E402 — after the sys.path shim above
    create_dim_sub_icb,
    ensure_schema_exists,
    get_connection,
    ingest_files,
    replace_months,
    upsert_organisations,
)

FILENAME_MONTH_RE = re.compile(r"openprescribing_spending_(\d{4})(\d{2})\.json$")


def month_from_filename(json_path: str) -> date:
    """Month covered by a payload that carries no date field, read off the filename."""
    match = FILENAME_MONTH_RE.search(Path(json_path).name)
    if not match:
        raise ValueError(
            f"{Path(json_path).name} carries no 'date' field and no YYYYMM in its name, "
            "so the month it covers cannot be established"
        )
    return date(int(match.group(1)), int(match.group(2)), 1)


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
        # Fall back to the month named in the filename, not the current date: stamping
        # the run date onto the rows would put the wrong month in the fact table and
        # make every re-run land somewhere new instead of replacing the same month.
        df["month_date"] = month_from_filename(json_path)

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


def _write_prescribing(conn, df):
    replace_months(conn, "fact_prescribing", df)

    # Update dim_organisation with practices from this dataset. A practice row can never
    # overwrite an org already known as a trust (A&E walk-in centres appear in both).
    org_rows = df[["practice_code", "practice_name", "sub_icb_code"]].drop_duplicates(
        "practice_code", keep="last"
    )
    org_rows = org_rows.rename(
        columns={"practice_code": "org_code", "practice_name": "org_name", "sub_icb_code": "parent_org_code"}
    )
    org_rows["org_type"] = "practice"
    upsert_organisations(conn, org_rows, ["org_name", "parent_org_code"])


def etl_prescribing(db_path: str = "data/processed/nhs.duckdb", force: bool = False):
    """ETL pipeline for GP prescribing data."""
    conn = get_connection(db_path)
    try:
        ensure_schema_exists(conn)
        create_dim_sub_icb(conn)
        run = ingest_files(
            conn, dataset="prescribing",
            paths=sorted(Path("data/raw").glob("openprescribing_spending_*.json")),
            load_fn=load_prescribing_data, write_fn=_write_prescribing, force=force,
        )
        return run
    finally:
        conn.close()


if __name__ == "__main__":
    etl_prescribing()


