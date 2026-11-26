"""Hermetic tests for reporting/load_postgres.py (no PostgreSQL server needed)."""
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from reporting import load_postgres as lp


def test_env_loading_and_missing_settings(tmp_path, monkeypatch):
    for k in lp.REQUIRED_ENV:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="PGHOST"):
        lp.load_env(tmp_path / "absent.env")

    env = tmp_path / ".env"
    env.write_text("\n".join(f"{k}=v_{k}" for k in lp.REQUIRED_ENV))
    assert lp.load_env(env)["PGDATABASE"] == "v_PGDATABASE"


def test_row_count_mismatch_fails_the_refresh(capsys):
    lp.verify_counts([("a", 3, 3)])
    with pytest.raises(RuntimeError, match="b"):
        lp.verify_counts([("a", 3, 3), ("b", 5, 4)])


def test_specs_are_consistent_with_their_sources(tmp_path):
    """Column lists match the SELECTs, DDL has keys and the (org_code, month_date) indexes."""
    con = duckdb.connect(":memory:")
    for spec in lp.CSV_TABLES:
        n = lp._export_csv(con, f"SELECT * FROM read_csv('{spec['csv'].as_posix()}', all_varchar=true)",
                           tmp_path / f"{spec['name']}.csv")
        header_cols = [r[0] for r in con.execute(
            f"DESCRIBE SELECT * FROM read_csv('{spec['csv'].as_posix()}', all_varchar=true)").fetchall()]
        assert header_cols == [c for c, _ in spec["columns"]]
        assert n > 0
    # Unresolved trusts have no icb_code: exported as \N so PostgreSQL stores NULL.
    assert "\\N" in (tmp_path / "trust_to_icb.csv").read_text(encoding="utf-8")

    for spec in lp.TABLES:
        ddl = lp.create_table_sql(spec)
        assert "PRIMARY KEY" in ddl
        has_org_month = "org_code" in spec["source"] and "month_date" in spec["source"]
        if has_org_month and spec["name"] != "dim_organisation":
            assert any(cols == "org_code, month_date" for cols, _ in spec["indexes"]), spec["name"]
        assert len(spec["columns"]) == len(spec["source"].split(" FROM ")[0].split("SELECT ")[1].split(", "))


def test_every_published_table_has_a_unique_name_and_a_key():
    names = [spec["name"] for spec in lp.TABLES + lp.CSV_TABLES]
    assert len(names) == len(set(names))
    for spec in lp.TABLES + lp.CSV_TABLES:
        assert "PRIMARY KEY" in lp.create_table_sql(spec), spec["name"]


def test_optional_tables_load_empty_when_the_stage_has_not_run():
    """Tier 3 may not have been run yet: the refresh must not fail on its missing tables."""
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE present (x INTEGER)")
    assert lp._source_has_table(con, "present")
    assert not lp._source_has_table(con, "corr_signals_tier3a")
    optional = {spec["name"] for spec in lp.TABLES if spec.get("optional")}
    assert {"corr_signals", "corr_signals_tier3a", "corr_signals_tier3b", "pipeline_runs"} <= optional
    # the core facts are required: a missing one is a real error, not an empty table
    assert not {"fact_ae_attendances", "fact_rtt_waits", "fact_prescribing", "fact_dental_uda"} & optional
