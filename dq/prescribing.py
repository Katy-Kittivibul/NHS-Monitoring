"""Stage 1 data quality checks for GP prescribing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dq.checks import (
    check_null_rate_jump,
    check_org_missing_vs_prior_month,
    check_org_never_reported,
    check_out_of_range,
    check_row_count_jump,
    ensure_dq_schema_exists,
    get_connection,
    write_flags,
)

DATASET = "prescribing"
TABLE = "fact_prescribing"


def run_prescribing_dq(db_path: str = "data/processed/nhs.duckdb") -> list[dict]:
    """Run all Stage 1 DQ checks for GP prescribing and write flags."""
    conn = get_connection(db_path)
    try:
        ensure_dq_schema_exists(conn)

        flags = []
        flags += check_row_count_jump(conn, DATASET, TABLE)
        flags += check_null_rate_jump(
            conn, DATASET, TABLE,
            columns=["practice_name", "sub_icb_code", "items", "actual_cost"],
        )
        flags += check_out_of_range(conn, DATASET, TABLE, "items", min_val=0)
        flags += check_out_of_range(conn, DATASET, TABLE, "quantity", min_val=0)
        flags += check_out_of_range(conn, DATASET, TABLE, "actual_cost", min_val=0)
        flags += check_org_missing_vs_prior_month(conn, DATASET, TABLE, org_col="practice_code")
        # check_org_never_reported is structurally a no-op here: dim_organisation's
        # org_type='practice' roster is populated exclusively from fact_prescribing
        # itself (etl/prescribing.py upserts every practice_code it sees), so no
        # practice can ever be "in dim_organisation but never in fact_prescribing".
        # Kept for consistency — becomes meaningful automatically if that ever changes.
        flags += check_org_never_reported(
            conn, DATASET, TABLE, org_col="practice_code", dim_org_type="practice"
        )

        write_flags(conn, flags)
        print(f"[OK] DQ checks complete for {DATASET}: {len(flags)} flags written")
        return flags
    finally:
        conn.close()


if __name__ == "__main__":
    run_prescribing_dq()
