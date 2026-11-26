"""Master orchestrator for Stage 2 changepoint/SPC signal detection."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cp.changepoints import run_changepoints
from cp.signals import ensure_cp_schema_exists, get_connection
from cp.spc_baseline import run_spc_baseline

SERIES = [("ae_attendances", False), ("ae_attendances", True), ("rtt_waits", False), ("rtt_waits", True)]


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

    # Each (dataset, peer) pair is its own series with its own limits and penalty. The raw A&E
    # series is the headline one downstream stages read; the peer-relative series (trust minus
    # the national value for the month) removes shared seasonality and national shocks, and a
    # raw changepoint is "robust" when its peer-relative twin agrees (see vw_changepoints).
    for dataset, peer in SERIES:
        label = f"{dataset}{' (peer-relative)' if peer else ''}"
        print(f"\n[{label}] SPC baseline rules (Part A)...")
        run_spc_baseline(db_path, dataset=dataset, peer=peer)
        print(f"[{label}] PELT changepoint detection (Part B)...")
        run_changepoints(db_path, dataset=dataset, peer=peer)

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
