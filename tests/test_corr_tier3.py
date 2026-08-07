"""Tests for Stage 3 Tier 3 — signal-driven ICB resolution."""
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from corr.tier3_deltas import to_mom_delta
from corr.tier3_signal_driven import (
    ensure_tier3_schema_exists,
    write_tier3_signals,
    resolve_trust_icb,
    get_question_b_eligible_trusts,
    run_question_a,
    run_question_b,
    run_tier3,
    CORRELATION_THRESHOLD,
)
from etl.common import get_connection

REAL_DB_PATH = "data/processed/nhs.duckdb"
TRUST_TO_ICB_PATH = "corr/reference/trust_to_icb.csv"


def _dates(n, start="2024-01-01"):
    return list(pd.date_range(start, periods=n, freq="MS"))


# --- to_mom_delta ------------------------------------------------------------

class TestToMomDelta:
    def test_first_month_is_dropped(self):
        dates = _dates(3)
        series = pd.DataFrame({"key": ["X"] * 3, "month_date": dates, "value": [100.0, 110.0, 121.0]})
        result = to_mom_delta(series)
        assert len(result) == 2
        assert result["month_date"].tolist() == dates[1:]

    def test_percentage_change_is_correct(self):
        dates = _dates(2)
        series = pd.DataFrame({"key": ["X"] * 2, "month_date": dates, "value": [100.0, 110.0]})
        result = to_mom_delta(series)
        assert result.iloc[0]["value"] == pytest.approx(10.0)

    def test_negative_change_is_correct(self):
        dates = _dates(2)
        series = pd.DataFrame({"key": ["X"] * 2, "month_date": dates, "value": [200.0, 150.0]})
        result = to_mom_delta(series)
        assert result.iloc[0]["value"] == pytest.approx(-25.0)

    def test_gap_month_has_no_delta(self):
        # month 3 skipped entirely -> month 4 has no immediately-preceding month
        dates = _dates(4)
        series = pd.DataFrame({
            "key": ["X"] * 3,
            "month_date": [dates[0], dates[1], dates[3]],
            "value": [100.0, 110.0, 130.0],
        })
        result = to_mom_delta(series)
        assert dates[3] not in result["month_date"].tolist()

    def test_zero_prior_value_is_skipped(self):
        dates = _dates(2)
        series = pd.DataFrame({"key": ["X"] * 2, "month_date": dates, "value": [0.0, 50.0]})
        assert to_mom_delta(series).empty

    def test_multiple_keys_are_independent(self):
        dates = _dates(2)
        series = pd.DataFrame({
            "key": ["X", "X", "Y", "Y"],
            "month_date": dates + dates,
            "value": [100.0, 120.0, 50.0, 40.0],
        })
        result = to_mom_delta(series)
        by_key = dict(zip(result["key"], result["value"]))
        assert by_key["X"] == pytest.approx(20.0)
        assert by_key["Y"] == pytest.approx(-20.0)


# --- resolve_trust_icb ---------------------------------------------------------

class TestResolveTrustIcb:
    @pytest.fixture
    def mapping(self):
        return pd.DataFrame([
            {"trust_code": "R0A", "icb_code": "QOP", "multi_icb": False, "resolution_status": "resolved"},
            {"trust_code": "RYX", "icb_code": "QMJ", "multi_icb": True, "resolution_status": "multi_icb"},
            {"trust_code": "RYX", "icb_code": "QRV", "multi_icb": True, "resolution_status": "multi_icb"},
            {"trust_code": "RYX", "icb_code": "QWE", "multi_icb": True, "resolution_status": "multi_icb"},
            {"trust_code": "RGHOST", "icb_code": None, "multi_icb": False,
             "resolution_status": "no_icb_relationship_in_window"},
            {"trust_code": "RERR", "icb_code": None, "multi_icb": False,
             "resolution_status": "error: HTTP 500"},
        ])

    def test_resolved_trust_returns_its_icb(self, mapping):
        icb_code, status, detail = resolve_trust_icb(mapping, "R0A")
        assert icb_code == "QOP"
        assert status == "resolved"

    def test_multi_icb_trust_is_flagged_not_silently_picked(self, mapping):
        icb_code, status, detail = resolve_trust_icb(mapping, "RYX")
        assert icb_code == "MULTI_ICB"
        assert status == "multi_icb"
        assert "QMJ" in detail and "QRV" in detail and "QWE" in detail

    def test_no_relationship_in_window_is_unresolved(self, mapping):
        icb_code, status, detail = resolve_trust_icb(mapping, "RGHOST")
        assert icb_code == "UNRESOLVED"
        assert status == "no_icb_relationship_in_window"

    def test_ods_error_is_unresolved_with_error_tag(self, mapping):
        icb_code, status, detail = resolve_trust_icb(mapping, "RERR")
        assert icb_code == "UNRESOLVED"
        assert status == "resolution_error"
        # the raw error text belongs in `detail`, never in the categorical `status`/`finding`
        assert "HTTP 500" in detail

    def test_trust_not_in_mapping_at_all(self, mapping):
        icb_code, status, detail = resolve_trust_icb(mapping, "RNOWHERE")
        assert icb_code == "UNRESOLVED"
        assert status == "not_in_tier3_universe"

    def test_status_never_embeds_instance_data(self, mapping):
        """Regression guard: status feeds directly into the `finding` column,
        which must stay a clean, small, categorical vocabulary."""
        for trust_code in mapping["trust_code"].unique():
            _, status, _ = resolve_trust_icb(mapping, trust_code)
            assert "," not in status
            assert ":" not in status


# --- write_tier3_signals idempotency --------------------------------------------

class TestWriteTier3SignalsIdempotent:
    @pytest.fixture
    def synthetic_conn(self):
        conn = duckdb.connect(":memory:")
        ensure_tier3_schema_exists(conn)
        yield conn
        conn.close()

    def test_rerun_does_not_duplicate(self, synthetic_conn):
        signals = [{"trust_code": "R0A", "icb_code": "QOP", "month_date": pd.Timestamp("2024-09-01").date(),
                    "finding": "prescribing_correlated_shift", "detail": "r=0.7"}]
        write_tier3_signals(synthetic_conn, "corr_signals_tier3a", "R0A", signals)
        write_tier3_signals(synthetic_conn, "corr_signals_tier3a", "R0A", signals)

        count = synthetic_conn.execute("SELECT COUNT(*) FROM corr_signals_tier3a").fetchall()[0][0]
        assert count == 1

    def test_rerun_clears_stale_signals_for_trust(self, synthetic_conn):
        signals = [{"trust_code": "R0A", "icb_code": "QOP", "month_date": pd.Timestamp("2024-09-01").date(),
                    "finding": "prescribing_correlated_shift", "detail": "r=0.7"}]
        write_tier3_signals(synthetic_conn, "corr_signals_tier3a", "R0A", signals)
        write_tier3_signals(synthetic_conn, "corr_signals_tier3a", "R0A", [])

        count = synthetic_conn.execute("SELECT COUNT(*) FROM corr_signals_tier3a").fetchall()[0][0]
        assert count == 0

    def test_different_trusts_do_not_clobber_each_other(self, synthetic_conn):
        s1 = [{"trust_code": "R0A", "icb_code": "QOP", "month_date": pd.Timestamp("2024-09-01").date(),
               "finding": "prescribing_correlated_shift", "detail": "v1"}]
        s2 = [{"trust_code": "RQX", "icb_code": "QMF", "month_date": pd.Timestamp("2024-09-01").date(),
               "finding": "prescribing_correlated_shift", "detail": "v1"}]
        write_tier3_signals(synthetic_conn, "corr_signals_tier3a", "R0A", s1)
        write_tier3_signals(synthetic_conn, "corr_signals_tier3a", "RQX", s2)

        count = synthetic_conn.execute("SELECT COUNT(*) FROM corr_signals_tier3a").fetchall()[0][0]
        assert count == 2

    def test_both_tier3_tables_created(self, synthetic_conn):
        tables = {row[0] for row in synthetic_conn.execute("SHOW TABLES").fetchall()}
        assert {"corr_signals_tier3a", "corr_signals_tier3b"} <= tables


# --- get_question_b_eligible_trusts ---------------------------------------------

class TestQuestionBEligibleTrusts:
    def test_excludes_specialist_and_signal_trusts(self):
        conn = duckdb.connect(":memory:")
        conn.execute("""
            CREATE TABLE dim_organisation (
                org_code VARCHAR, org_name VARCHAR, org_type VARCHAR,
                parent_org_code VARCHAR, region_name VARCHAR, specialist_no_ae BOOLEAN
            )
        """)
        conn.execute("""
            INSERT INTO dim_organisation VALUES
                ('R0A', 'Active trust with a signal', 'trust', NULL, NULL, FALSE),
                ('RFLAT', 'Active trust, no signal', 'trust', NULL, NULL, FALSE),
                ('RAN', 'Specialist, no A&E', 'trust', NULL, NULL, TRUE)
        """)
        conn.execute("CREATE TABLE cp_signals (org_code VARCHAR, month_date DATE, method VARCHAR)")
        conn.execute("INSERT INTO cp_signals VALUES ('R0A', '2024-09-01', 'pelt_changepoint')")

        eligible = get_question_b_eligible_trusts(conn)
        assert eligible == ["RFLAT"]
        conn.close()


# --- Integration tests against the real backfilled DB ---------------------------

class TestRealDataIntegration:
    @pytest.fixture(autouse=True)
    def skip_if_no_db(self):
        if not Path(REAL_DB_PATH).exists():
            pytest.skip(f"{REAL_DB_PATH} not found — run etl/run_all.py, cp/run_all.py first")
        if not Path(TRUST_TO_ICB_PATH).exists():
            pytest.skip(f"{TRUST_TO_ICB_PATH} not found — run corr/reference/trust_to_icb.py first")

    def test_question_a_covers_every_signal_trust(self):
        conn = get_connection(REAL_DB_PATH)
        try:
            signal_trusts = set(conn.execute("SELECT DISTINCT org_code FROM cp_signals").df()["org_code"])
        finally:
            conn.close()

        signals = run_question_a(REAL_DB_PATH)
        assert len(signals) > 0
        covered_trusts = {s["trust_code"] for s in signals}
        assert covered_trusts == signal_trusts

    def test_question_a_correlated_findings_respect_threshold(self):
        run_question_a(REAL_DB_PATH)
        conn = get_connection(REAL_DB_PATH)
        try:
            rows = conn.execute("""
                SELECT finding, detail FROM corr_signals_tier3a
                WHERE finding LIKE '%_correlated_shift' OR finding LIKE '%_no_correlated_shift'
            """).df()
        finally:
            conn.close()
        assert len(rows) > 0
        for _, row in rows.iterrows():
            r = float(row["detail"].split("r=")[1].split(",")[0])
            if row["finding"].endswith("_no_correlated_shift"):
                assert abs(r) < CORRELATION_THRESHOLD
            else:
                assert abs(r) >= CORRELATION_THRESHOLD

    def test_question_a_multi_icb_trust_is_skipped_not_guessed(self):
        run_question_a(REAL_DB_PATH)
        conn = get_connection(REAL_DB_PATH)
        try:
            trust_to_icb_multi = conn.execute("SELECT DISTINCT org_code FROM cp_signals").df()
        finally:
            conn.close()
        # RYX is a known multi-ICB trust (QMJ/QRV/QWE all active in the report window)
        if "RYX" not in set(trust_to_icb_multi["org_code"]):
            pytest.skip("RYX has no cp_signals row in this DB snapshot")

        conn = get_connection(REAL_DB_PATH)
        try:
            rows = conn.execute(
                "SELECT icb_code, finding FROM corr_signals_tier3a WHERE trust_code = 'RYX'"
            ).df()
        finally:
            conn.close()
        assert (rows["icb_code"] == "MULTI_ICB").all()
        assert rows["finding"].str.contains("skipped_multi_icb").all()

    def test_question_b_covers_every_eligible_trust(self):
        conn = get_connection(REAL_DB_PATH)
        try:
            eligible = set(get_question_b_eligible_trusts(conn))
        finally:
            conn.close()

        signals = run_question_b(REAL_DB_PATH)
        assert len(signals) > 0
        covered_trusts = {s["trust_code"] for s in signals}
        assert covered_trusts == eligible

    def test_question_b_and_question_a_trust_sets_are_disjoint(self):
        conn = get_connection(REAL_DB_PATH)
        try:
            signal_trusts = set(conn.execute("SELECT DISTINCT org_code FROM cp_signals").df()["org_code"])
            eligible = set(get_question_b_eligible_trusts(conn))
        finally:
            conn.close()
        assert signal_trusts.isdisjoint(eligible)

    def test_run_tier3_is_idempotent_on_real_db(self):
        run_tier3(REAL_DB_PATH)
        conn = get_connection(REAL_DB_PATH)
        try:
            first = (
                conn.execute("SELECT COUNT(*) FROM corr_signals_tier3a").fetchall()[0][0],
                conn.execute("SELECT COUNT(*) FROM corr_signals_tier3b").fetchall()[0][0],
            )
        finally:
            conn.close()

        run_tier3(REAL_DB_PATH)
        conn = get_connection(REAL_DB_PATH)
        try:
            second = (
                conn.execute("SELECT COUNT(*) FROM corr_signals_tier3a").fetchall()[0][0],
                conn.execute("SELECT COUNT(*) FROM corr_signals_tier3b").fetchall()[0][0],
            )
        finally:
            conn.close()

        assert first == second
        assert first[0] > 0 and first[1] > 0
