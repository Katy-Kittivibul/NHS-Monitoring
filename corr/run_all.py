"""Master orchestrator for Stage 3 Tier 1/2 correlation signal detection."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from corr.signals import ensure_corr_schema_exists, get_connection
from corr.tier1_region import run_tier1_region_correlations
from corr.tier2_trust import run_tier2_trust_correlations


def run_all_corr(db_path: str = "data/processed/nhs.duckdb"):
    """Run Stage 3 Tier 1 (region) and Tier 2 (trust) correlations, then print a summary."""
    print("=" * 60)
    print("NHS Monitoring Copilot — Stage 3 Correlation (Tier 1/2)")
    print("=" * 60)

    conn = get_connection(db_path)
    try:
        ensure_corr_schema_exists(conn)
    finally:
        conn.close()

    print("\n[1/2] Tier 1 — region-level correlation...")
    run_tier1_region_correlations(db_path)

    print("\n[2/2] Tier 2 — trust-level A&E vs RTT correlation...")
    run_tier2_trust_correlations(db_path)

    conn = get_connection(db_path)
    try:
        summary = conn.execute("""
            SELECT tier, dataset_pair, COUNT(*) AS n
            FROM corr_signals
            GROUP BY tier, dataset_pair
            ORDER BY tier, dataset_pair
        """).fetchall()

        print("\n" + "=" * 60)
        print("corr_signals summary (tier / dataset_pair / count):")
        if summary:
            for tier, dataset_pair, n in summary:
                print(f"  • {tier:15s} {dataset_pair:25s} {n}")
        else:
            print("  (no signals detected)")
        print("=" * 60)
    finally:
        conn.close()


if __name__ == "__main__":
    run_all_corr()
