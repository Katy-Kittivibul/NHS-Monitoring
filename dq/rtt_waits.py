"""Stage 1 data quality checks for RTT waiting times."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dq.checks import (
    get_connection,
    ensure_dq_schema_exists,
    write_flags,
    check_row_count_jump,
    check_null_rate_jump,
    check_out_of_range,
    check_org_missing_vs_prior_month,
    check_org_never_reported,
)

DATASET = "rtt_waits"
TABLE = "fact_rtt_waits"


def run_rtt_waits_dq(db_path: str = "data/processed/nhs.duckdb") -> list[dict]:
    """Run all Stage 1 DQ checks for RTT waiting times and write flags."""
    conn = get_connection(db_path)
    try:
        ensure_dq_schema_exists(conn)

        flags = []
        flags += check_row_count_jump(conn, DATASET, TABLE)
        flags += check_null_rate_jump(
            conn, DATASET, TABLE,
            columns=["org_name", "specialty_name", "waiting_list_size", "over_18wk_pct"],
        )
        flags += check_out_of_range(conn, DATASET, TABLE, "over_18wk_pct", min_val=0, max_val=100)
        flags += check_out_of_range(conn, DATASET, TABLE, "waiting_list_size", min_val=0)
        flags += check_out_of_range(conn, DATASET, TABLE, "over_18wk_count", min_val=0)
        flags += check_out_of_range(conn, DATASET, TABLE, "median_wait_weeks", min_val=0)
        flags += check_org_missing_vs_prior_month(conn, DATASET, TABLE, org_col="org_code")
        flags += check_org_never_reported(conn, DATASET, TABLE, org_col="org_code", dim_org_type="trust")

        write_flags(conn, flags)
        print(f"[OK] DQ checks complete for {DATASET}: {len(flags)} flags written")
        return flags
    finally:
        conn.close()


if __name__ == "__main__":
    run_rtt_waits_dq()
