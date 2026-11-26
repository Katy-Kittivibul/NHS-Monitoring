"""Builds corr/reference/trust_to_icb.csv — trust -> ICB resolved from NHS
ODS's dated organisation relationships, time-scoped to the Mar2024-Mar2025
reporting window Tier 3 needs.

This is deliberately NOT today's live ODS relationship: NHS England is
mid-way through an ICB merger wave (the same one dim_sub_icb's Thames Valley
patch and corr/reference/icb_to_region.csv's S0E4D exception both work
around), so a trust's *current* parent ICB is often a brand-new merged code
that didn't exist during the reporting period and isn't in dim_organisation.
Each trust's ODS relationship (id RE5/RE8) is dated, so the fix is to pick
whichever relationship(s) overlap the report window, not whichever is
"Active" today.

Only the RE8 ("IS PARTNER TO" an ICB) relationship is used — not RE5 ("IS
LOCATED IN THE GEOGRAPHY OF"), which is a single geographic pointer whose
targets aren't even guaranteed to be ICB codes (older trusts' RE5 history
includes pre-ICB region codes). NHS ODS documents RE8 as explicitly
allowing more than one simultaneously-active relationship per trust, per
each partner ICB's constitution — so a trust like RYX (Central London
Community Healthcare) genuinely, correctly has 3 concurrent RE8 partners
in our window (QRV, QMJ, QWE), not a data error. Those are flagged
(multi_icb=True, one row per candidate ICB) rather than silently resolved
to a single ICB, so Tier 3 can decide not to attribute their signal to any
one ICB rather than attributing it to an arbitrary one.

Only resolves the trust universe Tier 3 actually needs — every trust with
at least one cp_signals row, plus fact_ae_attendances trusts with
specialist_no_ae = FALSE and zero cp_signals rows (Question B's "flat but
eligible" set) — not the full 139/200-trust roster corr/'s Tier 1/2 uses.
"""
import json
import re
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

from cp.signals import HEADLINE_DATASET  # noqa: E402
from etl.common import get_connection  # noqa: E402

ODS_BASE = "https://directory.spineservices.nhs.uk/ORD/2-0-0/organisations"
REPORT_START = date(2024, 3, 1)
REPORT_END = date(2025, 3, 1)
DEFAULT_OUT_PATH = str(Path(__file__).parent / "trust_to_icb.csv")

RELATIONSHIP_ID = "RE8"  # "IS PARTNER TO" an ICB — RE5 ("IS LOCATED IN THE GEOGRAPHY
# OF") is excluded: it's a single geographic pointer, not an ICB-partnership
# relationship, and its targets historically include non-ICB geography codes.

# RE8 isn't exclusively used for trust->ICB partnerships — non-hospital orgs on
# the trust roster (walk-in centres, GP-affiliated health centres, mislabeled
# org_type='trust' in dim_organisation) can carry an RE8 to what turns out to be
# a Primary Care Network, not an ICB (e.g. "U22426" — confirmed empirically:
# every genuine ICB code seen across this project is either the classic 3-char
# "Q" + 2 chars form (QMF, QRV, ...) or the 5-char merged-ICB form introduced by
# the 2026 reorg (S0E4D, Z9B2Z, S1Y5D); PCN-style codes ("U" + 5 digits) match
# neither and are rejected rather than silently treated as an ICB.
_ICB_CODE_PATTERN = re.compile(r"^Q[A-Z0-9]{2}$|^[A-Z]\d[A-Z]\d[A-Z]$")


def get_trust_universe(conn) -> list[str]:
    """cp_signals trusts, union Question B's eligible no-signal/non-specialist trusts."""
    signal_trusts = set(conn.execute(
        f"SELECT DISTINCT org_code FROM cp_signals WHERE dataset = '{HEADLINE_DATASET}'").df()["org_code"])
    eligible = set(conn.execute(f"""
        SELECT org_code FROM dim_organisation
        WHERE org_type = 'trust' AND specialist_no_ae = FALSE
          AND org_code NOT IN (SELECT DISTINCT org_code FROM cp_signals WHERE dataset = '{HEADLINE_DATASET}')
    """).df()["org_code"])
    return sorted(signal_trusts | eligible)


def _parse_date(d):
    return date.fromisoformat(d) if d else None


def fetch_org_relationships(org_code: str, timeout: float = 15.0) -> list[dict]:
    """Fetch this trust's dated RE8 ("IS PARTNER TO" an ICB) relationships from
    the live NHS ODS API."""
    url = f"{ODS_BASE}/{org_code}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)

    rels = data.get("Organisation", {}).get("Rels", {}).get("Rel", [])
    out = []
    for rel in rels:
        if rel.get("id") != RELATIONSHIP_ID:
            continue
        target = rel.get("Target", {}).get("OrgId", {}).get("extension")
        if not target:
            continue
        date_entries = {d["Type"]: d for d in rel.get("Date", [])}
        span = date_entries.get("Operational") or date_entries.get("Legal") or {}
        out.append({
            "icb_code": target,
            "start": _parse_date(span.get("Start")),
            "end": _parse_date(span.get("End")),
        })
    return out


def resolve_icb_for_window(rels: list[dict], window_start: date = REPORT_START,
                            window_end: date = REPORT_END) -> list[str]:
    """ICB-shaped codes with a relationship overlapping [window_start, window_end] at all."""
    active = set()
    for rel in rels:
        start = rel["start"] or date.min
        end = rel["end"] or date.max
        if start <= window_end and end >= window_start and _ICB_CODE_PATTERN.match(rel["icb_code"]):
            active.add(rel["icb_code"])
    return sorted(active)


def build_trust_to_icb(db_path: str = "data/processed/nhs.duckdb",
                        out_path: str = DEFAULT_OUT_PATH,
                        sleep_s: float = 0.1) -> pd.DataFrame:
    """Resolve ICB(s) for the Tier 3 trust universe and write corr/reference/trust_to_icb.csv."""
    conn = get_connection(db_path)
    try:
        trusts = get_trust_universe(conn)
    finally:
        conn.close()

    rows = []
    for i, trust_code in enumerate(trusts, 1):
        try:
            rels = fetch_org_relationships(trust_code)
            icbs = resolve_icb_for_window(rels)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
            rows.append({"trust_code": trust_code, "icb_code": None, "multi_icb": False,
                         "resolution_status": f"error: {e}"})
            print(f"[{i}/{len(trusts)}] {trust_code}: ERROR {e}")
            continue

        if len(icbs) == 0:
            rows.append({"trust_code": trust_code, "icb_code": None, "multi_icb": False,
                         "resolution_status": "no_icb_relationship_in_window"})
        elif len(icbs) == 1:
            rows.append({"trust_code": trust_code, "icb_code": icbs[0], "multi_icb": False,
                         "resolution_status": "resolved"})
        else:
            for icb_code in icbs:
                rows.append({"trust_code": trust_code, "icb_code": icb_code, "multi_icb": True,
                             "resolution_status": "multi_icb"})

        print(f"[{i}/{len(trusts)}] {trust_code}: {icbs if icbs else '(none)'}")
        if sleep_s:
            time.sleep(sleep_s)

    df = pd.DataFrame(rows, columns=["trust_code", "icb_code", "multi_icb", "resolution_status"])
    df.to_csv(out_path, index=False)
    n_multi = df.loc[df["multi_icb"], "trust_code"].nunique()
    n_none = (df["resolution_status"] == "no_icb_relationship_in_window").sum()
    n_error = df["resolution_status"].str.startswith("error:").sum()
    print(f"[OK] wrote {len(df)} rows ({df['trust_code'].nunique()} trusts) to {out_path} "
          f"— {n_multi} multi-ICB, {n_none} unresolved, {n_error} errors")
    return df


def load_trust_to_icb(path: str = DEFAULT_OUT_PATH) -> pd.DataFrame:
    """Read the cached trust->ICB resolution — Tier 3 consumers should use this,
    not re-hit the ODS API on every run."""
    return pd.read_csv(path)


if __name__ == "__main__":
    build_trust_to_icb()
