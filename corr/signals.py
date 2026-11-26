"""Shared primitives for Stage 3 correlation signals — schema + idempotent
writes, mirroring cp/signals.py's role for Stage 2.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd

from etl.common import get_connection  # noqa: E402 — reuse the existing connection factory

# p_value / n_obs / ci_* come from the correlation itself; q_value is the Benjamini-Hochberg
# adjusted p within its test family (corr/inference.py) and is_significant_fdr is q <= 0.05.
INFERENCE_COLUMNS = [
    ("p_value", "DOUBLE"), ("n_obs", "INTEGER"), ("ci_low", "DOUBLE"), ("ci_high", "DOUBLE"),
    ("q_value", "DOUBLE"), ("is_significant_fdr", "BOOLEAN"),
]
SIGNAL_COLUMNS = ["tier", "dataset_pair", "geography_level", "geography_code", "month_date",
                  "correlation_metric", "detail"] + [c for c, _ in INFERENCE_COLUMNS]

__all__ = [
    "get_connection",
    "ensure_corr_schema_exists",
    "signal_row",
    "write_corr_signals",
]


def ensure_corr_schema_exists(conn):
    """Create the corr_signals table and its per-tier views if they don't already exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS corr_signals (
            tier VARCHAR,
            dataset_pair VARCHAR,
            geography_level VARCHAR,
            geography_code VARCHAR,
            month_date DATE,
            correlation_metric DOUBLE,
            detail VARCHAR,
            PRIMARY KEY (tier, dataset_pair, geography_code, month_date)
        )
    """)
    # Inference columns (Phase 2). Databases created before then lack them; ADD COLUMN IF NOT
    # EXISTS keeps old files usable. Rows written before the upgrade have NULLs here until
    # the stage is re-run.
    for column, ddl in INFERENCE_COLUMNS:
        conn.execute(f"ALTER TABLE corr_signals ADD COLUMN IF NOT EXISTS {column} {ddl}")
    # The dashboard reads tier 1 and tier 2 through these views rather than
    # filtering corr_signals inline. Tier literals are defined in
    # corr/tier1_region.py and corr/tier2_trust.py; keep them in step.
    conn.execute("""
        CREATE OR REPLACE VIEW corr_signals_tier1 AS
        SELECT * FROM corr_signals WHERE tier = 'tier1_region'
    """)
    conn.execute("""
        CREATE OR REPLACE VIEW corr_signals_tier2 AS
        SELECT * FROM corr_signals WHERE tier = 'tier2_trust'
    """)


def signal_row(tier: str, dataset_pair: str, geography_level: str, result: dict) -> dict:
    """A corr_signals row from one rolling_correlation() result."""
    return {
        "tier": tier, "dataset_pair": dataset_pair, "geography_level": geography_level,
        "geography_code": result["key"], "month_date": result["month_date"],
        "correlation_metric": result["correlation_metric"], "detail": result["detail"],
        "p_value": result["p_value"], "n_obs": result["n_obs"],
        "ci_low": result["ci_low"], "ci_high": result["ci_high"],
    }


def write_corr_signals(conn, tier: str, dataset_pair: str, signals: list[dict]):
    """Idempotently write signals for one (tier, dataset_pair) pair.

    Replaces all existing corr_signals rows for that pair, since each run
    recomputes correlations from scratch. Deletes first even when `signals`
    is empty, so a geography that no longer has a full window doesn't leave
    a stale signal behind.
    """
    conn.execute("DELETE FROM corr_signals WHERE tier = ? AND dataset_pair = ?", [tier, dataset_pair])
    if not signals:
        return
    df = pd.DataFrame(signals).reindex(columns=SIGNAL_COLUMNS)  # noqa: F841 — used by name in SQL
    conn.execute(f"INSERT INTO corr_signals ({', '.join(SIGNAL_COLUMNS)}) SELECT * FROM df")
