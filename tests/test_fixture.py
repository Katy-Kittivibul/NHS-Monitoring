"""The synthetic CI fixture must stay valid: it is the only data the CI Postgres job sees."""
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests" / "fixtures"))

from build_fixture_db import MONTHS, build_fixture_db  # noqa: E402

from reporting import load_postgres as lp  # noqa: E402


@pytest.fixture(scope="module")
def fixture_db(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("fx") / "fixture.duckdb")
    build_fixture_db(path)
    conn = duckdb.connect(path, read_only=True)
    yield conn
    conn.close()


def test_every_table_the_loader_copies_can_be_selected(fixture_db):
    for spec in lp.TABLES:
        rows = fixture_db.execute(spec["source"]).fetchall()
        assert rows, f"fixture has no rows for {spec['name']}"
        assert len(fixture_db.description) == len(spec["columns"]), spec["name"]


def test_build_is_deterministic(tmp_path):
    first, second = str(tmp_path / "a.duckdb"), str(tmp_path / "b.duckdb")
    build_fixture_db(first)
    build_fixture_db(second)
    queries = ("SELECT * FROM fact_ae_attendances ORDER BY ALL", "SELECT * FROM fact_rtt_waits ORDER BY ALL")
    for query in queries:
        frames = []
        for path in (first, second):
            conn = duckdb.connect(path, read_only=True)
            frames.append(conn.execute(query).df())
            conn.close()
        pd.testing.assert_frame_equal(*frames)


def test_enough_months_for_the_xmr_view(fixture_db):
    months = fixture_db.execute("SELECT COUNT(DISTINCT month_date) FROM fact_ae_attendances").fetchone()[0]
    assert months == len(MONTHS) >= 16  # vw_spc_xmr needs 15+ points per trust and measure


def test_rtt_c999_equals_sum_of_specialties(fixture_db):
    bad = fixture_db.execute("""
        SELECT COUNT(*) FROM (
            SELECT month_date, org_code,
                   SUM(waiting_list_size) FILTER (WHERE specialty_code <> 'C_999') AS parts,
                   SUM(waiting_list_size) FILTER (WHERE specialty_code = 'C_999') AS total
            FROM fact_rtt_waits WHERE org_code <> 'NXA01' GROUP BY 1, 2
        ) WHERE parts <> total""").fetchone()[0]
    assert bad == 0


def test_covers_the_edge_cases_the_views_branch_on(fixture_db):
    ae = fixture_db.execute("""
        SELECT org_code, SUM(attendances_type1) AS t1, SUM(total_attendances) AS total
        FROM fact_ae_attendances GROUP BY 1""").df()
    assert (ae["t1"] == 0).sum() >= 2  # a Type-1-less trust and the zero-attendance specialist
    assert (ae["total"] == 0).sum() == 1
    assert fixture_db.execute(
        "SELECT COUNT(*) FROM dim_organisation WHERE specialist_no_ae").fetchone()[0] == 1
    # an independent-sector provider that reports RTT only, and is not an 'R' code
    assert fixture_db.execute(
        "SELECT COUNT(*) FROM fact_rtt_waits WHERE org_code = 'NXA01'").fetchone()[0] > 0
    assert fixture_db.execute(
        "SELECT COUNT(*) FROM fact_ae_attendances WHERE org_code = 'NXA01'").fetchone()[0] == 0


def test_trust_codes_resolve_in_the_reference_files(fixture_db):
    reference = pd.read_csv(_ROOT / "corr" / "reference" / "trust_icb_current.csv")
    codes = {r[0] for r in fixture_db.execute(
        "SELECT DISTINCT org_code FROM fact_ae_attendances").fetchall()}
    known = set(reference.loc[reference["n_icbs"] == 1, "trust_code"])
    assert codes <= known
