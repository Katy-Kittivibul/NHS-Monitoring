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
    "get_series",
    "series_dataset",
    "MIN_MONTHLY_VOLUME",
    "HEADLINE_DATASET",
]

# The headline series every downstream consumer (Tier 3, the dashboard) has always meant by
# "a trust with a signal": raw A&E breach share. Peer-relative and RTT signals live under other
# `dataset` values in the same table, so anything that wants only the headline filters on this.
HEADLINE_DATASET = "ae_attendances"

# A trust whose MEDIAN monthly volume is below this floor is not monitored: its percentage is
# dominated by a handful of patients (a specialist unit with ~0 attendances "breaches" by
# going from 0% to 25%). The floor is written into every signal's detail so it is auditable.
MIN_MONTHLY_VOLUME = {"ae_attendances": 1000, "rtt_waits": 500}

# dataset -> SELECT of one row per provider-month: value is the monitored percentage, volume its
# denominator (for the floor) and numerator what the national weighted benchmark sums.
_SERIES_SQL = {
    "ae_attendances": """
        SELECT org_code, month_date, breach_pct AS value, total_attendances AS volume,
               total_breaches_4hr AS numerator
        FROM fact_ae_attendances""",
    "rtt_waits": """
        SELECT org_code, month_date,
               over_18wk_count * 100.0 / NULLIF(waiting_list_size, 0) AS value,
               waiting_list_size AS volume, over_18wk_count AS numerator
        FROM fact_rtt_waits WHERE specialty_code = 'C_999'""",
}


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
    df = pd.DataFrame(signals)  # noqa: F841 — referenced by name in SQL below
    conn.execute("INSERT INTO cp_signals SELECT * FROM df")


def get_ae_breach_pct_series(conn) -> pd.DataFrame:
    """A&E breach_pct time series per trust, ordered by org_code then month."""
    return conn.execute("""
        SELECT org_code, org_name, month_date, breach_pct
        FROM fact_ae_attendances
        ORDER BY org_code, month_date
    """).df()


def series_dataset(dataset: str, peer: bool) -> str:
    """The cp_signals.dataset value for a series: 'ae_attendances' (raw) or 'ae_attendances_peer'."""
    return f"{dataset}_peer" if peer else dataset


def get_series(conn, dataset: str, peer: bool = False) -> pd.DataFrame:
    """Per-provider monthly series for Stage 2: columns [org_code, month_date, value].

    peer=False: the provider's own percentage (A&E breach %, RTT % waiting over 18 weeks).
    peer=True : that percentage minus the national volume-weighted percentage for the same
                month, in percentage points. A pattern every trust shares (winter pressure, a
                national surge) cancels out, so what is left is how the trust moves relative
                to its peers, without estimating a seasonal model from ~2.5 cycles of data.
    Providers whose median monthly volume is under MIN_MONTHLY_VOLUME are dropped.
    """
    floor = int(MIN_MONTHLY_VOLUME[dataset])
    value_sql = "s.value - n.national_value" if peer else "s.value"
    return conn.execute(f"""
        WITH s AS ({_SERIES_SQL[dataset]}),
        national AS (
            SELECT month_date, SUM(numerator) * 100.0 / NULLIF(SUM(volume), 0) AS national_value
            FROM s GROUP BY month_date
        ),
        eligible AS (
            SELECT org_code FROM s GROUP BY org_code HAVING MEDIAN(volume) >= {floor}
        )
        SELECT s.org_code, s.month_date, {value_sql} AS value
        FROM s JOIN eligible USING (org_code) JOIN national n USING (month_date)
        WHERE s.value IS NOT NULL
        ORDER BY s.org_code, s.month_date
    """).df()
