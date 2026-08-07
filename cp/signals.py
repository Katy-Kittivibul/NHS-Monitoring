"""Shared primitives for Stage 2 changepoint/SPC signal detection.

Both cp/spc_baseline.py (Part A) and cp/changepoints.py (Part B) run against
the same per-trust breach_pct series and write into one shared cp_signals
table, so the DB plumbing and series-fetch query live here once.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd

from etl.common import get_connection  # noqa: E402 — reuse the existing connection factory

__all__ = [
    "get_connection",
    "ensure_cp_schema_exists",
    "write_signals",
    "get_ae_breach_pct_series",
]


def ensure_cp_schema_exists(conn):
    """Create the cp_signals table if it doesn't already exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cp_signals (
            dataset VARCHAR,
            org_code VARCHAR,
            month_date DATE,
            method VARCHAR,
            signal_type VARCHAR,
            detail VARCHAR,
            PRIMARY KEY (dataset, org_code, month_date, method, signal_type)
        )
    """)


def write_signals(conn, dataset: str, method: str, signals: list[dict]):
    """Idempotently write signals for one (dataset, method) pair.

    Replaces all existing cp_signals rows for that pair, since each run
    recomputes signals from scratch for every trust currently in the data.
    Deletes first even when `signals` is empty, so a trust/period that no
    longer triggers a rule doesn't leave a stale signal behind.
    """
    conn.execute("DELETE FROM cp_signals WHERE dataset = ? AND method = ?", [dataset, method])
    if not signals:
        return
    df = pd.DataFrame(signals)
    conn.execute("INSERT INTO cp_signals SELECT * FROM df")


def get_ae_breach_pct_series(conn) -> pd.DataFrame:
    """A&E breach_pct time series per trust, ordered by org_code then month."""
    return conn.execute("""
        SELECT org_code, org_name, month_date, breach_pct
        FROM fact_ae_attendances
        ORDER BY org_code, month_date
    """).df()
