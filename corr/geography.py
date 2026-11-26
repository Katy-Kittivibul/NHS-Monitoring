"""Region resolution for Stage 3 Tier 1 — the geography every fact table
already reaches, one way or another.

dim_organisation.region_name used to be an unclean key: etl/ae_attendances.py
wrote NHS England region *names* ("NHS ENGLAND LONDON") and etl/rtt_waits.py wrote
region *codes* ("Y56") into the same column, whichever loaded last winning. Both
loads now write the canonical pair (region_code + region_name) via
etl.common.normalise_region, but databases built before that change still hold
mixed values, so the normalisation below is kept (REGION_FULL_NAME_TO_CODE,
imported from etl.common — 7 codes, 7 names, 1:1).

dim_organisation has no ICB->region path at all (every ICB row's region_name
is NULL) — get_icb_region_map() fills that gap from corr/reference/
icb_to_region.csv, sourced from the same ONS April-2024 Sub-ICB->ICB->NHSER
lookup dim_sub_icb uses (see etl/reference/sub_icb_to_icb.csv), plus one
manual row for S0E4D (NHS Thames Valley), which postdates that lookup.
"""
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd

from etl.common import REGION_FULL_NAME_TO_CODE  # noqa: F401, E402 — single source of truth, re-exported

# Resolved relative to this module, not the CWD: this is a repo-tracked reference
# file, so it must load identically whether the pipeline is run from the repo
# root, from Docker (WORKDIR /app), or from pytest's rootdir. Same pattern as
# corr/reference/trust_to_icb.py's DEFAULT_OUT_PATH.
DEFAULT_ICB_REGION_PATH = str(Path(__file__).parent / "reference" / "icb_to_region.csv")

_REGION_CODE_PATTERN = re.compile(r"^Y5[6-9]$|^Y6[0-3]$")


def get_trust_region_map(conn) -> pd.DataFrame:
    """org_code -> region_code for every trust with a resolvable region_name."""
    trusts = conn.execute("""
        SELECT org_code, region_name
        FROM dim_organisation
        WHERE org_type = 'trust' AND region_name IS NOT NULL
    """).df()

    def normalise(region_name: str) -> str | None:
        region_name = region_name.strip()
        if _REGION_CODE_PATTERN.match(region_name):
            return region_name
        return REGION_FULL_NAME_TO_CODE.get(region_name.upper())

    trusts["region_code"] = trusts["region_name"].apply(normalise)
    resolved = trusts.dropna(subset=["region_code"])[["org_code", "region_code"]]
    return resolved.reset_index(drop=True)


def get_icb_region_map(mapping_path: str = DEFAULT_ICB_REGION_PATH) -> pd.DataFrame:
    """icb_code -> region_code / region_name, from the static ONS-sourced reference file."""
    return pd.read_csv(mapping_path)[["icb_code", "region_code", "region_name"]]
