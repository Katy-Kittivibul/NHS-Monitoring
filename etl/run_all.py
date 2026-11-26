"""Master ETL orchestrator for NHS monitoring system."""
import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from etl.ae_attendances import etl_ae_attendances  # noqa: E402
from etl.common import create_dim_date, ensure_schema_exists, get_connection  # noqa: E402
from etl.dental_uda import etl_dental_uda  # noqa: E402
from etl.prescribing import etl_prescribing  # noqa: E402
from etl.rtt_waits import etl_rtt_waits  # noqa: E402


def run_all_etl(db_path: str = "data/processed/nhs.duckdb", force: bool = False):
    """Run complete ETL pipeline for all 4 datasets.

    Raw files already recorded in ingestion_manifest with an unchanged sha256 are skipped;
    force=True reloads everything."""
    print("=" * 60)
    print("NHS Monitoring Copilot — Stage 0 ETL Pipeline")
    print("=" * 60)

    # Ensure database and schema exist
    conn = get_connection(db_path)
    try:
        ensure_schema_exists(conn)

        # Create dimension tables
        print("\n[Step 1/6] Creating dimension tables...")
        create_dim_date(conn)
        print("[OK] Dimension tables created")
    finally:
        conn.close()

    # Load fact tables in order
    print("\n[Step 2/6] Loading A&E attendances...")
    runs = [etl_ae_attendances(db_path, force=force)]

    print("\n[Step 3/6] Loading RTT waiting times...")
    runs.append(etl_rtt_waits(db_path, force=force))

    print("\n[Step 4/6] Loading GP prescribing...")
    runs.append(etl_prescribing(db_path, force=force))

    print("\n[Step 5/6] Loading dental UDA...")
    runs.append(etl_dental_uda(db_path, force=force))

    files_loaded = sum(run.files_loaded for run in runs)
    files_skipped = sum(run.files_skipped for run in runs)

    # Verify loaded tables
    print("\n[Step 6/6] Verifying schema and row counts...")
    conn = get_connection(db_path)
    try:
        tables = [
            "dim_date",
            "dim_organisation",
            "fact_ae_attendances",
            "fact_rtt_waits",
            "fact_prescribing",
            "fact_dental_uda"
        ]

        for table in tables:
            result = conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchall()
            count = result[0][0] if result else 0
            print(f"  • {table}: {count} rows")
    finally:
        conn.close()


    print("\n" + "=" * 60)
    print(f"[OK] Stage 0 ETL complete! ({files_loaded} file(s) loaded, {files_skipped} unchanged)")
    if files_loaded:
        # dq_flags / cp_signals / corr_* were computed from the previous data and are now stale.
        print("[NOTE] Raw data changed: re-run the downstream stages (`make pipeline`, or "
              "dq/run_all.py, cp/run_all.py, corr/run_all.py) and then `make reporting`.")
    print("=" * 60)
    return files_loaded


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 0: load raw NHS files into DuckDB.")
    parser.add_argument("--db", default="data/processed/nhs.duckdb", help="DuckDB file to load into")
    parser.add_argument("--force", action="store_true", help="reload files even if unchanged")
    cli = parser.parse_args()
    run_all_etl(cli.db, force=cli.force)

