import pandas as pd
from pathlib import Path
import re
from datetime import datetime
from common import get_connection, ensure_schema_exists


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

    # Filter out totals row (case-insensitive, whitespace-trimmed)
    df = df[~df["Period"].astype(str).str.strip().str.upper().str.startswith("TOTAL")].copy()

    # Parse month date from Period column
    df["month_date"] = df["Period"].apply(parse_ae_period)


    # Clean and cast attendance columns
    df["attendances_type1"] = df["A&E attendances Type 1"].apply(clean_ae_value)
    df["attendances_type2"] = df["A&E attendances Type 2"].apply(clean_ae_value)
    df["attendances_other"] = df["A&E attendances Other A&E Department"].apply(clean_ae_value)

    # Derived: total attendances
    df["total_attendances"] = df["attendances_type1"] + df["attendances_type2"] + df["attendances_other"]

    # Clean and cast breach columns
    df["breaches_4hr_type1"] = df["Attendances over 4hrs Type 1"].apply(clean_ae_value)
    df["breaches_4hr_type2"] = df["Attendances over 4hrs Type 2"].apply(clean_ae_value)
    df["breaches_4hr_other"] = df["Attendances over 4hrs Other Department"].apply(clean_ae_value)

    # Derived: total breaches
    df["total_breaches_4hr"] = df["breaches_4hr_type1"] + df["breaches_4hr_type2"] + df["breaches_4hr_other"]

    # Derived: breach percentage (avoid division by zero)
    df["breach_pct"] = df.apply(
        lambda row: (row["total_breaches_4hr"] / row["total_attendances"] * 100.0)
        if row["total_attendances"] > 0 else 0.0,
        axis=1
    )

    # Admissions via A&E (sum of Type 1, Type 2, Other)
    ae_type1_col = "Emergency admissions via A&E - Type 1"
    ae_type2_col = "Emergency admissions via A&E - Type 2"
    ae_other_col = "Emergency admissions via A&E - Other A&E department"

    admissions_sum = pd.Series(0, index=df.index)
    if ae_type1_col in df.columns:
        admissions_sum = admissions_sum + df[ae_type1_col].apply(clean_ae_value)
    if ae_type2_col in df.columns:
        admissions_sum = admissions_sum + df[ae_type2_col].apply(clean_ae_value)
    if ae_other_col in df.columns:
        admissions_sum = admissions_sum + df[ae_other_col].apply(clean_ae_value)
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


def etl_ae_attendances(db_path: str = "data/processed/nhs.duckdb"):
    """ETL pipeline for A&E attendances."""
    conn = get_connection(db_path)
    try:
        ensure_schema_exists(conn)

        csv_files = sorted(Path("data/raw").glob("ae_attendances_*.csv"))
        if not csv_files:
            print("⚠ Skipping A&E: No ae_attendances_*.csv files found in data/raw/")
            return

        for csv_path in csv_files:
            print(f"Loading A&E attendances: {csv_path.name}...")
            df = load_ae_attendances(str(csv_path))
            if df.empty:
                continue

            # Upsert into fact table (clear old data first for idempotency)
            month_date = df["month_date"].iloc[0]
            conn.execute(
                "DELETE FROM fact_ae_attendances WHERE month_date = ?",
                [month_date]
            )

            conn.execute("""
                INSERT INTO fact_ae_attendances
                SELECT * FROM df
            """)

            # Update dim_organisation with trusts from this dataset
            org_rows = df[["org_code", "org_name", "parent_org"]].drop_duplicates()
            org_rows = org_rows.rename(columns={"parent_org": "region_name"})
            org_rows["org_type"] = "trust"
            org_rows["parent_org_code"] = None

            conn.execute("""
                INSERT INTO dim_organisation (org_code, org_name, org_type, parent_org_code, region_name)
                SELECT org_code, org_name, org_type, parent_org_code, region_name FROM org_rows
                ON CONFLICT (org_code) DO UPDATE SET
                    org_name = EXCLUDED.org_name,
                    org_type = EXCLUDED.org_type,
                    region_name = EXCLUDED.region_name
            """)

            print(f"[OK] Loaded A&E ({csv_path.name}): {len(df)} rows into fact_ae_attendances")

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

    finally:
        conn.close()


if __name__ == "__main__":
    etl_ae_attendances()


