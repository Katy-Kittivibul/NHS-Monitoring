"""ICB-grain series fetchers for Stage 3 Tier 3.

Builds on the same fact tables corr/rollups.py uses for Tier 1's region
grain, but stops one level higher — at ICB, not region — since Tier 3
attributes a trust's signal to a single ICB (via corr/reference/
trust_to_icb.py), not to a whole NHS England region.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd


def get_prescribing_items_by_icb(conn) -> pd.DataFrame:
    return conn.execute("""
        SELECT ds.icb_code AS key, fp.month_date, SUM(fp.items) AS value
        FROM fact_prescribing fp
        JOIN dim_sub_icb ds ON fp.sub_icb_code = ds.sub_icb_code
        GROUP BY ds.icb_code, fp.month_date
    """).df()


def get_dental_pct_achieved_by_icb(conn) -> pd.DataFrame:
    return conn.execute("""
        SELECT icb_code AS key, month_date,
               SUM(uda_delivered) * 100.0 / NULLIF(SUM(uda_target), 0) AS value
        FROM fact_dental_uda
        GROUP BY icb_code, month_date
    """).df()
