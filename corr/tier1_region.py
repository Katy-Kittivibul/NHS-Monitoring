"""Stage 3 Tier 1 — region-level correlation between {A&E, RTT} and
{prescribing, dental} trends, via dim_organisation.region_name (trusts) and
corr/reference/icb_to_region.csv (ICBs).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from corr.correlation import rolling_correlation, to_first_difference
from corr.inference import annotate_fdr
from corr.rollups import (
    get_ae_breach_pct_by_region,
    get_dental_pct_achieved_by_region,
    get_prescribing_items_by_region,
    get_rtt_over18wk_pct_by_region,
)
from corr.signals import ensure_corr_schema_exists, get_connection, signal_row, write_corr_signals

TIER = "tier1_region"
GEOGRAPHY_LEVEL = "region"

DATASET_PAIRS = [
    ("ae_prescribing_region", get_ae_breach_pct_by_region, get_prescribing_items_by_region),
    ("ae_dental_region", get_ae_breach_pct_by_region, get_dental_pct_achieved_by_region),
    ("rtt_prescribing_region", get_rtt_over18wk_pct_by_region, get_prescribing_items_by_region),
    ("rtt_dental_region", get_rtt_over18wk_pct_by_region, get_dental_pct_achieved_by_region),
]


def run_tier1_region_correlations(db_path: str = "data/processed/nhs.duckdb") -> list[dict]:
    """Run all 4 Tier 1 regional dataset-pair correlations and write signals to corr_signals."""
    conn = get_connection(db_path)
    try:
        ensure_corr_schema_exists(conn)

        all_signals = []
        for dataset_pair, fetch_a, fetch_b in DATASET_PAIRS:
            series_a = fetch_a(conn)
            series_b = fetch_b(conn)
            results = rolling_correlation(to_first_difference(series_a), to_first_difference(series_b))

            signals = [signal_row(TIER, dataset_pair, GEOGRAPHY_LEVEL, r) for r in results]
            annotate_fdr(signals, family_keys=("tier", "dataset_pair", "month_date"))

            write_corr_signals(conn, TIER, dataset_pair, signals)
            print(f"[OK] Tier 1 {dataset_pair}: {len(signals)} signals")
            all_signals.extend(signals)

        return all_signals
    finally:
        conn.close()


if __name__ == "__main__":
    run_tier1_region_correlations()
