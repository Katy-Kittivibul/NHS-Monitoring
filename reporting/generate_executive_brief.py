"""Generate docs/executive_findings.md from the DuckDB analytical database.

    python reporting/generate_executive_brief.py [duckdb] [out.md]

Every figure in the brief comes from a query in this file: there are no hand-typed numbers,
and the output carries its as-of month, data window and the git commit it was generated
from. Output is deterministic (no wall-clock time; rows are fully ordered), so regenerating
from unchanged data gives byte-identical text, which is what the snapshot test relies on.

Language rule: the brief reports associations. Bed-block and demand explanations are offered
only as "consistent with", never as causes, and the limitations section is part of every
brief rather than an afterthought.
"""
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb  # noqa: E402
from scipy.stats import spearmanr  # noqa: E402

DEFAULT_DB = "data/processed/nhs.duckdb"
DEFAULT_OUT = _ROOT / "docs" / "executive_findings.md"

# National A&E 4-hour target by financial-year start (sql/00_ae_targets.sql is the reporting-layer
# copy; tests/test_executive_brief.py fails if the two ever disagree).
AE_TARGETS = {2023: 76.0, 2024: 78.0, 2025: 78.0, 2026: 82.0}
CONSTITUTIONAL_STANDARD = 95.0
RTT_STANDARD = 92.0
TOP_N = 10
PARETO_SHARE = 0.80
MIN_ADMISSIONS_SAMPLE = 12_000  # trailing-12-month attendances a trust needs to enter the scatter


def git_sha() -> str:
    """Short commit hash, with "-dirty" when the working tree has uncommitted changes (so a brief
    generated from unpublished code is not passed off as a clean commit)."""
    try:
        run = {"capture_output": True, "text": True, "timeout": 10, "check": True, "cwd": _ROOT}
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], **run).stdout.strip() or "unknown"
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], **run).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _month(value) -> str:
    return value.strftime("%B %Y")


def _target_for(month) -> float:
    fy_start = month.year if month.month >= 4 else month.year - 1
    return AE_TARGETS[fy_start]


def _num(value: float, places: int = 1) -> str:
    return f"{value:,.{places}f}"


def _p_text(p: float) -> str:
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


def _table(headers: list[str], rows: list[list]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines


def collect(conn) -> dict:
    """Every number the brief reports, as plain Python values."""
    d = {}
    d["window"] = {
        name: conn.execute(f"SELECT MIN(month_date), MAX(month_date), COUNT(DISTINCT month_date) FROM {table}"
                           ).fetchone()
        for name, table in (("A&E", "fact_ae_attendances"), ("RTT", "fact_rtt_waits"),
                            ("GP prescribing", "fact_prescribing"), ("NHS dental", "fact_dental_uda"))
    }
    latest = d["window"]["A&E"][1]
    d["latest"] = latest

    # --- A&E against the target ---------------------------------------------------------------
    monthly = conn.execute("""
        SELECT month_date, SUM(total_attendances) AS att, SUM(total_breaches_4hr) AS br,
               SUM(attendances_type1) AS att1, SUM(breaches_4hr_type1) AS br1
        FROM fact_ae_attendances GROUP BY month_date ORDER BY month_date""").fetchall()
    d["ae_monthly"] = monthly
    d["ae_excess_by_month"] = {}
    for month, att, br, *_ in monthly:
        d["ae_excess_by_month"][month] = None  # filled from the trust-level query below
    trust_excess = conn.execute("""
        SELECT month_date, org_code, MAX(org_name) AS org_name, SUM(total_attendances) AS att,
               SUM(total_breaches_4hr) AS br
        FROM fact_ae_attendances GROUP BY month_date, org_code""").fetchall()
    per_month = {}
    latest_rows = []
    for month, code, name, att, br in trust_excess:
        allowed = att * (1 - _target_for(month) / 100.0)
        excess = max(br - allowed, 0.0)
        per_month[month] = per_month.get(month, 0.0) + excess
        if month == latest:
            latest_rows.append((excess, code, name, att, br))
    d["ae_excess_by_month"] = per_month
    d["ae_worst_trusts"] = sorted(latest_rows, key=lambda r: (-r[0], r[1]))[:TOP_N]
    d["ae_latest_trusts_missing_target"] = sum(1 for r in latest_rows if r[0] > 0)
    d["ae_latest_trusts"] = len(latest_rows)

    # --- RTT -----------------------------------------------------------------------------------
    d["rtt"] = conn.execute("""
        SELECT month_date, SUM(waiting_list_size), SUM(over_18wk_count), SUM(over_52wk_count)
        FROM fact_rtt_waits WHERE specialty_code = 'C_999'
        GROUP BY month_date ORDER BY month_date""").fetchall()
    rtt_latest = d["window"]["RTT"][1]
    d["rtt_latest"] = rtt_latest
    pareto = conn.execute("""
        SELECT specialty_name, SUM(over_52wk_count) AS waiters FROM fact_rtt_waits
        WHERE month_date = ? AND specialty_code <> 'C_999' GROUP BY specialty_name
        HAVING SUM(over_52wk_count) > 0 ORDER BY waiters DESC, specialty_name""", [rtt_latest]).fetchall()
    d["pareto_total"] = sum(w for _, w in pareto)
    d["pareto"] = pareto

    # --- Changepoints and correlations -----------------------------------------------------------
    d["robust_changepoints"] = conn.execute("""
        SELECT r.org_code, MAX(f.org_name), r.month_date,
               TRY_CAST(regexp_extract(r.detail, 'mean_before=(-?[0-9.]+)', 1) AS DOUBLE) AS before_mean,
               TRY_CAST(regexp_extract(r.detail, 'mean_after=(-?[0-9.]+)', 1) AS DOUBLE) AS after_mean
        FROM cp_signals r
        JOIN fact_ae_attendances f ON f.org_code = r.org_code AND f.month_date = ?
        WHERE r.dataset = 'ae_attendances' AND r.signal_type = 'changepoint'
          AND r.month_date > ? - INTERVAL 6 MONTH
          AND EXISTS (SELECT 1 FROM cp_signals p
                      WHERE p.dataset = 'ae_attendances_peer' AND p.org_code = r.org_code
                        AND p.signal_type = 'changepoint'
                        AND p.month_date BETWEEN r.month_date - INTERVAL 1 MONTH
                                             AND r.month_date + INTERVAL 1 MONTH)
        GROUP BY r.org_code, r.month_date, r.detail
        ORDER BY r.month_date DESC, r.org_code""", [latest, latest]).fetchall()
    d["raw_changepoints_recent"] = conn.execute("""
        SELECT COUNT(*) FROM cp_signals WHERE dataset = 'ae_attendances' AND signal_type = 'changepoint'
          AND month_date > ? - INTERVAL 6 MONTH""", [latest]).fetchone()[0]

    corr_month = conn.execute(
        "SELECT MAX(month_date) FROM corr_signals WHERE tier = 'tier2_trust'").fetchone()[0]
    d["corr_month"] = corr_month
    if corr_month is not None:
        d["corr_tests"], d["corr_raw_sig"], d["corr_fdr_sig"] = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM((p_value < 0.05)::INT), 0),
                   COALESCE(SUM(is_significant_fdr::INT), 0)
            FROM corr_signals WHERE tier = 'tier2_trust' AND month_date = ?""", [corr_month]).fetchone()
        d["corr_fdr_rows"] = conn.execute("""
            SELECT c.geography_code, MAX(f.org_name), c.correlation_metric, c.ci_low, c.ci_high, c.q_value
            FROM corr_signals c LEFT JOIN fact_ae_attendances f
              ON f.org_code = c.geography_code AND f.month_date = ?
            WHERE c.tier = 'tier2_trust' AND c.month_date = ? AND c.is_significant_fdr
            GROUP BY c.geography_code, c.correlation_metric, c.ci_low, c.ci_high, c.q_value
            ORDER BY c.q_value, c.geography_code""", [latest, corr_month]).fetchall()
        d["tier1_tests"], d["tier1_fdr_sig"] = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(is_significant_fdr::INT), 0) FROM corr_signals
            WHERE tier = 'tier1_region'""").fetchone()

    # --- Admission conversion vs breaches ---------------------------------------------------------
    rows = conn.execute("""
        SELECT org_code, 100.0 * SUM(admissions_via_ae) / SUM(total_attendances) AS conversion,
               100.0 * SUM(total_breaches_4hr) / SUM(total_attendances) AS breach
        FROM fact_ae_attendances
        WHERE month_date > ? - INTERVAL 12 MONTH
        GROUP BY org_code
        HAVING SUM(attendances_type1) > 0 AND SUM(total_attendances) >= ?
        ORDER BY org_code""", [latest, MIN_ADMISSIONS_SAMPLE]).fetchall()
    d["admissions_n"] = len(rows)
    if len(rows) >= 5:
        rho, p = spearmanr([r[1] for r in rows], [r[2] for r in rows])
        d["admissions_rho"], d["admissions_p"] = float(rho), float(p)
    return d


def render(d: dict, sha: str) -> str:
    latest = d["latest"]
    lines = ["# NHS Monitoring Copilot: executive findings", ""]
    window = d["window"]
    lines += [
        f"**As of {_month(latest)}** · generated from commit `{sha}` · "
        "figures are produced by `reporting/generate_executive_brief.py`; do not edit by hand.",
        "",
        "## Data window",
        "",
    ]
    lines += _table(["Dataset", "First month", "Latest month", "Months loaded"], [
        [name, _month(lo), _month(hi), n] for name, (lo, hi, n) in window.items()])
    if window["GP prescribing"][1] < latest or window["NHS dental"][1] < latest:
        lines += ["", "Primary-care data ends before the A&E and RTT data, so any comparison that involves "
                  "prescribing or dental activity can only use the months where both sides have data."]

    # 1 A&E
    att, br = d["ae_monthly"][-1][1], d["ae_monthly"][-1][2]
    perf = 100.0 * (1 - br / att)
    target = _target_for(latest)
    excess_latest = d["ae_excess_by_month"][latest]
    last12 = sorted(d["ae_excess_by_month"])[-12:]
    excess_12 = sum(d["ae_excess_by_month"][m] for m in last12)
    lines += [
        "", "## 1. A&E 4-hour performance against the target", "",
        f"- England-wide, {_num(perf)}% of A&E attendances in {_month(latest)} were seen within 4 hours, "
        f"against a national target of {_num(target, 0)}% and the constitutional standard of "
        f"{_num(CONSTITUTIONAL_STANDARD, 0)}%.",
        f"- That is about {_num(excess_latest, 0)} breaches above what the target allows in the month "
        f"({d['ae_latest_trusts_missing_target']} of {d['ae_latest_trusts']} reporting providers were "
        f"over their allowance), and {_num(excess_12, 0)} over the last {len(last12)} months.",
        "", f"Providers with the most breaches above target allowance in {_month(latest)}:", "",
    ]
    lines += _table(["Provider", "Attendances", "Breaches", "Above allowance"], [
        [f"{name} ({code})", _num(a, 0), _num(b, 0), _num(excess, 0)]
        for excess, code, name, a, b in d["ae_worst_trusts"]])

    # 2 RTT Pareto
    rtt_latest = d["rtt_latest"]
    _, lst, over18, over52 = [r for r in d["rtt"] if r[0] == rtt_latest][0]
    within18 = 100.0 * (1 - over18 / lst)
    lines += [
        "", "## 2. Elective waits: where the 52+ week waiters are", "",
        f"- In {_month(rtt_latest)}, {_num(within18)}% of the {_num(lst, 0)} patients on a waiting list had "
        f"waited under 18 weeks (standard {_num(RTT_STANDARD, 0)}%); {_num(over52, 0)} had waited 52 weeks "
        "or more.",
    ]
    cumulative, shown = 0.0, []
    for name, waiters in d["pareto"]:
        cumulative += waiters
        shown.append([name, _num(waiters, 0), _num(100.0 * waiters / d["pareto_total"]),
                      _num(100.0 * cumulative / d["pareto_total"])])
        if cumulative / d["pareto_total"] >= PARETO_SHARE or len(shown) >= TOP_N:
            break
    lines += [f"- {len(shown)} treatment functions account for "
              f"{_num(100.0 * cumulative / d['pareto_total'])}% of specialty-level 52+ week waits.", ""]
    lines += _table(["Treatment function", "52+ week waiters", "Share %", "Cumulative %"], shown)

    # 3 Shifts
    lines += ["", "## 3. Signals worth a conversation", ""]
    robust = d["robust_changepoints"]
    lines += [
        f"- **Shifts in a trust's own A&E performance** (last 6 months): {d['raw_changepoints_recent']} "
        f"changepoints on the trust's own series, of which {len(robust)} are also seen once the national "
        "pattern is removed (trust minus national). Those are less likely to be a shared seasonal or "
        "national movement.",
    ]
    if robust:
        lines += [""]
        lines += _table(["Trust", "Month", "Breach % before", "Breach % after"], [
            [f"{name} ({code})", _month(month), _num(before, 1) if before is not None else "n/a",
             _num(after, 1) if after is not None else "n/a"]
            for code, name, month, before, after in robust[:TOP_N]])
    if d["corr_month"] is not None:
        lines += [
            "",
            f"- **A&E and RTT moving together:** {d['corr_tests']} trusts were tested for a link between "
            f"month-on-month changes in A&E breaches and in the RTT 18-week share (window ending "
            f"{_month(d['corr_month'])}). {d['corr_raw_sig']} pass an uncorrected p < 0.05, but with that "
            f"many tests about {_num(0.05 * d['corr_tests'], 0)} would pass by chance alone. "
            f"After false-discovery-rate control (Benjamini-Hochberg, q <= 0.05) {d['corr_fdr_sig']} remain.",
        ]
        if d["corr_fdr_rows"]:
            lines += [""]
            lines += _table(["Trust", "r", "95% interval", "q"], [
                [f"{name or code} ({code})", _num(r, 2), f"{_num(lo, 2)} to {_num(hi, 2)}", _num(q, 3)]
                for code, name, r, lo, hi, q in d["corr_fdr_rows"][:TOP_N]])
        lines += [
            "",
            f"- **Regions:** {d['tier1_fdr_sig']} of {d['tier1_tests']} region-level comparisons of "
            "A&E/RTT with prescribing and dental activity survive false-discovery-rate control, so no "
            "region-level link between acute pressure and primary-care activity is supported by "
            "this much data.",
        ]

    # 4 Admissions
    lines += ["", "## 4. Admission conversion and breaches", ""]
    if "admissions_rho" in d:
        rho, p = d["admissions_rho"], d["admissions_p"]
        sentence = (
            f"Across {d['admissions_n']} providers with Type 1 A&E over the last 12 months, the share of "
            f"attendances admitted has a Spearman rank correlation of {_num(rho, 2)} ({_p_text(p)}) with "
            "the share of attendances breaching 4 hours. "
        )
        if p < 0.05 and rho > 0:
            sentence += ("Providers that admit a larger share of their attendees tend to breach more. This "
                         "is consistent with inpatient bed availability and case mix shaping A&E waits, but "
                         "the data cannot separate those from other explanations.")
        elif p < 0.05:
            sentence += ("Providers that admit a larger share of their attendees tend to breach less; the "
                         "data cannot say why.")
        else:
            sentence += "There is no clear association at this sample size."
        lines += [sentence]
    else:
        lines += ["Too few providers with Type 1 A&E to report an association."]

    lines += [
        "", "## Limitations", "",
        "- **Short series.** Monthly series are short (see the data window), so changepoint detection has "
        "limited power and correlation windows contain 6 paired changes; confidence intervals are wide.",
        "- **Association, not cause.** Everything above describes co-movement. None of it shows that one "
        "service's pressure causes another's.",
        "- **Ecological data.** Figures are provider or ICB aggregates; they say nothing about individual "
        "patients.",
        "- **Boundary changes.** Provider mergers, ICB reorganisations and changes to reporting rules can "
        "look like real shifts; check the data-quality flags before acting on a signal.",
        "- **Multiple testing.** Thousands of comparisons are run; only results that survive false-discovery-"
        "rate control are described as supported.",
        "",
    ]
    return "\n".join(lines)


def generate(db_path: str = DEFAULT_DB, out_path: Path | str = DEFAULT_OUT, sha: str | None = None) -> str:
    conn = duckdb.connect(db_path, read_only=True)
    try:
        text = render(collect(conn), sha if sha is not None else git_sha())
    finally:
        conn.close()
    if out_path is not None:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(text, encoding="utf-8", newline="\n")
    return text


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    generate(db, out)
    print(f"[OK] Wrote {out}")
