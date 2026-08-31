"""Stage 3 Tier 3 — signal-driven ICB resolution.

Tier 1/2 correlate at a fixed geography (region or shared trust codes) for
every trust, regardless of whether anything interesting happened. Tier 3
only resolves a trust to its ICB when Stage 2 (cp/) has already flagged
something — it asks two different questions of two different trust
populations, using corr/reference/trust_to_icb.py's time-scoped, ODS-sourced
resolution (which flags rather than guesses at multi-ICB trusts like RYX):

- Question A: for a trust with a cp_signals changepoint/SPC signal, did its
  ICB's prescribing/dental MoM-delta correlate with the trust's own breach_pct
  MoM-delta in the same rolling window? Reuses corr/correlation.py's
  rolling_correlation() unchanged, fed MoM-delta series instead of raw levels.
- Question B: for a trust with *no* cp_signals row and real A&E activity
  (specialist_no_ae = FALSE — excludes trusts that structurally never run a
  general A&E department), did its ICB's prescribing/dental show a changepoint
  anyway, despite the trust's own A&E series looking flat? Reuses
  cp/changepoints.py's detect_changepoints() unchanged, applied to the ICB's
  series instead of a trust's breach_pct.

Neither question touches etl/, dq/, cp/, or corr's Tier 1/2 modules — both
only import their detection/correlation logic and read the already-built
fact/signal tables.

Two open judgement calls, not specified by the brief and not confirmed by
the user when asked, so documented here for visibility:
- Question A's "correlated" threshold is |r| >= CORRELATION_THRESHOLD (0.5).
- Question B's "drift" detector is cp/changepoints.py's PELT/l2+BIC method,
  not a dq/-style single-month jump check.
Both are easy to change — see the constants below and detect_changepoints's
own import.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd

from corr.correlation import WINDOW_MONTHS, rolling_correlation
from corr.reference.trust_to_icb import load_trust_to_icb
from corr.rollups import get_ae_breach_pct_by_trust
from corr.tier3_deltas import to_mom_delta
from corr.tier3_rollups import get_dental_pct_achieved_by_icb, get_prescribing_items_by_icb
from cp.changepoints import MIN_POINTS as CP_MIN_POINTS
from cp.changepoints import detect_changepoints
from etl.common import get_connection

CORRELATION_THRESHOLD = 0.5  # |r| at/above this counts as "correlated" for Question A
NO_DATA_ANCHOR = pd.Timestamp("2025-03-01")  # fallback month_date when an ICB has no rows at all


def ensure_tier3_schema_exists(conn):
    """Create corr_signals_tier3a / corr_signals_tier3b if they don't already exist."""
    for table in ("corr_signals_tier3a", "corr_signals_tier3b"):
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                trust_code VARCHAR,
                icb_code VARCHAR,
                month_date DATE,
                finding VARCHAR,
                detail VARCHAR,
                PRIMARY KEY (trust_code, icb_code, month_date, finding)
            )
        """)


def write_tier3_signals(conn, table: str, trust_code: str, signals: list[dict]):
    """Idempotently write one trust's signals to `table` — DELETE then INSERT,
    scoped to trust_code, since Question A/B process one trust at a time."""
    conn.execute(f"DELETE FROM {table} WHERE trust_code = ?", [trust_code])
    if not signals:
        return
    df = pd.DataFrame(signals)  # noqa: F841 — referenced by name in SQL below
    conn.execute(f"INSERT INTO {table} SELECT * FROM df")


def resolve_trust_icb(trust_to_icb: pd.DataFrame, trust_code: str) -> tuple[str, str, str]:
    """Return (icb_code, status, status_detail).

    icb_code is a real code only when status == 'resolved'; otherwise it's a
    sentinel ('UNRESOLVED' / 'MULTI_ICB') so the output tables' composite key
    never needs a NULL icb_code. status is always one of a small fixed set of
    tags (used in the `finding` column, so it must never embed per-trust data
    like specific ICB codes) — status_detail carries whatever instance-
    specific information there is, for the `detail` column instead."""
    rows = trust_to_icb[trust_to_icb["trust_code"] == trust_code]
    if rows.empty:
        return "UNRESOLVED", "not_in_tier3_universe", "trust not in the Tier 3 resolution universe"

    raw_status = rows.iloc[0]["resolution_status"]
    if raw_status == "resolved":
        return rows.iloc[0]["icb_code"], "resolved", ""
    if raw_status == "multi_icb":
        icbs = sorted(rows["icb_code"].tolist())
        return "MULTI_ICB", "multi_icb", f"candidate ICBs: {', '.join(icbs)}"
    if raw_status == "no_icb_relationship_in_window":
        return "UNRESOLVED", "no_icb_relationship_in_window", \
            "no RE8 ICB-partner relationship overlapping Mar2024-Mar2025"
    return "UNRESOLVED", "resolution_error", f"ODS lookup failed: {raw_status}"


# --- Question A ---------------------------------------------------------------

def run_question_a(db_path: str = "data/processed/nhs.duckdb") -> list[dict]:
    """For every trust with a cp_signals row, check whether its ICB's
    prescribing/dental MoM-delta correlated with the trust's own breach_pct
    MoM-delta in the window containing the signal month."""
    conn = get_connection(db_path)
    try:
        ensure_tier3_schema_exists(conn)
        trust_to_icb = load_trust_to_icb()

        signal_months = conn.execute(
            "SELECT DISTINCT org_code AS trust_code, month_date FROM cp_signals"
        ).df()

        trust_breach_delta = to_mom_delta(get_ae_breach_pct_by_trust(conn))
        icb_deltas = {
            "prescribing": to_mom_delta(get_prescribing_items_by_icb(conn)),
            "dental": to_mom_delta(get_dental_pct_achieved_by_icb(conn)),
        }

        all_signals = []
        for trust_code, group in signal_months.groupby("trust_code"):
            icb_code, status, status_detail = resolve_trust_icb(trust_to_icb, trust_code)
            signals = []

            if status != "resolved":
                for metric in ("prescribing", "dental"):
                    signals.append({
                        "trust_code": trust_code, "icb_code": icb_code,
                        "month_date": group["month_date"].max(),
                        "finding": f"{metric}_skipped_{status}",
                        "detail": status_detail,
                    })
            else:
                trust_delta = trust_breach_delta[trust_breach_delta["key"] == trust_code]

                for metric, delta_series in icb_deltas.items():
                    icb_delta = delta_series[delta_series["key"] == icb_code].assign(key=trust_code)
                    corr_by_month = {r["month_date"]: r for r in rolling_correlation(trust_delta, icb_delta)}

                    for signal_month in group["month_date"]:
                        result = corr_by_month.get(signal_month)
                        if result is None:
                            signals.append({
                                "trust_code": trust_code, "icb_code": icb_code, "month_date": signal_month,
                                "finding": f"{metric}_insufficient_window",
                                "detail": (
                                    f"no full {WINDOW_MONTHS}-month paired MoM-delta window "
                                    f"ending {signal_month.date()}"
                                ),
                            })
                            continue

                        r = result["correlation_metric"]
                        finding = f"{metric}_correlated_shift" if abs(r) >= CORRELATION_THRESHOLD \
                            else f"{metric}_no_correlated_shift"
                        signals.append({
                            "trust_code": trust_code, "icb_code": icb_code, "month_date": signal_month,
                            "finding": finding, "detail": result["detail"],
                        })

            write_tier3_signals(conn, "corr_signals_tier3a", trust_code, signals)
            all_signals.extend(signals)

        print(
            f"[OK] Tier 3 Question A: {len(all_signals)} signals "
            f"across {signal_months['trust_code'].nunique()} trusts"
        )
        return all_signals
    finally:
        conn.close()


# --- Question B ---------------------------------------------------------------

def get_question_b_eligible_trusts(conn) -> list[str]:
    """fact_ae_attendances trusts with real activity (specialist_no_ae = FALSE)
    and zero cp_signals rows — Tier 2's "looked flat" population."""
    return conn.execute("""
        SELECT org_code FROM dim_organisation
        WHERE org_type = 'trust' AND specialist_no_ae = FALSE
          AND org_code NOT IN (SELECT DISTINCT org_code FROM cp_signals)
        ORDER BY org_code
    """).df()["org_code"].tolist()


def run_question_b(db_path: str = "data/processed/nhs.duckdb") -> list[dict]:
    """For every eligible no-signal trust, check whether its ICB's
    prescribing/dental series has an l2+BIC changepoint anyway."""
    conn = get_connection(db_path)
    try:
        ensure_tier3_schema_exists(conn)
        trust_to_icb = load_trust_to_icb()
        eligible_trusts = get_question_b_eligible_trusts(conn)

        icb_series = {
            "prescribing": get_prescribing_items_by_icb(conn),
            "dental": get_dental_pct_achieved_by_icb(conn),
        }

        all_signals = []
        for trust_code in eligible_trusts:
            icb_code, status, status_detail = resolve_trust_icb(trust_to_icb, trust_code)
            signals = []

            if status != "resolved":
                anchor = conn.execute(
                    "SELECT MAX(month_date) FROM fact_ae_attendances WHERE org_code = ?", [trust_code]
                ).fetchall()[0][0] or NO_DATA_ANCHOR
                for metric in ("prescribing", "dental"):
                    signals.append({
                        "trust_code": trust_code, "icb_code": icb_code, "month_date": anchor,
                        "finding": f"{metric}_skipped_{status}",
                        "detail": status_detail,
                    })
            else:
                for metric, series in icb_series.items():
                    icb_values_df = series[series["key"] == icb_code].sort_values("month_date")

                    if len(icb_values_df) < CP_MIN_POINTS:
                        anchor = icb_values_df["month_date"].max() if len(icb_values_df) else NO_DATA_ANCHOR
                        signals.append({
                            "trust_code": trust_code, "icb_code": icb_code, "month_date": anchor,
                            "finding": f"{metric}_insufficient_data",
                            "detail": (
                                f"ICB {icb_code} has {len(icb_values_df)} months, "
                                f"need >= {CP_MIN_POINTS}"
                            ),
                        })
                        continue

                    dates = icb_values_df["month_date"].tolist()
                    values = icb_values_df["value"].to_numpy(dtype=float)
                    breakpoints = detect_changepoints(values)

                    if breakpoints:
                        for idx in breakpoints:
                            before_mean = float(values[:idx].mean())
                            after_mean = float(values[idx:].mean())
                            signals.append({
                                "trust_code": trust_code, "icb_code": icb_code, "month_date": dates[idx],
                                "finding": f"{metric}_changepoint_detected",
                                "detail": f"mean_before={before_mean:.2f}, mean_after={after_mean:.2f}, "
                                          f"n_before={idx}, n_after={len(values) - idx}",
                            })
                    else:
                        signals.append({
                            "trust_code": trust_code, "icb_code": icb_code, "month_date": dates[-1],
                            "finding": f"{metric}_no_changepoint",
                            "detail": f"no l2+BIC changepoint across {len(values)} months",
                        })

            write_tier3_signals(conn, "corr_signals_tier3b", trust_code, signals)
            all_signals.extend(signals)

        print(f"[OK] Tier 3 Question B: {len(all_signals)} signals across {len(eligible_trusts)} trusts")
        return all_signals
    finally:
        conn.close()


def run_tier3(db_path: str = "data/processed/nhs.duckdb"):
    """Run both Tier 3 questions and print a summary."""
    print("=" * 60)
    print("NHS Monitoring Copilot — Stage 3 Tier 3 (signal-driven ICB resolution)")
    print("=" * 60)

    conn = get_connection(db_path)
    try:
        ensure_tier3_schema_exists(conn)
    finally:
        conn.close()

    print("\n[1/2] Question A — signal trusts vs ICB MoM-delta correlation...")
    run_question_a(db_path)

    print("\n[2/2] Question B — no-signal trusts vs ICB changepoint/drift...")
    run_question_b(db_path)

    conn = get_connection(db_path)
    try:
        for table in ("corr_signals_tier3a", "corr_signals_tier3b"):
            summary = conn.execute(f"""
                SELECT finding, COUNT(*) AS n FROM {table} GROUP BY finding ORDER BY finding
            """).fetchall()
            print(f"\n{table} summary (finding / count):")
            for finding, n in summary:
                print(f"  • {finding:35s} {n}")
    finally:
        conn.close()
    print("=" * 60)


if __name__ == "__main__":
    run_tier3()
