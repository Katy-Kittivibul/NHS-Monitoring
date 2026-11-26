"""Hermetic tests for the Phase 1 ETL plumbing: vectorised cleaners, dim_organisation
precedence, the ingestion manifest and pipeline_runs. No data/ directory is needed."""
import json

import numpy as np
import pandas as pd
import pytest

from etl.ae_attendances import clean_ae_value, etl_ae_attendances
from etl.common import (
    ensure_schema_exists,
    file_sha256,
    get_connection,
    ingest_files,
    map_unique,
    normalise_region,
    safe_pct,
    to_float_series,
    to_int_series,
    track_run,
    upsert_organisations,
)
from etl.dental_uda import clean_dental_value
from etl.prescribing import etl_prescribing
from etl.rtt_waits import clean_rtt_value

DIRTY = [None, np.nan, "-", "", " ", "abc", "1,234", "12", " 7 ", "3.7", "-2.5", "1e3",
         0, 5, 3.9, -3.9, 123456789012, "0", "0.0", "*", "N/A"]


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(str(tmp_path / "t.duckdb"))
    ensure_schema_exists(connection)
    yield connection
    connection.close()


class TestVectorisedCleaners:
    def test_int_matches_ae_reference(self):
        expected = [clean_ae_value(v) for v in DIRTY]
        assert to_int_series(DIRTY).tolist() == expected

    def test_int_matches_rtt_reference(self):
        expected = [clean_rtt_value(v) for v in DIRTY]
        assert to_int_series(DIRTY).tolist() == expected

    def test_float_matches_dental_reference(self):
        values = [v for v in DIRTY if not (isinstance(v, str) and v.strip().lower() == "nan")]
        expected = [clean_dental_value(v) for v in values]
        assert to_float_series(values).tolist() == expected

    def test_int_dtype_and_inf_handling(self):
        out = to_int_series([1.0, np.inf, -np.inf, np.nan])
        assert out.dtype == "int64"
        assert out.tolist() == [1, 0, 0, 0]

    def test_cleaners_preserve_index(self):
        series = pd.Series(["1", "-", "3"], index=[10, 20, 30])
        assert to_int_series(series).index.tolist() == [10, 20, 30]

    def test_safe_pct_zero_and_negative_denominator(self):
        out = safe_pct([5, 5, 0, 50], [0, -1, 10, 200])
        assert out.tolist() == [0.0, 0.0, 0.0, 25.0]

    def test_map_unique_parses_each_distinct_value_once(self):
        calls = []

        def parse(value):
            calls.append(value)
            return value.upper()

        out = map_unique(pd.Series(["a", "b", "a", "a", None]), parse)
        assert out.tolist()[:4] == ["A", "B", "A", "A"]
        assert pd.isna(out.iloc[4])
        assert sorted(calls) == ["a", "b"]


class TestNormaliseRegion:
    @pytest.mark.parametrize("raw", ["Y60", " y60 ", "NHS ENGLAND MIDLANDS", "NHS ENGLAND MIDLANDS "])
    def test_both_forms_resolve_to_same_pair(self, raw):
        assert normalise_region(raw) == ("Y60", "NHS ENGLAND MIDLANDS")

    @pytest.mark.parametrize("raw", [None, np.nan, "", "Y99", "somewhere else"])
    def test_unrecognised_is_none(self, raw):
        assert normalise_region(raw) == (None, None)


def _org(code, name, org_type, parent=None):
    return pd.DataFrame({"org_code": [code], "org_name": [name], "org_type": [org_type],
                         "parent_org_code": [parent]})


class TestOrgPrecedence:
    def test_practice_never_downgrades_trust(self, conn):
        upsert_organisations(conn, _org("RXX", "Trust Name", "trust"), ["org_name", "parent_org_code"])
        upsert_organisations(conn, _org("RXX", "Walk-in Centre", "practice", "16C"),
                             ["org_name", "parent_org_code"])
        assert conn.execute(
            "SELECT org_type, org_name, parent_org_code FROM dim_organisation WHERE org_code='RXX'"
        ).fetchone() == ("trust", "Trust Name", None)

    def test_trust_upgrades_practice_regardless_of_load_order(self, conn):
        upsert_organisations(conn, _org("RXX", "Walk-in Centre", "practice", "16C"),
                             ["org_name", "parent_org_code"])
        upsert_organisations(conn, _org("RXX", "Trust Name", "trust"), ["org_name", "parent_org_code"])
        assert conn.execute(
            "SELECT org_type, org_name, parent_org_code FROM dim_organisation WHERE org_code='RXX'"
        ).fetchone() == ("trust", "Trust Name", None)

    def test_same_rank_updates_attributes(self, conn):
        upsert_organisations(conn, _org("RXX", "Old", "trust"), ["org_name", "parent_org_code"])
        upsert_organisations(conn, _org("RXX", "New", "trust"), ["org_name", "parent_org_code"])
        name = conn.execute("SELECT org_name FROM dim_organisation WHERE org_code='RXX'").fetchone()[0]
        assert name == "New"


def _write_numbers(conn, df):
    conn.register("_t", df)
    conn.execute("DELETE FROM t_fact")
    conn.execute("INSERT INTO t_fact SELECT * FROM _t")
    conn.unregister("_t")


class TestIngestionManifest:
    @pytest.fixture
    def setup(self, conn, tmp_path):
        conn.execute("CREATE TABLE t_fact (n INTEGER)")
        raw = tmp_path / "raw"
        raw.mkdir()
        path = raw / "file_202401.csv"
        path.write_text("n\n1\n2\n3\n")
        return conn, path

    @staticmethod
    def _load(path):
        return pd.read_csv(path)

    def _ingest(self, conn, path, **kwargs):
        return ingest_files(conn, dataset="t", paths=[path], load_fn=self._load,
                            write_fn=_write_numbers, **kwargs)

    def test_second_run_skips_unchanged_file(self, setup):
        conn, path = setup
        first = self._ingest(conn, path)
        second = self._ingest(conn, path)
        assert (first.files_loaded, first.files_skipped) == (1, 0)
        assert (second.files_loaded, second.files_skipped) == (0, 1)
        assert conn.execute("SELECT sha256, row_count FROM ingestion_manifest").fetchone() == (
            file_sha256(path), 3)

    def test_changed_file_is_reloaded(self, setup):
        conn, path = setup
        self._ingest(conn, path)
        path.write_text("n\n1\n2\n3\n4\n")
        run = self._ingest(conn, path)
        assert run.files_loaded == 1
        assert conn.execute("SELECT COUNT(*) FROM t_fact").fetchone()[0] == 4
        assert conn.execute("SELECT row_count FROM ingestion_manifest").fetchone()[0] == 4

    def test_force_reloads_unchanged_file(self, setup):
        conn, path = setup
        self._ingest(conn, path)
        run = self._ingest(conn, path, force=True)
        assert (run.files_loaded, run.files_skipped) == (1, 0)

    def test_failed_write_is_not_marked_loaded_and_is_retried(self, setup):
        conn, path = setup

        def boom(c, df):
            _write_numbers(c, df)
            raise RuntimeError("write blew up")

        with pytest.raises(RuntimeError, match="write blew up"):
            ingest_files(conn, dataset="t", paths=[path], load_fn=self._load, write_fn=boom)
        assert conn.execute("SELECT COUNT(*) FROM ingestion_manifest").fetchone()[0] == 0
        status, detail = conn.execute("SELECT status, detail FROM pipeline_runs").fetchone()
        assert status == "failed"
        assert "RuntimeError: write blew up" in detail
        # ...so the next run retries the file rather than skipping it, and repairs the table.
        assert self._ingest(conn, path).files_loaded == 1
        assert conn.execute("SELECT COUNT(*) FROM t_fact").fetchone()[0] == 3

    def test_crashed_forced_reload_is_retried_not_skipped(self, setup):
        """--force on an already-loaded file that crashes mid-write must not leave a stale
        'current' manifest row behind, or the next plain run would skip the damaged month."""
        conn, path = setup
        self._ingest(conn, path)

        def boom(c, df):
            c.execute("DELETE FROM t_fact")
            raise RuntimeError("crash after delete")

        with pytest.raises(RuntimeError):
            ingest_files(conn, dataset="t", paths=[path], load_fn=self._load, write_fn=boom, force=True)
        assert conn.execute("SELECT COUNT(*) FROM t_fact").fetchone()[0] == 0
        assert self._ingest(conn, path).files_loaded == 1
        assert conn.execute("SELECT COUNT(*) FROM t_fact").fetchone()[0] == 3

    def test_no_files_is_logged_as_skipped(self, conn):
        run = ingest_files(conn, dataset="t", paths=[], load_fn=None, write_fn=None)
        assert run.status == "skipped"
        assert conn.execute("SELECT status FROM pipeline_runs").fetchone()[0] == "skipped"


class TestPipelineRuns:
    def test_success_row(self, conn):
        with track_run(conn, "etl", "demo") as run:
            run.rows_processed = 42
            run.files_loaded = 2
        status, rows, started, finished, detail = conn.execute(
            "SELECT status, rows_processed, started_at, finished_at, detail FROM pipeline_runs"
        ).fetchone()
        assert (status, rows) == ("success", 42)
        assert finished >= started
        assert json.loads(detail) == {"files_loaded": 2, "files_skipped": 0}

    def test_failure_row_and_exception_propagates(self, conn):
        with pytest.raises(ValueError, match="bad input"):
            with track_run(conn, "etl", "demo"):
                raise ValueError("bad input")
        status, detail = conn.execute("SELECT status, detail FROM pipeline_runs").fetchone()
        assert status == "failed"
        assert detail == "ValueError: bad input"


AE_COLS = ("Period,Org Code,Org name,Parent Org,A&E attendances Type 1,A&E attendances Type 2,"
           "A&E attendances Other A&E Department,Attendances over 4hrs Type 1,"
           "Attendances over 4hrs Type 2,Attendances over 4hrs Other Department,"
           "Emergency admissions via A&E - Type 1\n")


class TestEndToEndOrgFix:
    """A&E walk-in centre also present as a GP practice: it must stay a trust, whichever
    dataset loads last, and RTT/A&E region labels must agree."""

    def _seed(self, tmp_path, monkeypatch):
        raw = tmp_path / "data" / "raw"
        raw.mkdir(parents=True)
        (raw / "ae_attendances_202401.csv").write_text(
            AE_COLS
            + "MSitAE-JANUARY-2024,RXX,WALK-IN TRUST,NHS ENGLAND MIDLANDS ,100,0,0,10,0,0,5\n"
            + "MSitAE-JANUARY-2024,RYY,OTHER TRUST,NHS ENGLAND LONDON,200,0,0,40,0,0,9\n"
        )
        (raw / "openprescribing_spending_202401.json").write_text(json.dumps([
            {"row_id": "RXX", "row_name": "WALK-IN PRACTICE", "ccg": "16C", "items": 5,
             "quantity": 10, "actual_cost": 1.5, "setting": 4, "bnf_section_code": "0205",
             "date": "2024-01-01"},
        ]))
        monkeypatch.chdir(tmp_path)
        return str(tmp_path / "db.duckdb")

    def test_walk_in_centre_stays_trust_after_prescribing_load(self, tmp_path, monkeypatch):
        db = self._seed(tmp_path, monkeypatch)
        etl_ae_attendances(db)
        etl_prescribing(db)
        conn = get_connection(db)
        try:
            assert conn.execute(
                "SELECT org_type, org_name, region_code, region_name FROM dim_organisation "
                "WHERE org_code='RXX'"
            ).fetchone() == ("trust", "WALK-IN TRUST", "Y60", "NHS ENGLAND MIDLANDS")
            # The practice-grain fact is still loaded in full.
            assert conn.execute("SELECT COUNT(*) FROM fact_prescribing").fetchone()[0] == 1
        finally:
            conn.close()

    def test_etl_is_incremental_and_force_reloads(self, tmp_path, monkeypatch):
        db = self._seed(tmp_path, monkeypatch)
        etl_ae_attendances(db)
        etl_ae_attendances(db)
        etl_ae_attendances(db, force=True)
        conn = get_connection(db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM fact_ae_attendances").fetchone()[0] == 2
            runs = conn.execute(
                "SELECT detail FROM pipeline_runs WHERE dataset='ae_attendances' ORDER BY started_at"
            ).fetchall()
            assert [json.loads(d)["files_loaded"] for (d,) in runs] == [1, 0, 1]
            assert [json.loads(d)["files_skipped"] for (d,) in runs] == [0, 1, 0]
        finally:
            conn.close()

    def test_run_all_reports_changes_and_warns_downstream_is_stale(self, tmp_path, monkeypatch, capsys):
        from etl.run_all import run_all_etl

        db = self._seed(tmp_path, monkeypatch)
        assert run_all_etl(db) == 2  # the A&E and prescribing files; RTT/dental have none
        first = capsys.readouterr().out
        assert "2 file(s) loaded, 0 unchanged" in first
        assert "re-run the downstream stages" in first

        assert run_all_etl(db) == 0
        second = capsys.readouterr().out
        assert "0 file(s) loaded, 2 unchanged" in second
        assert "re-run the downstream stages" not in second
