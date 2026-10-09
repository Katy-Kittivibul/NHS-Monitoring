"""Builds corr/reference/trust_icb_current.csv: every trust in the A&E and RTT fact
tables mapped to its ICB(s) as at its own last reporting month.

Unlike trust_to_icb.csv (a fixed Mar2024-Mar2025 window sized for Tier 3, and only the
Tier 3 trust universe), this covers all trusts in the fact tables and picks the
RE8 ("IS PARTNER TO") ICB relationship that was in force in the trust's last month of
data, so a trust that moved into a merged ICB during 2026 maps to the ICB it reports
under now. ICB names are looked up from ODS because merged codes (e.g. Z9B2Z) are not
in icb_to_region.csv. A trust with no ICB relationship (walk-in centres, UTCs,
independent providers) gets one row with a NULL icb_code and status
'no_icb_relationship'; it is labelled "Unmapped" downstream, never dropped.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd

from corr.reference.trust_to_icb import _ICB_CODE_PATTERN, ODS_BASE, fetch_org_relationships  # noqa: E402
from etl.common import get_connection  # noqa: E402

DEFAULT_OUT_PATH = str(Path(__file__).parent / "trust_icb_current.csv")
COLUMNS = ["trust_code", "as_of_month", "icb_code", "icb_name", "n_icbs", "resolution_status"]


def icbs_in_force(rels: list[dict], as_of: date) -> list[str]:
    """ICB-shaped partner codes whose relationship was in force on `as_of`."""
    return sorted({
        r["icb_code"] for r in rels
        if (r["start"] or date.min) <= as_of <= (r["end"] or date.max)
        and _ICB_CODE_PATTERN.match(r["icb_code"])
    })


def fetch_org_name(code: str, timeout: float = 15.0) -> str | None:
    req = urllib.request.Request(f"{ODS_BASE}/{code}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp).get("Organisation", {}).get("Name")


def build(db_path: str = "data/processed/nhs.duckdb", out_path: str = DEFAULT_OUT_PATH,
          sleep_s: float = 0.1) -> pd.DataFrame:
    conn = get_connection(db_path)
    try:
        last = conn.execute("""
            SELECT org_code, MAX(month_date) AS last_month FROM (
                SELECT org_code, month_date FROM fact_ae_attendances
                UNION ALL SELECT org_code, month_date FROM fact_rtt_waits
            ) GROUP BY org_code ORDER BY org_code
        """).fetchall()
    finally:
        conn.close()

    names: dict[str, str | None] = {}
    rows = []
    for i, (trust, last_month) in enumerate(last, 1):
        try:
            icbs = icbs_in_force(fetch_org_relationships(trust), last_month)
            status = "no_icb_relationship" if not icbs else ("resolved" if len(icbs) == 1 else "multi_icb")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            icbs, status = [], f"error: {e}"
        for icb in icbs or [None]:
            if icb and icb not in names:
                try:
                    names[icb] = fetch_org_name(icb)
                except (urllib.error.URLError, TimeoutError, ValueError):
                    names[icb] = None
            rows.append({"trust_code": trust, "as_of_month": last_month, "icb_code": icb,
                         "icb_name": names.get(icb) if icb else None, "n_icbs": len(icbs),
                         "resolution_status": status})
        print(f"[{i}/{len(last)}] {trust}: {icbs or '(none)'}")
        time.sleep(sleep_s)

    df = pd.DataFrame(rows, columns=COLUMNS)
    df.to_csv(out_path, index=False)
    print(df.groupby("resolution_status")["trust_code"].nunique())
    return df


if __name__ == "__main__":
    build()
