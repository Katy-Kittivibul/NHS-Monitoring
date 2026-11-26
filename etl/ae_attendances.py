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
    normalise_region,
    replace_months,
    safe_pct,
    to_int_series,
    upsert_organisations,
)


def parse_ae_period(period_str: str) -> datetime:
    """
    Parse A&E period string like 'MSitAE-MARCH-2024' to datetime.
    Format: MSitAE-<MONTH>-<YEAR>
    """
    match = re.search(r"([A-Z]+)-(\d{4})", period_str)
    if match:
        month_str = match.group(1)
        year_str = match.group(2)
        month_map = {
            "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4,
            "MAY": 5, "JUNE": 6, "JULY": 7, "AUGUST": 8,
            "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12
        }
        month = month_map.get(month_str)
        if month:
            return datetime(int(year_str), month, 1).date()
    raise ValueError(f"Cannot parse period: {period_str}")


def clean_ae_value(val) -> int:
    """Convert A&E column values to integers, handling dashes and NaN."""
    if pd.isna(val) or val == "-":
        return 0
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 0


def load_ae_attendances(csv_path: str = "data/raw/ae_attendances_202403.csv"):
    """Load and transform A&E attendances data."""
    df = pd.read_csv(csv_path)

    # Filter out the totals row (case-insensitive, whitespace-trimmed). Most files mark it
    # in Period, but the Feb 2026 file has a normal Period and only Org Code = TOTAL, which
    # otherwise loads as a fake organisation and doubles the national total.
    is_total = (
        df["Period"].astype(str).str.strip().str.upper().str.startswith("TOTAL")
        | (df["Org Code"].astype(str).str.strip().str.upper() == "TOTAL")
    )
    df = df[~is_total].copy()

    # Parse month date from Period column
    df["month_date"] = map_unique(df["Period"], parse_ae_period)


    # Clean and cast attendance columns
    df["attendances_type1"] = to_int_series(df["A&E attendances Type 1"])
    df["attendances_type2"] = to_int_series(df["A&E attendances Type 2"])
    df["attendances_other"] = to_int_series(df["A&E attendances Other A&E Department"])

    # Derived: total attendances
    df["total_attendances"] = df["attendances_type1"] + df["attendances_type2"] + df["attendances_other"]

    # Clean and cast breach columns
    df["breaches_4hr_type1"] = to_int_series(df["Attendances over 4hrs Type 1"])
    df["breaches_4hr_type2"] = to_int_series(df["Attendances over 4hrs Type 2"])
    df["breaches_4hr_other"] = to_int_series(df["Attendances over 4hrs Other Department"])

    # Derived: total breaches
    df["total_breaches_4hr"] = df["breaches_4hr_type1"] + df["breaches_4hr_type2"] + df["breaches_4hr_other"]

    # Derived: breach percentage (avoid division by zero)
    df["breach_pct"] = safe_pct(df["total_breaches_4hr"], df["total_attendances"])

    # Admissions via A&E (sum of Type 1, Type 2, Other)
    ae_type1_col = "Emergency admissions via A&E - Type 1"
    ae_type2_col = "Emergency admissions via A&E - Type 2"
    ae_other_col = "Emergency admissions via A&E - Other A&E department"

    admissions_sum = pd.Series(0, index=df.index)
    if ae_type1_col in df.columns:
        admissions_sum = admissions_sum + to_int_series(df[ae_type1_col])
    if ae_type2_col in df.columns:
        admissions_sum = admissions_sum + to_int_series(df[ae_type2_col])
    if ae_other_col in df.columns:
        admissions_sum = admissions_sum + to_int_series(df[ae_other_col])
    df["admissions_via_ae"] = admissions_sum

    # Select required columns
    result = df[[
        "month_date", "Org Code", "Org name", "Parent Org",
        "attendances_type1", "attendances_type2", "attendances_other",
        "total_attendances", "breaches_4hr_type1", "breaches_4hr_type2",
        "breaches_4hr_other", "total_breaches_4hr", "breach_pct",
        "admissions_via_ae"
    ]].copy()

    # Rename to match star schema
    result.columns = [
        "month_date", "org_code", "org_name", "parent_org",
        "attendances_type1", "attendances_type2", "attendances_other",
        "total_attendances", "breaches_4hr_type1", "breaches_4hr_type2",
        "breaches_4hr_other", "total_breaches_4hr", "breach_pct",
        "admissions_via_ae"
    ]

    return result


def _write_ae(conn, df):
    replace_months(conn, "fact_ae_attendances", df)

    # Update dim_organisation with trusts from this dataset
    org_rows = df[["org_code", "org_name", "parent_org"]].drop_duplicates("org_code", keep="last").copy()
    regions = org_rows["parent_org"].map(normalise_region)
    org_rows["region_code"] = regions.map(lambda pair: pair[0])
    # Unrecognised labels are kept (stripped) rather than dropped, so nothing is lost silently.
    org_rows["region_name"] = [
        pair[1] or (str(raw).strip() if pd.notna(raw) else None)
        for pair, raw in zip(regions, org_rows["parent_org"])
    ]
    org_rows["org_type"] = "trust"
    upsert_organisations(conn, org_rows, ["org_name", "region_name", "region_code"])


def etl_ae_attendances(db_path: str = "data/processed/nhs.duckdb", force: bool = False):
    """ETL pipeline for A&E attendances."""
    conn = get_connection(db_path)
    try:
        ensure_schema_exists(conn)
        run = ingest_files(
            conn, dataset="ae_attendances",
            paths=sorted(Path("data/raw").glob("ae_attendances_*.csv")),
            load_fn=load_ae_attendances, write_fn=_write_ae, force=force,
        )

        # Flag trusts with zero attendances across every month they have data
        # for — specialist/non-emergency trusts (orthopaedic-only, oncology-only,
        # mental health, etc.) that are on the A&E reporting roster but
        # structurally never run a general A&E department, so downstream reports
        # don't fold them into the same "0% breach, stable" bucket as trusts with
        # real activity. Recomputed from the full fact table on every run, so a
        # trust that starts reporting real attendances later flips back to False.
        conn.execute("""
            UPDATE dim_organisation
            SET specialist_no_ae = TRUE
            WHERE org_code IN (
                SELECT org_code FROM fact_ae_attendances
                GROUP BY org_code
                HAVING SUM(total_attendances) = 0
            )
        """)
        conn.execute("""
            UPDATE dim_organisation
            SET specialist_no_ae = FALSE
            WHERE org_code IN (
                SELECT org_code FROM fact_ae_attendances
                GROUP BY org_code
                HAVING SUM(total_attendances) > 0
            )
        """)
        return run
    finally:
        conn.close()


if __name__ == "__main__":
    etl_ae_attendances()


