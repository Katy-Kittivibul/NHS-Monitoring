"""Stage 3 Tier 2 — direct trust-level A&E vs RTT correlation, using the 139
shared trust codes directly. No geography mapping needed — this is the most
reliable Stage 3 signal since it skips region resolution entirely.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from corr.correlation import rolling_correlation, to_first_difference
from corr.inference import annotate_fdr
from corr.rollups import get_ae_breach_pct_by_trust, get_rtt_over18wk_pct_by_trust
from corr.signals import ensure_corr_schema_exists, get_connection, signal_row, write_corr_signals

TIER = "tier2_trust"
DATASET_PAIR = "ae_rtt_trust"
GEOGRAPHY_LEVEL = "trust"


def run_tier2_trust_correlations(db_path: str = "data/processed/nhs.duckdb") -> list[dict]:
    """Run direct trust-level A&E vs RTT correlation and write signals to corr_signals."""
    conn = get_connection(db_path)
    try:
        ensure_corr_schema_exists(conn)

        series_a = get_ae_breach_pct_by_trust(conn)
        series_b = get_rtt_over18wk_pct_by_trust(conn)
        results = rolling_correlation(to_first_difference(series_a), to_first_difference(series_b))

        signals = [signal_row(TIER, DATASET_PAIR, GEOGRAPHY_LEVEL, r) for r in results]
        annotate_fdr(signals, family_keys=("tier", "dataset_pair", "month_date"))

        write_corr_signals(conn, TIER, DATASET_PAIR, signals)
        print(f"[OK] Tier 2 {DATASET_PAIR}: {len(signals)} signals")
        return signals
    finally:
        conn.close()


if __name__ == "__main__":
    run_tier2_trust_correlations()
