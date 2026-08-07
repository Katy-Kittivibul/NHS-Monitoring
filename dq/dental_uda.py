"""Stage 1 data quality checks for dental UDA activity."""
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

DATASET = "dental_uda"
TABLE = "fact_dental_uda"


def run_dental_uda_dq(db_path: str = "data/processed/nhs.duckdb") -> list[dict]:
    """Run all Stage 1 DQ checks for dental UDA activity and write flags."""
    conn = get_connection(db_path)
    try:
        ensure_dq_schema_exists(conn)

        flags = []
        flags += check_row_count_jump(conn, DATASET, TABLE)
        flags += check_null_rate_jump(
            conn, DATASET, TABLE,
            columns=["icb_name", "provider_name", "uda_delivered"],
        )
        flags += check_out_of_range(conn, DATASET, TABLE, "uda_target", min_val=0)
        flags += check_out_of_range(conn, DATASET, TABLE, "uda_delivered", min_val=0)
        flags += check_out_of_range(conn, DATASET, TABLE, "band_1_delivered", min_val=0)
        flags += check_out_of_range(conn, DATASET, TABLE, "band_2_delivered", min_val=0)
        flags += check_out_of_range(conn, DATASET, TABLE, "band_3_delivered", min_val=0)
        flags += check_out_of_range(conn, DATASET, TABLE, "band_urgent_delivered", min_val=0)
        flags += check_org_missing_vs_prior_month(conn, DATASET, TABLE, org_col="contract_number")
        # check_org_never_reported runs at ICB grain, not contract_number: dim_organisation
        # has no org_type for individual dental providers (only ICBs, via icb_code), so
        # contract_number can't be checked against it. ICB is meaningful, though: ICB rows
        # can enter dim_organisation from other pipelines now (e.g. etl/prescribing.py's
        # dim_sub_icb bridge), so an ICB with zero fact_dental_uda rows is a real, catchable gap.
        flags += check_org_never_reported(conn, DATASET, TABLE, org_col="icb_code", dim_org_type="icb")

        write_flags(conn, flags)
        print(f"[OK] DQ checks complete for {DATASET}: {len(flags)} flags written")
        return flags
    finally:
        conn.close()


if __name__ == "__main__":
    run_dental_uda_dq()
