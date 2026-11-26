"""The generated executive brief: deterministic, sourced from queries, and honest in its wording."""
import os
import re
import sys
from pathlib import Path

import duckdb
import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests" / "fixtures"))

from build_fixture_db import build_fixture_db  # noqa: E402

from reporting import generate_executive_brief as brief  # noqa: E402

SNAPSHOT = _ROOT / "tests" / "fixtures" / "executive_brief_snapshot.md"
SHA = "fixture"


@pytest.fixture(scope="module")
def fixture_path(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("brief") / "fixture.duckdb")
    build_fixture_db(path)
    return path


@pytest.fixture(scope="module")
def text(fixture_path):
    return brief.generate(fixture_path, out_path=None, sha=SHA)


def test_regenerates_byte_identically(fixture_path, tmp_path):
    first, second = tmp_path / "a.md", tmp_path / "b.md"
    brief.generate(fixture_path, first, sha=SHA)
    brief.generate(fixture_path, second, sha=SHA)
    assert first.read_bytes() == second.read_bytes()


def test_matches_the_committed_snapshot(text):
    """Any change to the brief shows up as a reviewable diff. Refresh deliberately with
    UPDATE_SNAPSHOT=1 pytest tests/test_executive_brief.py."""
    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        SNAPSHOT.write_text(text, encoding="utf-8", newline="\n")
    assert SNAPSHOT.exists(), "snapshot missing: run with UPDATE_SNAPSHOT=1"
    assert text == SNAPSHOT.read_text(encoding="utf-8")


def test_carries_as_of_window_and_commit(text):
    assert "**As of August 2025**" in text
    assert f"commit `{SHA}`" in text
    assert "## Data window" in text and "March 2024" in text


def test_headline_numbers_come_from_the_database(fixture_path, text):
    conn = duckdb.connect(fixture_path, read_only=True)
    att, breaches = conn.execute(
        "SELECT SUM(total_attendances), SUM(total_breaches_4hr) FROM fact_ae_attendances "
        "WHERE month_date = '2025-08-01'").fetchone()
    waiting, over52 = conn.execute(
        "SELECT SUM(waiting_list_size), SUM(over_52wk_count) FROM fact_rtt_waits "
        "WHERE month_date = '2025-08-01' AND specialty_code = 'C_999'").fetchone()
    conn.close()
    assert f"{100.0 * (1 - breaches / att):.1f}% of A&E attendances" in text
    assert f"{waiting:,.0f} patients on a waiting list" in text
    assert f"{over52:,.0f} had waited 52 weeks" in text


def test_pareto_shares_are_consistent(text):
    section = text.split("## 2.")[1].split("## 3.")[0]
    cumulative = [float(m) for m in re.findall(r"\| ([0-9.]+) \|\n", section)]
    assert cumulative == sorted(cumulative) and cumulative[-1] <= 100.0


def test_fdr_count_cannot_exceed_the_raw_count(text):
    m = re.search(r"(\d+) pass an uncorrected p < 0.05.*?(\d+) remain", text, re.S)
    assert m and int(m.group(2)) <= int(m.group(1))  # BH can only remove discoveries


def test_language_is_about_association_not_cause(text):
    body = text.split("## Limitations")[0].lower()  # the limitations section may say "causes" to deny it
    lowered = text.lower()
    for banned in ("causes", "caused", "because of", "due to", "led to", "leads to", "drives", "results in"):
        assert banned not in body, banned
    assert "association, not cause" in lowered
    assert any(phrase in lowered for phrase in
               ("consistent with", "no clear association", "tend to breach less"))
    assert "## Limitations" in text


def test_targets_match_the_reporting_layer_view():
    """The brief's target table and sql/00_ae_targets.sql must say the same thing."""
    sql = (_ROOT / "sql" / "00_ae_targets.sql").read_text(encoding="utf-8")
    rows = re.findall(r"\('(\d{4})/\d{2}', DATE '[0-9-]+', DATE '[0-9-]+', ([0-9.]+)\)", sql)
    assert {int(y): float(t) for y, t in rows} == brief.AE_TARGETS


def test_missing_inference_columns_fail_loudly(tmp_path):
    path = str(tmp_path / "old.duckdb")
    build_fixture_db(path)
    conn = duckdb.connect(path)
    conn.execute("ALTER TABLE corr_signals DROP COLUMN is_significant_fdr")
    conn.close()
    with pytest.raises(duckdb.Error):
        brief.generate(path, out_path=None, sha=SHA)
