"""Master orchestrator for Stage 1 data quality checks."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dq.checks import get_connection, ensure_dq_schema_exists
from dq.ae_attendances import run_ae_attendances_dq
from dq.rtt_waits import run_rtt_waits_dq
from dq.prescribing import run_prescribing_dq
from dq.dental_uda import run_dental_uda_dq


def run_all_dq(db_path: str = "data/processed/nhs.duckdb"):
    """Run Stage 1 DQ checks for all 4 datasets and print a status summary."""
    print("=" * 60)
    print("NHS Monitoring Copilot — Stage 1 Data Quality Checks")
    print("=" * 60)

    conn = get_connection(db_path)
    try:
        ensure_dq_schema_exists(conn)
    finally:
        conn.close()

    print("\n[1/4] A&E attendances...")
    run_ae_attendances_dq(db_path)

    print("\n[2/4] RTT waiting times...")
    run_rtt_waits_dq(db_path)

    print("\n[3/4] GP prescribing...")
    run_prescribing_dq(db_path)

    print("\n[4/4] Dental UDA...")
    run_dental_uda_dq(db_path)

    conn = get_connection(db_path)
    try:
        summary = conn.execute("""
            SELECT dataset, status, COUNT(*) AS n
            FROM dq_flags
            GROUP BY dataset, status
            ORDER BY dataset, status
        """).fetchall()

        print("\n" + "=" * 60)
        print("dq_flags summary (dataset / status / count):")
        for dataset, status, n in summary:
            print(f"  • {dataset:15s} {status:5s} {n}")

        non_ok = conn.execute("""
            SELECT dataset, month_date, check_name, status, detail
            FROM dq_flags
            WHERE status != 'OK'
            ORDER BY dataset, month_date, check_name
        """).fetchall()
        if non_ok:
            print("\nFlags requiring attention:")
            for dataset, month_date, check_name, status, detail in non_ok:
                print(f"  [{status}] {dataset} {month_date} {check_name}: {detail}")
        else:
            print("\nNo WARN/FAIL flags — all checks OK.")
        print("=" * 60)
    finally:
        conn.close()


if __name__ == "__main__":
    run_all_dq()
