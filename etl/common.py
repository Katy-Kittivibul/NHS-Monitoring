import duckdb
from datetime import datetime
from dateutil.relativedelta import relativedelta
import pandas as pd


def get_connection(db_path: str = "data/processed/nhs.duckdb"):
    """Get or create DuckDB connection."""
    return duckdb.connect(db_path)


def create_dim_date(conn, start_date: str = "2020-01-01", end_date: str = "2025-12-31"):
    """Create dim_date dimension table with month-level granularity."""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

    dates = []
    current = start
    while current <= end:
        month_start = current.replace(day=1)
        year = month_start.year
        month = month_start.month
        month_name = month_start.strftime("%B")
        quarter = f"Q{(month - 1) // 3 + 1}"
        # Financial year: April to March (e.g., 2023/24)
        fy_year = year if month >= 4 else year - 1
        financial_year = f"{fy_year}/{fy_year + 1 - 2000}"

        dates.append({
            "date_key": month_start,
            "year": year,
            "month": month,
            "month_name": month_name,
            "quarter": quarter,
            "financial_year": financial_year,
        })
        current = current + relativedelta(months=1)

    df = pd.DataFrame(dates)  # noqa: F841 — referenced by name in SQL below
    conn.execute("""
        CREATE OR REPLACE TABLE dim_date AS
        SELECT
            date_key,
            year,
            month,
            month_name,
            quarter,
            financial_year
        FROM df
    """)
    print(f"[OK] Created dim_date: {len(dates)} rows")


def create_dim_organisation(conn, org_data: pd.DataFrame = None):
    """
    Create or replace dim_organisation table.
    org_data should have columns: org_code, org_name, org_type, parent_org_code, region_name
    """
    if org_data is None or org_data.empty:
        # Create minimal skeleton if no data provided
        org_data = pd.DataFrame({
            "org_code": [],
            "org_name": [],
            "org_type": [],
            "parent_org_code": [],
            "region_name": [],
        })

    conn.execute("""
        CREATE OR REPLACE TABLE dim_organisation AS
        SELECT DISTINCT
            org_code,
            org_name,
            org_type,
            parent_org_code,
            region_name
        FROM org_data
        WHERE org_code IS NOT NULL
    """)
    print(f"[OK] Created/updated dim_organisation: {len(org_data)} rows")


def create_dim_sub_icb(conn, mapping_path: str = "etl/reference/sub_icb_to_icb.csv"):
    """Create/replace dim_sub_icb, the Sub ICB Location -> ICB bridge fact_prescribing
    needs to roll up cleanly (fact_prescribing.sub_icb_code is a Sub ICB code like
    "16C", not an ICB code like "QHM", so it can't join dim_organisation directly).

    Source: ONS Open Geography Portal, "Sub ICB Locations to Integrated Care Boards
    to NHSER (April 2024) Lookup in EN" (106 rows), plus one manually-added row for
    U2G6B — a Sub ICB code that postdates that lookup, now returned by OpenPrescribing
    for practices under the NHS Thames Valley ICB merger (operational 2025-11-13).
    See etl/reference/sub_icb_to_icb.csv for the full provenance note.

    Also upserts any ICB in the mapping that isn't already in dim_organisation
    (e.g. Thames Valley, which predates the dental UDA data dim_organisation's ICB
    rows are normally sourced from) — existing ICB rows are left untouched.
    """
    mapping = pd.read_csv(mapping_path)  # noqa: F841 — read into scope for the SQL below

    conn.execute("""
        CREATE OR REPLACE TABLE dim_sub_icb AS
        SELECT DISTINCT sub_icb_code, sub_icb_name, icb_code, icb_name
        FROM mapping
        WHERE sub_icb_code IS NOT NULL
    """)

    icb_rows = mapping[["icb_code", "icb_name"]].drop_duplicates().rename(
        columns={"icb_code": "org_code", "icb_name": "org_name"}
    )
    icb_rows["org_type"] = "icb"
    icb_rows["parent_org_code"] = None
    icb_rows["region_name"] = None

    conn.execute("""
        INSERT INTO dim_organisation (org_code, org_name, org_type, parent_org_code, region_name)
        SELECT org_code, org_name, org_type, parent_org_code, region_name FROM icb_rows
        ON CONFLICT (org_code) DO NOTHING
    """)
    print(f"[OK] Created dim_sub_icb: {len(mapping)} rows")


def ensure_schema_exists(conn):
    """Ensure all required tables exist with correct schemas."""
    # Create tables if they don't exist
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dim_date (
            date_key DATE PRIMARY KEY,
            year INT,
            month INT,
            month_name VARCHAR,
            quarter VARCHAR,
            financial_year VARCHAR
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS dim_organisation (
            org_code VARCHAR PRIMARY KEY,
            org_name VARCHAR,
            org_type VARCHAR,
            parent_org_code VARCHAR,
            region_name VARCHAR,
            specialist_no_ae BOOLEAN
        )
    """)
    # dim_organisation predates this column on any DB created before this change —
    # CREATE TABLE IF NOT EXISTS above is a no-op against an existing table, so an
    # explicit migration is needed too. NULL = not an A&E-reporting org at all
    # (e.g. an RTT-only trust); set to TRUE/FALSE by etl/ae_attendances.py once
    # fact_ae_attendances is loaded.
    conn.execute("ALTER TABLE dim_organisation ADD COLUMN IF NOT EXISTS specialist_no_ae BOOLEAN")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS dim_sub_icb (
            sub_icb_code VARCHAR PRIMARY KEY,
            sub_icb_name VARCHAR,
            icb_code VARCHAR,
            icb_name VARCHAR
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS fact_ae_attendances (
            month_date DATE,
            org_code VARCHAR,
            org_name VARCHAR,
            parent_org VARCHAR,
            attendances_type1 BIGINT,
            attendances_type2 BIGINT,
            attendances_other BIGINT,
            total_attendances BIGINT,
            breaches_4hr_type1 BIGINT,
            breaches_4hr_type2 BIGINT,
            breaches_4hr_other BIGINT,
            total_breaches_4hr BIGINT,
            breach_pct DOUBLE,
            admissions_via_ae BIGINT,
            PRIMARY KEY (month_date, org_code)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS fact_rtt_waits (
            month_date DATE,
            org_code VARCHAR,
            org_name VARCHAR,
            region_code VARCHAR,
            specialty_code VARCHAR,
            specialty_name VARCHAR,
            waiting_list_size BIGINT,
            over_18wk_count BIGINT,
            over_18wk_pct DOUBLE,
            median_wait_weeks DOUBLE,
            PRIMARY KEY (month_date, org_code, specialty_code)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS fact_prescribing (
            month_date DATE,
            practice_code VARCHAR,
            practice_name VARCHAR,
            sub_icb_code VARCHAR,
            bnf_section_code VARCHAR,
            setting_code INTEGER,
            items BIGINT,
            quantity DOUBLE,
            actual_cost DOUBLE,
            PRIMARY KEY (month_date, practice_code, bnf_section_code)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS fact_dental_uda (
            month_date DATE,
            icb_code VARCHAR,
            icb_name VARCHAR,
            contract_number VARCHAR,
            provider_name VARCHAR,
            uda_target DOUBLE,
            uda_delivered DOUBLE,
            band_1_delivered DOUBLE,
            band_2_delivered DOUBLE,
            band_3_delivered DOUBLE,
            band_urgent_delivered DOUBLE,
            PRIMARY KEY (month_date, contract_number)
        )
    """)
