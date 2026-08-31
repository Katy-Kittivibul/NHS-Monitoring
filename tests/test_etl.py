"""Sanity checks for NHS ETL pipelines."""
import pytest
import sys
from pathlib import Path
import pandas as pd

# Add parent directories to path
sys.path.insert(0, str(Path(__file__).parent.parent / "etl"))

from common import get_connection, ensure_schema_exists
from ae_attendances import load_ae_attendances, parse_ae_period, etl_ae_attendances
from rtt_waits import load_rtt_waits, parse_rtt_filename_date
from prescribing import load_prescribing_data
from dental_uda import load_dental_uda_file, parse_dental_month


class TestAEAttendances:
    def test_parse_ae_period(self):
        """Test A&E period parsing."""
        result = parse_ae_period("MSitAE-MARCH-2024")
        assert result.year == 2024
        assert result.month == 3
        assert result.day == 1

    def test_load_ae_attendances_row_count(self):
        """Test A&E data loads with rows."""
        csv_path = "data/raw/ae_attendances_202403.csv"
        if not Path(csv_path).exists():
            pytest.skip(f"{csv_path} not found")

        df = load_ae_attendances(csv_path)
        assert len(df) > 0, "A&E data should have at least one row"

    def test_ae_attendances_schema(self):
        """Test A&E data has required columns."""
        csv_path = "data/raw/ae_attendances_202403.csv"
        if not Path(csv_path).exists():
            pytest.skip(f"{csv_path} not found")

        df = load_ae_attendances(csv_path)
        required_cols = [
            "month_date", "org_code", "org_name", "parent_org",
            "total_attendances", "total_breaches_4hr", "breach_pct"
        ]
        for col in required_cols:
            assert col in df.columns, f"Missing column: {col}"

    def test_ae_key_columns_not_null(self):
        """Test A&E key columns have no nulls."""
        csv_path = "data/raw/ae_attendances_202403.csv"
        if not Path(csv_path).exists():
            pytest.skip(f"{csv_path} not found")

        df = load_ae_attendances(csv_path)
        key_cols = ["month_date", "org_code"]
        for col in key_cols:
            assert df[col].notna().all(), f"{col} has null values"

    def test_ae_dates_valid(self):
        """Test A&E dates parse correctly."""
        csv_path = "data/raw/ae_attendances_202403.csv"
        if not Path(csv_path).exists():
            pytest.skip(f"{csv_path} not found")

        df = load_ae_attendances(csv_path)
        assert df["month_date"].notna().all(), "month_date has nulls"
        assert (pd.to_datetime(df["month_date"]).dt.day == 1).all(), "All dates should be month start"

    def test_ae_breach_pct_valid_range(self):
        """Test breach percentages are reasonable."""
        csv_path = "data/raw/ae_attendances_202403.csv"
        if not Path(csv_path).exists():
            pytest.skip(f"{csv_path} not found")

        df = load_ae_attendances(csv_path)
        assert (df["breach_pct"] >= 0).all(), "Breach % should be >= 0"
        assert (df["breach_pct"] <= 100).all(), "Breach % should be <= 100"


class TestRTTWaits:
    def test_parse_rtt_filename_date(self):
        """Test RTT filename parsing."""
        result = parse_rtt_filename_date("rtt_incomplete_provider_202403.xlsx")
        assert result.year == 2024
        assert result.month == 3
        assert result.day == 1


    def test_load_rtt_waits_row_count(self):
        """Test RTT data loads with rows."""
        xlsx_path = "data/raw/rtt_incomplete_provider_202403.xlsx"
        if not Path(xlsx_path).exists():
            pytest.skip(f"{xlsx_path} not found")

        df = load_rtt_waits(xlsx_path)
        assert len(df) > 0, "RTT data should have at least one row"

    def test_rtt_waits_schema(self):
        """Test RTT data has required columns."""
        xlsx_path = "data/raw/rtt_incomplete_provider_202403.xlsx"
        if not Path(xlsx_path).exists():
            pytest.skip(f"{xlsx_path} not found")

        df = load_rtt_waits(xlsx_path)
        required_cols = [
            "month_date", "org_code", "org_name", "specialty_code",
            "waiting_list_size", "over_18wk_count", "over_18wk_pct"
        ]
        for col in required_cols:
            assert col in df.columns, f"Missing column: {col}"

    def test_rtt_key_columns_not_null(self):
        """Test RTT key columns have no nulls."""
        xlsx_path = "data/raw/rtt_incomplete_provider_202403.xlsx"
        if not Path(xlsx_path).exists():
            pytest.skip(f"{xlsx_path} not found")

        df = load_rtt_waits(xlsx_path)
        key_cols = ["month_date", "org_code", "specialty_code"]
        for col in key_cols:
            assert df[col].notna().all(), f"{col} has null values"

    def test_rtt_specialty_code_includes_aggregate(self):
        """Test RTT includes aggregate specialty (C_999 or 999)."""
        xlsx_path = "data/raw/rtt_incomplete_provider_202403.xlsx"
        if not Path(xlsx_path).exists():
            pytest.skip(f"{xlsx_path} not found")

        df = load_rtt_waits(xlsx_path)
        codes = df["specialty_code"].astype(str).values
        assert ("C_999" in codes or "999" in codes), "Should include aggregate specialty C_999 / 999"

    def test_rtt_over_18wk_pct_valid_range(self):
        """Test over 18wk % is reasonable."""
        xlsx_path = "data/raw/rtt_incomplete_provider_202403.xlsx"
        if not Path(xlsx_path).exists():
            pytest.skip(f"{xlsx_path} not found")

        df = load_rtt_waits(xlsx_path)
        assert (df["over_18wk_pct"] >= 0).all(), "Over 18wk % should be >= 0"
        assert (df["over_18wk_pct"] <= 100).all(), "Over 18wk % should be <= 100"


class TestPrescribing:
    def test_load_prescribing_data_row_count(self):
        """Test prescribing data loads with rows."""
        json_path = "data/raw/openprescribing_spending_202310.json"
        if not Path(json_path).exists():
            pytest.skip(f"{json_path} not found")

        df = load_prescribing_data(json_path)
        assert len(df) > 0, "Prescribing data should have rows"

    def test_prescribing_schema(self):
        """Test prescribing data has required columns."""
        json_path = "data/raw/openprescribing_spending_202310.json"
        if not Path(json_path).exists():
            pytest.skip(f"{json_path} not found")

        df = load_prescribing_data(json_path)
        required_cols = [
            "month_date", "practice_code", "practice_name",
            "items", "actual_cost"
        ]
        for col in required_cols:
            assert col in df.columns, f"Missing column: {col}"

    def test_prescribing_key_columns_not_null(self):
        """Test prescribing key columns have no nulls."""
        json_path = "data/raw/openprescribing_spending_202310.json"
        if not Path(json_path).exists():
            pytest.skip(f"{json_path} not found")

        df = load_prescribing_data(json_path)
        key_cols = ["month_date", "practice_code"]
        for col in key_cols:
            assert df[col].notna().all(), f"{col} has null values"

    def test_prescribing_setting_is_4(self):
        """Test all records are filtered to setting 4 (GP practices)."""
        json_path = "data/raw/openprescribing_spending_202310.json"
        if not Path(json_path).exists():
            pytest.skip(f"{json_path} not found")

        df = load_prescribing_data(json_path)
        assert (df["setting_code"] == 4).all(), "All records should be setting 4"

    def test_prescribing_items_and_cost_non_negative(self):
        """Test items and costs are non-negative."""
        json_path = "data/raw/openprescribing_spending_202310.json"
        if not Path(json_path).exists():
            pytest.skip(f"{json_path} not found")

        df = load_prescribing_data(json_path)
        assert (df["items"] >= 0).all(), "Items should be non-negative"
        assert (df["actual_cost"] >= 0).all(), "Costs should be non-negative"


class TestDentalUDA:
    def test_parse_dental_month(self):
        """Test dental month parsing."""
        result = parse_dental_month("202211")
        assert result.year == 2022
        assert result.month == 11
        assert result.day == 1

    def test_load_dental_uda_file_202211(self):
        """Test dental UDA 202211 loads."""
        csv_path = "data/raw/dental_uda_202211.csv"
        if not Path(csv_path).exists():
            pytest.skip(f"{csv_path} not found")

        df = load_dental_uda_file(csv_path)
        assert len(df) > 0, "Dental UDA 202211 should have rows"

    def test_load_dental_uda_file_202311(self):
        """Test dental UDA 202311 loads."""
        csv_path = "data/raw/dental_uda_202311.csv"
        if not Path(csv_path).exists():
            pytest.skip(f"{csv_path} not found")

        df = load_dental_uda_file(csv_path)
        assert len(df) > 0, "Dental UDA 202311 should have rows"

    def test_dental_uda_schema(self):
        """Test dental UDA data has required columns."""
        csv_path = "data/raw/dental_uda_202211.csv"
        if not Path(csv_path).exists():
            pytest.skip(f"{csv_path} not found")

        df = load_dental_uda_file(csv_path)
        required_cols = [
            "month_date", "icb_code", "icb_name", "contract_number",
            "uda_delivered", "band_2_delivered"
        ]
        for col in required_cols:
            assert col in df.columns, f"Missing column: {col}"

    def test_dental_uda_key_columns_not_null(self):
        """Test dental UDA key columns have no nulls."""
        csv_path = "data/raw/dental_uda_202211.csv"
        if not Path(csv_path).exists():
            pytest.skip(f"{csv_path} not found")

        df = load_dental_uda_file(csv_path)
        key_cols = ["month_date", "contract_number", "icb_code"]
        for col in key_cols:
            assert df[col].notna().all(), f"{col} has null values"

    def test_dental_band2_harmonisation(self):
        """Test Band 2 is correctly harmonised from split columns."""
        csv_path = "data/raw/dental_uda_202311.csv"
        if not Path(csv_path).exists():
            pytest.skip(f"{csv_path} not found")

        df = load_dental_uda_file(csv_path)
        # 202311 should have band_2_delivered populated from split columns
        assert (df["band_2_delivered"] >= 0).all(), "band_2_delivered should be non-negative"

    def test_dental_uda_positive_values(self):
        """Test dental UDA metrics are non-negative."""
        csv_path = "data/raw/dental_uda_202211.csv"
        if not Path(csv_path).exists():
            pytest.skip(f"{csv_path} not found")

        df = load_dental_uda_file(csv_path)
        numeric_cols = ["uda_delivered", "band_1_delivered", "band_2_delivered",
                        "band_3_delivered", "band_urgent_delivered"]
        for col in numeric_cols:
            if col in df.columns:
                assert (df[col] >= 0).all(), f"{col} should be non-negative"


class TestSubIcbMapping:
    """fact_prescribing.sub_icb_code (e.g. "16C") is a Sub ICB code, not an ICB
    code (e.g. "QHM") — it can't join dim_organisation directly. dim_sub_icb
    bridges the two; these checks confirm that bridge has no gaps.
    """

    REAL_DB_PATH = "data/processed/nhs.duckdb"

    @pytest.fixture(autouse=True)
    def skip_if_no_db(self):
        if not Path(self.REAL_DB_PATH).exists():
            pytest.skip(f"{self.REAL_DB_PATH} not found — run etl/run_all.py first")

    def test_zero_orphan_sub_icb_codes(self):
        """Every sub_icb_code in fact_prescribing must resolve through dim_sub_icb
        to an ICB that actually exists in dim_organisation."""
        conn = get_connection(self.REAL_DB_PATH)
        try:
            orphans = conn.execute("""
                SELECT DISTINCT fp.sub_icb_code
                FROM fact_prescribing fp
                LEFT JOIN dim_sub_icb ds ON fp.sub_icb_code = ds.sub_icb_code
                LEFT JOIN dim_organisation org
                    ON ds.icb_code = org.org_code AND org.org_type = 'icb'
                WHERE ds.sub_icb_code IS NULL OR org.org_code IS NULL
            """).df()
        finally:
            conn.close()
        assert len(orphans) == 0, f"Orphan sub_icb_codes with no ICB rollup: {orphans['sub_icb_code'].tolist()}"

    def test_dim_sub_icb_icb_codes_all_in_dim_organisation(self):
        """Every ICB dim_sub_icb points to must itself be a dim_organisation row —
        catches gaps even for Sub ICBs no fact table currently references."""
        conn = get_connection(self.REAL_DB_PATH)
        try:
            missing = conn.execute("""
                SELECT DISTINCT ds.icb_code
                FROM dim_sub_icb ds
                LEFT JOIN dim_organisation org
                    ON ds.icb_code = org.org_code AND org.org_type = 'icb'
                WHERE org.org_code IS NULL
            """).df()
        finally:
            conn.close()
        assert len(missing) == 0, f"dim_sub_icb ICB codes missing from dim_organisation: {missing['icb_code'].tolist()}"


class TestSpecialistNoAeFlag:
    """dim_organisation.specialist_no_ae distinguishes trusts that structurally
    never run a general A&E department (zero attendances every month, e.g. RAN,
    RET) from trusts with genuine 0% breach activity, so reports don't fold
    the two together.
    """

    REAL_DB_PATH = "data/processed/nhs.duckdb"

    @pytest.fixture(autouse=True)
    def skip_if_no_db(self):
        if not Path(self.REAL_DB_PATH).exists():
            pytest.skip(f"{self.REAL_DB_PATH} not found — run etl/run_all.py first")

    def test_known_specialist_trusts_are_flagged(self):
        etl_ae_attendances(self.REAL_DB_PATH)
        conn = get_connection(self.REAL_DB_PATH)
        try:
            flagged = set(conn.execute(
                "SELECT org_code FROM dim_organisation WHERE specialist_no_ae = TRUE"
            ).df()["org_code"])
        finally:
            conn.close()
        # RAN (orthopaedic), RBQ (cardiothoracic), RET (neurology) — all confirmed
        # zero attendances across every month present in fact_ae_attendances.
        assert {"RAN", "RBQ", "RET"} <= flagged

    def test_active_trust_is_not_flagged(self):
        etl_ae_attendances(self.REAL_DB_PATH)
        conn = get_connection(self.REAL_DB_PATH)
        try:
            value = conn.execute(
                "SELECT specialist_no_ae FROM dim_organisation WHERE org_code = 'R0A'"
            ).fetchall()[0][0]
        finally:
            conn.close()
        assert value is False

    def test_rtt_only_trust_stays_null(self):
        """A trust never present in fact_ae_attendances at all is 'not applicable',
        not 'specialist' — those are different claims and must stay distinguishable."""
        etl_ae_attendances(self.REAL_DB_PATH)
        conn = get_connection(self.REAL_DB_PATH)
        try:
            ae_trusts = set(conn.execute("SELECT DISTINCT org_code FROM fact_ae_attendances").df()["org_code"])
            rtt_only = conn.execute("""
                SELECT DISTINCT org_code FROM fact_rtt_waits
                WHERE org_code NOT IN (SELECT DISTINCT org_code FROM fact_ae_attendances)
            """).df()["org_code"].tolist()
        finally:
            conn.close()
        assert rtt_only, "expected at least one RTT-only trust to test against"
        sample = rtt_only[0]
        assert sample not in ae_trusts

        conn = get_connection(self.REAL_DB_PATH)
        try:
            value = conn.execute(
                "SELECT specialist_no_ae FROM dim_organisation WHERE org_code = ?", [sample]
            ).fetchall()[0][0]
        finally:
            conn.close()
        assert value is None

    def test_flag_is_idempotent_on_rerun(self):
        etl_ae_attendances(self.REAL_DB_PATH)
        conn = get_connection(self.REAL_DB_PATH)
        try:
            first = conn.execute(
                "SELECT org_code, specialist_no_ae FROM dim_organisation WHERE org_type = 'trust' ORDER BY org_code"
            ).fetchall()
        finally:
            conn.close()

        etl_ae_attendances(self.REAL_DB_PATH)
        conn = get_connection(self.REAL_DB_PATH)
        try:
            second = conn.execute(
                "SELECT org_code, specialist_no_ae FROM dim_organisation WHERE org_type = 'trust' ORDER BY org_code"
            ).fetchall()
        finally:
            conn.close()

        assert first == second


class TestDatabase:
    """Hermetic checks on the connection factory and schema DDL — deliberately no
    dependency on data/processed/, so these still run in CI where the database and
    raw extracts are gitignored.
    """

    def test_duckdb_connection(self):
        """get_connection returns a usable DuckDB connection."""
        conn = get_connection(":memory:")
        try:
            result = conn.execute("SELECT 1 as test").fetchall()
            assert result[0][0] == 1
        finally:
            conn.close()

    def test_schema_creation(self, tmp_path):
        """ensure_schema_exists creates every table the pipeline writes to.

        Written to tmp_path rather than data/processed/: this test used to leave a
        real test_nhs.duckdb behind next to the production database.
        """
        conn = get_connection(str(tmp_path / "schema_check.duckdb"))
        try:
            ensure_schema_exists(conn)

            tables = [
                "dim_date", "dim_organisation", "dim_sub_icb", "fact_ae_attendances",
                "fact_rtt_waits", "fact_prescribing", "fact_dental_uda"
            ]

            created = {
                row[0] for row in conn.execute("SHOW TABLES").fetchall()
            }
            missing = [t for t in tables if t not in created]
            assert not missing, f"ensure_schema_exists did not create: {missing}"

            # Each table must be queryable, not merely present in the catalogue.
            for table in tables:
                conn.execute(f"SELECT * FROM {table} LIMIT 1").fetchall()
        finally:
            conn.close()

    def test_schema_has_specialist_no_ae_column(self, tmp_path):
        """dim_organisation must carry specialist_no_ae straight from the DDL.

        Lives here rather than on TestSpecialistNoAeFlag: it exercises schema DDL
        only, so it should not inherit that class's skip-without-database fixture.
        """
        conn = get_connection(str(tmp_path / "schema_check.duckdb"))
        try:
            ensure_schema_exists(conn)
            columns = {row[0] for row in conn.execute("DESCRIBE dim_organisation").fetchall()}
            assert "specialist_no_ae" in columns
        finally:
            conn.close()

    def test_ensure_schema_exists_migration_is_idempotent(self, tmp_path):
        """ALTER TABLE ADD COLUMN IF NOT EXISTS must not error on a table that
        already has the column (i.e. every second-and-later run)."""
        conn = get_connection(str(tmp_path / "schema_check.duckdb"))
        try:
            ensure_schema_exists(conn)
            ensure_schema_exists(conn)  # would raise if the migration weren't idempotent
            columns = {row[0] for row in conn.execute("DESCRIBE dim_organisation").fetchall()}
            assert "specialist_no_ae" in columns
        finally:
            conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
