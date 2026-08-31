"""Per-fact monthly series fetchers for Stage 3 correlation, at the two
grains Tier 1 (region) and Tier 2 (trust) need.

Every ratio here is reaggregated from its numerator/denominator sums
(SUM(breaches)/SUM(attendances), not AVG(breach_pct)) — averaging
already-computed percentages across trusts of very different sizes would
distort the regional trend (Simpson's-paradox-style), and A&E/RTT already
carry the raw counts needed to do this properly.

Each function returns a long-form DataFrame with columns [key, month_date,
value], where `key` is a region_code (Tier 1) or org_code (Tier 2) —
consistent naming so corr/correlation.py's rolling_correlation() doesn't
need per-dataset column-name plumbing.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd

from corr.geography import get_icb_region_map, get_trust_region_map  # noqa: E402


def get_ae_breach_pct_by_region(conn) -> pd.DataFrame:
    trust_region_map = get_trust_region_map(conn)  # noqa: F841 — referenced by name in SQL below
    return conn.execute("""
        SELECT r.region_code AS key, f.month_date,
               SUM(f.total_breaches_4hr) * 100.0 / NULLIF(SUM(f.total_attendances), 0) AS value
        FROM fact_ae_attendances f
        JOIN trust_region_map r ON f.org_code = r.org_code
        GROUP BY r.region_code, f.month_date
    """).df()


def get_ae_breach_pct_by_trust(conn) -> pd.DataFrame:
    return conn.execute("""
        SELECT org_code AS key, month_date, breach_pct AS value
        FROM fact_ae_attendances
    """).df()


def get_rtt_over18wk_pct_by_region(conn) -> pd.DataFrame:
    trust_region_map = get_trust_region_map(conn)  # noqa: F841
    return conn.execute("""
        SELECT r.region_code AS key, f.month_date,
               SUM(f.over_18wk_count) * 100.0 / NULLIF(SUM(f.waiting_list_size), 0) AS value
        FROM fact_rtt_waits f
        JOIN trust_region_map r ON f.org_code = r.org_code
        WHERE f.specialty_code = 'C_999'
        GROUP BY r.region_code, f.month_date
    """).df()


def get_rtt_over18wk_pct_by_trust(conn) -> pd.DataFrame:
    return conn.execute("""
        SELECT org_code AS key, month_date,
               over_18wk_count * 100.0 / NULLIF(waiting_list_size, 0) AS value
        FROM fact_rtt_waits
        WHERE specialty_code = 'C_999'
    """).df()


def get_prescribing_items_by_region(conn) -> pd.DataFrame:
    icb_region_map = get_icb_region_map()  # noqa: F841
    return conn.execute("""
        SELECT ir.region_code AS key, fp.month_date, SUM(fp.items) AS value
        FROM fact_prescribing fp
        JOIN dim_sub_icb ds ON fp.sub_icb_code = ds.sub_icb_code
        JOIN icb_region_map ir ON ds.icb_code = ir.icb_code
        GROUP BY ir.region_code, fp.month_date
    """).df()


def get_dental_pct_achieved_by_region(conn) -> pd.DataFrame:
    icb_region_map = get_icb_region_map()  # noqa: F841
    return conn.execute("""
        SELECT ir.region_code AS key, fd.month_date,
               SUM(fd.uda_delivered) * 100.0 / NULLIF(SUM(fd.uda_target), 0) AS value
        FROM fact_dental_uda fd
        JOIN icb_region_map ir ON fd.icb_code = ir.icb_code
        GROUP BY ir.region_code, fd.month_date
    """).df()
