import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta

# Resolved relative to this module, not the CWD: this is a repo-tracked reference
# file, so it must load identically whether the pipeline is run from the repo
# root, from Docker (WORKDIR /app), or from pytest's rootdir. Same pattern as
# corr/geography.py's DEFAULT_ICB_REGION_PATH.
DEFAULT_SUB_ICB_MAPPING_PATH = str(Path(__file__).parent / "reference" / "sub_icb_to_icb.csv")


# --- Vectorised cleaners -----------------------------------------------------------
# The per-dataset scalar cleaners (clean_ae_value, clean_rtt_value, clean_dental_value)
# stay as the reference implementation: tests/test_etl.py asserts these vectorised
# versions agree with them on a fuzz set of dirty values, and on every raw file.


def to_int_series(values) -> pd.Series:
    """Vectorised clean_ae_value / clean_rtt_value: non-numeric, blank, '-' and NaN -> 0,
    otherwise truncate toward zero. Accepts a Series or a scalar-per-row iterable."""
    num = pd.to_numeric(pd.Series(values), errors="coerce")
    num = num.where(np.isfinite(num), 0.0).fillna(0.0)
    return np.trunc(num).astype("int64")


def to_float_series(values) -> pd.Series:
    """Vectorised clean_dental_value: non-numeric and NaN -> 0.0."""
    num = pd.to_numeric(pd.Series(values), errors="coerce")
    return num.where(np.isfinite(num), 0.0).fillna(0.0).astype("float64")


def to_int_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """to_int_series applied column-wise (the index is preserved)."""
    return frame.apply(to_int_series)


def map_unique(series: pd.Series, func) -> pd.Series:
    """Series.map(func) evaluated once per distinct value — for parsers applied to a column
    that only has a handful of distinct values (e.g. the period label, repeated per row)."""
    lookup = {value: func(value) for value in series.dropna().unique()}
    return series.map(lookup)


def safe_pct(numerator, denominator) -> pd.Series:
    """numerator / denominator * 100, or 0.0 where the denominator is not positive."""
    num = pd.Series(numerator, dtype="float64")
    den = pd.Series(denominator, dtype="float64")
    return pd.Series(np.where(den > 0, num / den.where(den > 0, 1.0) * 100.0, 0.0), index=den.index)


# --- Organisation dimension: type precedence and region normalisation ---------------
# Several datasets describe the same org code in different roles (an A&E walk-in centre
# is a "trust" to A&E/RTT but also appears as a "practice" in GP prescribing). The role
# with the higher rank wins, regardless of load order, so a later, lower-ranked load can
# never downgrade an org or overwrite its attributes.
ORG_TYPE_RANK = {"trust": 3, "icb": 2, "practice": 1}

REGION_CODE_TO_NAME = {
    "Y56": "NHS ENGLAND LONDON",
    "Y58": "NHS ENGLAND SOUTH WEST",
    "Y59": "NHS ENGLAND SOUTH EAST",
    "Y60": "NHS ENGLAND MIDLANDS",
    "Y61": "NHS ENGLAND EAST OF ENGLAND",
    "Y62": "NHS ENGLAND NORTH WEST",
    "Y63": "NHS ENGLAND NORTH EAST AND YORKSHIRE",
}
REGION_FULL_NAME_TO_CODE = {name: code for code, name in REGION_CODE_TO_NAME.items()}


def normalise_region(value) -> tuple[str | None, str | None]:
    """Map either form of an NHS England region ("Y60" or "NHS ENGLAND MIDLANDS ") to the
    canonical (region_code, region_name) pair. Unrecognised/blank -> (None, None)."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None, None
    text = str(value).strip().upper()
    if text in REGION_CODE_TO_NAME:
        return text, REGION_CODE_TO_NAME[text]
    if text in REGION_FULL_NAME_TO_CODE:
        return REGION_FULL_NAME_TO_CODE[text], text
    return None, None


def _org_rank_sql(column: str) -> str:
    whens = " ".join(f"WHEN '{t}' THEN {r}" for t, r in ORG_TYPE_RANK.items())
    return f"CASE {column} {whens} ELSE 0 END"


def upsert_organisations(conn, org_rows: pd.DataFrame, update_cols: list[str]) -> None:
    """Upsert org_rows into dim_organisation without ever downgrading an org's type.

    org_rows needs org_code and org_type plus every column in update_cols. On conflict the
    incoming row only replaces the existing one when its org_type ranks >= the existing
    org_type (trust > icb > practice); otherwise the existing row is kept untouched.
    """
    columns = ["org_code", "org_type", *update_cols]
    incoming = org_rows[columns].copy()
    conn.register("_org_upsert_rows", incoming)
    try:
        wins = f"{_org_rank_sql('EXCLUDED.org_type')} >= {_org_rank_sql('dim_organisation.org_type')}"
        sets = ", ".join(
            f"{col} = CASE WHEN {wins} THEN EXCLUDED.{col} ELSE dim_organisation.{col} END"
            for col in ["org_type", *update_cols]
        )
        conn.execute(f"""
            INSERT INTO dim_organisation ({", ".join(columns)})
            SELECT {", ".join(columns)} FROM _org_upsert_rows
            ON CONFLICT (org_code) DO UPDATE SET {sets}
        """)
    finally:
        conn.unregister("_org_upsert_rows")


# --- Run observability and incremental-load manifest --------------------------------
# pipeline_runs: one row per (stage, dataset) execution, success or failure.
# ingestion_manifest: the sha256 of every raw file already loaded, so an unchanged file
# is skipped on the next run unless --force is given.


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RunTracker:
    """Mutable counters a pipeline step fills in while track_run is open."""

    def __init__(self):
        self.rows_processed = 0
        self.files_loaded = 0
        self.files_skipped = 0
        self.status = "success"
        self.detail: str | None = None


@contextmanager
def track_run(conn, stage: str, dataset: str):
    """Record the run in pipeline_runs: 'running' on entry, then 'success' / 'skipped' /
    'failed' (with the exception text) on exit. The original exception is always re-raised,
    and a failure to write the log never masks it."""
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, stage, dataset, started_at, status) "
        "VALUES (?, ?, ?, ?, 'running')",
        [run_id, stage, dataset, datetime.now()],
    )
    tracker = RunTracker()
    try:
        yield tracker
    except BaseException as exc:
        _finish_run(conn, run_id, "failed", tracker, f"{type(exc).__name__}: {exc}"[:1000])
        raise
    else:
        _finish_run(conn, run_id, tracker.status, tracker, tracker.detail)


def _finish_run(conn, run_id: str, status: str, tracker: RunTracker, detail: str | None) -> None:
    summary = json.dumps({"files_loaded": tracker.files_loaded, "files_skipped": tracker.files_skipped})
    try:
        conn.execute(
            "UPDATE pipeline_runs SET finished_at = ?, status = ?, rows_processed = ?, detail = ? "
            "WHERE run_id = ?",
            [datetime.now(), status, tracker.rows_processed, detail or summary, run_id],
        )
    except Exception as log_exc:  # never let logging mask the real outcome
        print(f"[WARN] could not record pipeline run {run_id}: {log_exc}")


def ingest_files(conn, *, dataset: str, paths, load_fn, write_fn, force: bool = False,
                 stage: str = "etl") -> RunTracker:
    """Load each raw file once, skipping any whose sha256 is already in ingestion_manifest.

    load_fn(path) -> DataFrame; write_fn(conn, df) writes it (delete-then-insert per month,
    dimension upserts). Pass force=True to reload everything.

    Crash safety: the manifest row is the commit marker. It is removed *before* the write and
    re-inserted only after the write completes, so a file is never recorded as loaded unless
    it fully was; a crash at any point means the next run retries that file. (An explicit
    transaction around the write would be simpler, but the DuckDB version pinned in
    requirements.txt rejects a same-transaction delete + re-insert of the same primary key.)
    """
    paths = [Path(p) for p in paths]
    with track_run(conn, stage, dataset) as run:
        if not paths:
            run.status = "skipped"
            run.detail = "no raw files found in data/raw/"
            print(f"⚠ Skipping {dataset}: {run.detail}")
            return run
        for path in paths:
            sha = file_sha256(path)
            current = conn.execute(
                "SELECT 1 FROM ingestion_manifest WHERE file_name = ? AND sha256 = ?", [path.name, sha]
            ).fetchone()
            if current and not force:
                print(f"  skip (unchanged) {path.name}")
                run.files_skipped += 1
                continue
            print(f"Loading {dataset}: {path.name}...")
            df = load_fn(str(path))
            conn.execute("DELETE FROM ingestion_manifest WHERE file_name = ?", [path.name])
            if not df.empty:
                write_fn(conn, df)
            conn.execute(
                "INSERT INTO ingestion_manifest (file_name, dataset, sha256, loaded_at, row_count) "
                "VALUES (?, ?, ?, ?, ?)",
                [path.name, dataset, sha, datetime.now(), len(df)],
            )
            run.files_loaded += 1
            run.rows_processed += len(df)
            print(f"[OK] Loaded {dataset} ({path.name}): {len(df)} rows")
        return run


def replace_months(conn, table: str, df: pd.DataFrame) -> None:
    """Idempotent fact write: delete every month present in df, then insert df.
    Every month, not just the first: a file covering several months would otherwise leave
    the later ones in place and duplicate them on re-run."""
    for month_date in df["month_date"].unique():
        conn.execute(f"DELETE FROM {table} WHERE month_date = ?", [month_date])
    conn.register("_fact_rows", df)
    try:
        conn.execute(f"INSERT INTO {table} SELECT * FROM _fact_rows")
    finally:
        conn.unregister("_fact_rows")


def get_connection(db_path: str = "data/processed/nhs.duckdb"):
    """Get or create DuckDB connection."""
    return duckdb.connect(db_path)


def create_dim_date(conn, start_date: str = "2020-01-01", end_date: str = "2027-12-31"):
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


def create_dim_sub_icb(conn, mapping_path: str = DEFAULT_SUB_ICB_MAPPING_PATH):
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
            specialist_no_ae BOOLEAN,
            region_code VARCHAR
        )
    """)
    # dim_organisation predates this column on any DB created before this change —
    # CREATE TABLE IF NOT EXISTS above is a no-op against an existing table, so an
    # explicit migration is needed too. NULL = not an A&E-reporting org at all
    # (e.g. an RTT-only trust); set to TRUE/FALSE by etl/ae_attendances.py once
    # fact_ae_attendances is loaded.
    conn.execute("ALTER TABLE dim_organisation ADD COLUMN IF NOT EXISTS specialist_no_ae BOOLEAN")
    # region_code is the canonical NHS England region key (Y56..Y63). region_name used to
    # hold *either* the code (RTT load) or the full name (A&E load), whichever ran last;
    # both loads now write both columns consistently via normalise_region().
    conn.execute("ALTER TABLE dim_organisation ADD COLUMN IF NOT EXISTS region_code VARCHAR")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id VARCHAR PRIMARY KEY,
            stage VARCHAR,
            dataset VARCHAR,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            status VARCHAR,
            rows_processed BIGINT,
            detail VARCHAR
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingestion_manifest (
            file_name VARCHAR PRIMARY KEY,
            dataset VARCHAR,
            sha256 VARCHAR,
            loaded_at TIMESTAMP,
            row_count BIGINT
        )
    """)

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
            over_52wk_count BIGINT,
            PRIMARY KEY (month_date, org_code, specialty_code)
        )
    """)
    # over_52wk_count was added after the first loads; CREATE TABLE IF NOT EXISTS is a
    # no-op on an existing table, so migrate it. Pre-existing rows are NULL until the
    # RTT files are reloaded by etl/rtt_waits.py.
    conn.execute("ALTER TABLE fact_rtt_waits ADD COLUMN IF NOT EXISTS over_52wk_count BIGINT")

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
