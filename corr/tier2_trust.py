"""Stage 3 Tier 2 — direct trust-level A&E vs RTT correlation, using the 139
shared trust codes directly. No geography mapping needed — this is the most
reliable Stage 3 signal since it skips region resolution entirely.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from corr.signals import get_connection, ensure_corr_schema_exists, write_corr_signals
from corr.correlation import rolling_correlation
from corr.rollups import get_ae_breach_pct_by_trust, get_rtt_over18wk_pct_by_trust

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
        results = rolling_correlation(series_a, series_b)

        signals = [{
            "tier": TIER, "dataset_pair": DATASET_PAIR, "geography_level": GEOGRAPHY_LEVEL,
            "geography_code": r["key"], "month_date": r["month_date"],
            "correlation_metric": r["correlation_metric"], "detail": r["detail"],
        } for r in results]

        write_corr_signals(conn, TIER, DATASET_PAIR, signals)
        print(f"[OK] Tier 2 {DATASET_PAIR}: {len(signals)} signals")
        return signals
    finally:
        conn.close()


if __name__ == "__main__":
    run_tier2_trust_correlations()
