"""Stage 1 data quality checks for A&E attendances."""
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

DATASET = "ae_attendances"
TABLE = "fact_ae_attendances"


def run_ae_attendances_dq(db_path: str = "data/processed/nhs.duckdb") -> list[dict]:
    """Run all Stage 1 DQ checks for A&E attendances and write flags."""
    conn = get_connection(db_path)
    try:
        ensure_dq_schema_exists(conn)

        flags = []
        flags += check_row_count_jump(conn, DATASET, TABLE)
        flags += check_null_rate_jump(
            conn, DATASET, TABLE,
            columns=["org_name", "total_attendances", "breach_pct"],
        )
        flags += check_out_of_range(conn, DATASET, TABLE, "breach_pct", min_val=0, max_val=100)
        flags += check_out_of_range(conn, DATASET, TABLE, "total_attendances", min_val=0)
        flags += check_out_of_range(conn, DATASET, TABLE, "total_breaches_4hr", min_val=0)
        flags += check_org_missing_vs_prior_month(conn, DATASET, TABLE, org_col="org_code")
        flags += check_org_never_reported(conn, DATASET, TABLE, org_col="org_code", dim_org_type="trust")

        write_flags(conn, flags)
        print(f"[OK] DQ checks complete for {DATASET}: {len(flags)} flags written")
        return flags
    finally:
        conn.close()


if __name__ == "__main__":
    run_ae_attendances_dq()
