"""Master orchestrator for Stage 2 changepoint/SPC signal detection."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cp.signals import get_connection, ensure_cp_schema_exists
from cp.spc_baseline import run_spc_baseline
from cp.changepoints import run_changepoints


def run_all_cp(db_path: str = "data/processed/nhs.duckdb"):
    """Run Stage 2 SPC rules and PELT changepoint detection, then print a summary."""
    print("=" * 60)
    print("NHS Monitoring Copilot — Stage 2 Changepoint Detection")
    print("=" * 60)

    conn = get_connection(db_path)
    try:
        ensure_cp_schema_exists(conn)
    finally:
        conn.close()

    print("\n[1/2] SPC baseline rules (Part A)...")
    run_spc_baseline(db_path)

    print("\n[2/2] PELT changepoint detection (Part B)...")
    run_changepoints(db_path)

    conn = get_connection(db_path)
    try:
        summary = conn.execute("""
            SELECT method, signal_type, COUNT(*) AS n
            FROM cp_signals
            GROUP BY method, signal_type
            ORDER BY method, signal_type
        """).fetchall()

        print("\n" + "=" * 60)
        print("cp_signals summary (method / signal_type / count):")
        if summary:
            for method, signal_type, n in summary:
                print(f"  • {method:20s} {signal_type:25s} {n}")
        else:
            print("  (no signals detected)")
        print("=" * 60)
    finally:
        conn.close()


if __name__ == "__main__":
    run_all_cp()
