"""Build a small, deterministic, synthetic DuckDB for CI (and for trying the reporting
layer without downloading anything).

    python tests/fixtures/build_fixture_db.py [out.duckdb]

The numbers are made up (seeded), but the trust/ICB/region *codes* come from the tracked
reference files in corr/reference/, so the SQL views resolve geography exactly as they do
on real data. It carries every table reporting/load_postgres.py copies, with enough shape
to exercise each view rule: 18 months (the XmR view needs 15+ points), a trust with no
Type 1 activity, a specialist trust with no A&E attendances at all, a non-"R" provider,
and C_999 RTT totals that equal the sum of their specialty rows.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from corr.inference import annotate_fdr  # noqa: E402
from corr.signals import SIGNAL_COLUMNS, ensure_corr_schema_exists  # noqa: E402
from corr.tier3_signal_driven import ensure_tier3_schema_exists  # noqa: E402
from cp.signals import ensure_cp_schema_exists  # noqa: E402
from dq.checks import ensure_dq_schema_exists  # noqa: E402
from etl.common import (  # noqa: E402
    REGION_CODE_TO_NAME,
    create_dim_date,
    ensure_schema_exists,
    get_connection,
    replace_months,
    upsert_organisations,
)

REFERENCE = _ROOT / "corr" / "reference"
MONTHS = pd.date_range("2024-03-01", periods=18, freq="MS")
SPECIALTIES = [("C_100", "General Surgery"), ("C_110", "Trauma & Orthopaedics"), ("C_120", "ENT")]
NO_TYPE1_TRUST_INDEX = 1  # this trust only has "other" (Type 3) A&E activity
SPECIALIST_TRUST_INDEX = 2  # this trust reports zero attendances every month


def pick_trusts() -> pd.DataFrame:
    """Two single-ICB 'R' trusts per region that the reference files can resolve."""
    trusts = pd.read_csv(REFERENCE / "trust_icb_current.csv")
    regions = pd.read_csv(REFERENCE / "icb_to_region.csv")
    merged = trusts[trusts["n_icbs"] == 1].merge(regions, on="icb_code", suffixes=("", "_icb"))
    merged = merged[merged["trust_code"].str.match(r"^R[A-Z0-9]{2}$")].sort_values("trust_code")
    picked = merged.groupby("region_code").head(2).sort_values(["region_code", "trust_code"])
    return picked[["trust_code", "region_code", "icb_code"]].reset_index(drop=True)


def build_facts(trusts: pd.DataFrame, rng: np.random.RandomState):
    ae_rows, rtt_rows = [], []
    for i, (code, region) in enumerate(zip(trusts["trust_code"], trusts["region_code"])):
        region_label = REGION_CODE_TO_NAME[region]
        scale = 3000 + 450 * i
        for t, month in enumerate(MONTHS):
            season = 1 + 0.08 * np.sin(2 * np.pi * (month.month - 1) / 12)
            if i == SPECIALIST_TRUST_INDEX:
                type1 = type2 = other = 0
            elif i == NO_TYPE1_TRUST_INDEX:
                type1, type2, other = 0, 0, int(scale * season)
            else:
                type1 = int(scale * season)
                type2 = int(scale * 0.1 * season)
                other = int(scale * 0.2 * season)
            total = type1 + type2 + other
            rate = 0.18 + 0.003 * t + 0.02 * rng.rand() + 0.01 * (i % 4)  # breach share
            b1, b2, bo = (int(x * rate) for x in (type1, type2, other))
            ae_rows.append({
                "month_date": month.date(), "org_code": code, "org_name": f"FIXTURE TRUST {code}",
                "parent_org": region_label, "attendances_type1": type1, "attendances_type2": type2,
                "attendances_other": other, "total_attendances": total, "breaches_4hr_type1": b1,
                "breaches_4hr_type2": b2, "breaches_4hr_other": bo, "total_breaches_4hr": b1 + b2 + bo,
                "breach_pct": (b1 + b2 + bo) / total * 100.0 if total else 0.0,
                "admissions_via_ae": int(type1 * 0.25),
            })
            totals = dict(waiting_list_size=0, over_18wk_count=0, over_52wk_count=0)
            for j, (spec_code, spec_name) in enumerate(SPECIALTIES):
                size = int((900 + 120 * i + 60 * j) * (1 + 0.01 * t))
                over18 = int(size * (0.30 + 0.01 * j + 0.1 * rng.rand() * 0.5))
                over52 = int(over18 * 0.05)
                totals["waiting_list_size"] += size
                totals["over_18wk_count"] += over18
                totals["over_52wk_count"] += over52
                rtt_rows.append(_rtt_row(month, code, region, spec_code, spec_name, size, over18, over52))
            rtt_rows.append(_rtt_row(month, code, region, "C_999", "Total", totals["waiting_list_size"],
                                     totals["over_18wk_count"], totals["over_52wk_count"]))
    # An independent-sector provider: RTT only, and not an "R" code.
    for t, month in enumerate(MONTHS):
        size = 400 + 5 * t
        rtt_rows.append(_rtt_row(month, "NXA01", "Y56", "C_100", "General Surgery", size // 2, size // 10, 0))
        rtt_rows.append(_rtt_row(month, "NXA01", "Y56", "C_999", "Total", size // 2, size // 10, 0))
    return pd.DataFrame(ae_rows), pd.DataFrame(rtt_rows)


def _rtt_row(month, code, region, spec_code, spec_name, size, over18, over52):
    return {
        "month_date": month.date(), "org_code": code, "org_name": f"FIXTURE TRUST {code}",
        "region_code": region, "specialty_code": spec_code, "specialty_name": spec_name,
        "waiting_list_size": size, "over_18wk_count": over18,
        "over_18wk_pct": over18 / size * 100.0 if size else 0.0, "median_wait_weeks": 9.5,
        "over_52wk_count": over52,
    }


def build_primary_care(trusts: pd.DataFrame, rng: np.random.RandomState):
    """Sub-ICBs, GP prescribing (13 months to 2025-03, like the real extract) and dental contracts."""
    icbs = pd.read_csv(REFERENCE / "icb_to_region.csv").set_index("icb_code")["icb_name"]
    sub_icbs, rx_rows, dental_rows = [], [], []
    for n, icb in enumerate(sorted(trusts["icb_code"].unique())):
        icb_name = icbs.get(icb, f"FIXTURE ICB {icb}")
        for k in range(2):
            sub = f"F{n:02d}{k}"
            sub_icbs.append({"sub_icb_code": sub, "sub_icb_name": f"{icb_name} - {sub}",
                             "icb_code": icb, "icb_name": icb_name})
            for pr in range(3):
                for month in MONTHS[:13]:
                    items = int(2000 + 150 * pr + 500 * rng.rand())
                    rx_rows.append({
                        "month_date": month.date(), "practice_code": f"F{n:02d}{k}{pr:02d}",
                        "practice_name": f"FIXTURE PRACTICE {n}{k}{pr}", "sub_icb_code": sub,
                        "bnf_section_code": "0205", "setting_code": 4, "items": items,
                        "quantity": items * 28.0, "actual_cost": items * 4.25,
                    })
        for c in range(2):
            for month in MONTHS:
                target = 900.0 + 100 * c
                delivered = target * (0.8 + 0.2 * rng.rand())
                dental_rows.append({
                    "month_date": month.date(), "icb_code": icb, "icb_name": icb_name,
                    "contract_number": f"FX{n:02d}{c}", "provider_name": f"FIXTURE DENTAL {n}{c}",
                    "uda_target": target, "uda_delivered": delivered,
                    "band_1_delivered": delivered * 0.5, "band_2_delivered": delivered * 0.3,
                    "band_3_delivered": delivered * 0.1, "band_urgent_delivered": delivered * 0.1,
                })
    return pd.DataFrame(sub_icbs), pd.DataFrame(rx_rows), pd.DataFrame(dental_rows)


def build_corr_signals(trusts: pd.DataFrame):
    """A few Tier 1/2/3 rows in the formats the loaders write ('r=.., p=.., n=6')."""
    rows = []
    for region in sorted(trusts["region_code"].unique()):
        for month in MONTHS[-3:]:
            rows.append({"tier": "tier1_region", "dataset_pair": "ae_dental_region",
                         "geography_level": "region", "geography_code": region, "month_date": month.date(),
                         "correlation_metric": 0.55, "detail": "r=0.550, p=0.258, n=6", "p_value": 0.258,
                         "n_obs": 6, "ci_low": -0.57, "ci_high": 0.95})
    for i, code in enumerate(trusts["trust_code"]):
        r = 0.7 if i % 2 else -0.3
        p = 0.0001 if i == 1 else 0.120  # one clearly significant trust, so the FDR flag is exercised
        for month in MONTHS[-3:]:
            rows.append({"tier": "tier2_trust", "dataset_pair": "ae_rtt_trust", "geography_level": "trust",
                         "geography_code": code, "month_date": month.date(), "correlation_metric": r,
                         "detail": f"r={r:.3f}, p={p:.3f}, n=6", "p_value": p, "n_obs": 6,
                         "ci_low": -0.8, "ci_high": 0.9})
    annotate_fdr(rows, family_keys=("tier", "dataset_pair", "month_date"))
    return pd.DataFrame(rows).reindex(columns=SIGNAL_COLUMNS)


def build_tier3(trusts: pd.DataFrame):
    month = MONTHS[-1].date()
    a_findings = [("dental_correlated_shift", "r=0.589, p=0.219, n=6", 0.219),
                  ("prescribing_no_correlated_shift", "r=-0.043, p=0.935, n=6", 0.935),
                  ("dental_insufficient_window", "no full 6-month paired MoM-delta window", None)]
    tier3a, tier3b = [], []
    for i, (code, icb) in enumerate(zip(trusts["trust_code"], trusts["icb_code"])):
        if i % 3 == 0:
            finding, detail, p = a_findings[(i // 3) % 3]
            tier3a.append((code, icb, month, finding, detail, p, 6 if p is not None else None,
                           -0.5 if p is not None else None, 0.9 if p is not None else None))
        elif i % 3 == 1:
            tier3b.append((code, icb, month, "dental_changepoint_detected",
                           "mean_before=6.10, mean_after=6.34, n_before=7, n_after=7"))
        else:
            tier3b.append((code, icb, month, "prescribing_no_changepoint",
                           "no l2+BIC changepoint across 13 months"))
    tier3a.append((trusts["trust_code"].iloc[0], "QH8", month, "prescribing_skipped_multi_icb",
                   "candidate ICBs: QH8, QMF", None, None, None, None))
    cols = ["trust_code", "icb_code", "month_date", "finding", "detail"]
    a = pd.DataFrame(tier3a, columns=cols + ["p_value", "n_obs", "ci_low", "ci_high"])
    a["metric"] = a["finding"].str.split("_").str[0].where(a["p_value"].notna())
    rows = a.to_dict("records")
    annotate_fdr([r for r in rows if isinstance(r["metric"], str)], family_keys=("metric",))
    a = pd.DataFrame(rows).reindex(columns=cols + ["p_value", "n_obs", "ci_low", "ci_high", "q_value",
                                                   "is_significant_fdr"])
    return a, pd.DataFrame(tier3b, columns=cols)


def build_pipeline_runs() -> pd.DataFrame:
    rows = []
    for i, (dataset, status) in enumerate([("ae_attendances", "success"), ("rtt_waits", "success"),
                                           ("prescribing", "skipped"), ("dental_uda", "failed")]):
        start = pd.Timestamp("2026-11-01 09:00:00") + pd.Timedelta(minutes=i)
        rows.append({"run_id": f"fixture-run-{i}", "stage": "etl", "dataset": dataset,
                     "started_at": start, "finished_at": start + pd.Timedelta(seconds=12 + i),
                     "status": status, "rows_processed": 1000 * (i + 1), "detail": "{}"})
    return pd.DataFrame(rows)


def build_fixture_db(path: str) -> dict:
    """Create `path` from scratch; returns a few row counts for the caller to print."""
    Path(path).unlink(missing_ok=True)
    rng = np.random.RandomState(20240301)
    trusts = pick_trusts()
    ae, rtt = build_facts(trusts, rng)

    conn = get_connection(path)
    try:
        ensure_schema_exists(conn)
        ensure_dq_schema_exists(conn)
        ensure_cp_schema_exists(conn)
        ensure_corr_schema_exists(conn)
        ensure_tier3_schema_exists(conn)
        create_dim_date(conn, "2024-01-01", "2026-12-31")
        replace_months(conn, "fact_ae_attendances", ae)
        replace_months(conn, "fact_rtt_waits", rtt)

        codes = [*trusts["trust_code"], "NXA01"]
        orgs = pd.DataFrame({
            "org_code": codes,
            "org_name": [f"FIXTURE TRUST {c}" for c in codes],
            "region_code": [*trusts["region_code"], "Y56"],
        })
        orgs["region_name"] = orgs["region_code"].map(REGION_CODE_TO_NAME)
        orgs["org_type"] = "trust"
        upsert_organisations(conn, orgs, ["org_name", "region_name", "region_code"])
        for flag, comparison in (("FALSE", ">"), ("TRUE", "=")):
            conn.execute(
                f"UPDATE dim_organisation SET specialist_no_ae = {flag} WHERE org_code IN "
                f"(SELECT org_code FROM fact_ae_attendances GROUP BY 1 "
                f"HAVING SUM(total_attendances) {comparison} 0)"
            )

        flags = pd.DataFrame([
            {"dataset": dataset, "month_date": MONTHS[-1].date(), "check_name": name,
             "status": status, "detail": f"fixture {name}"}
            for dataset in ("ae_attendances", "rtt_waits")
            for name, status in (("check_row_count", "OK"), ("check_no_total_org", "WARN"))
        ])
        conn.register("_flags", flags)
        conn.execute("INSERT INTO dq_flags SELECT * FROM _flags")
        detail = "mean_before=0.20, mean_after=0.30, n_before=9, n_after=9, min_monthly_volume=1000"
        first, second = trusts["trust_code"].iloc[0], trusts["trust_code"].iloc[3]
        signals = pd.DataFrame([
            # robust: the peer-relative series agrees within a month
            {"dataset": "ae_attendances", "org_code": first, "month_date": MONTHS[9].date(),
             "method": "pelt_changepoint", "signal_type": "changepoint", "detail": detail},
            {"dataset": "ae_attendances_peer", "org_code": first, "month_date": MONTHS[10].date(),
             "method": "pelt_changepoint", "signal_type": "changepoint", "detail": detail},
            # not robust: raw only
            {"dataset": "ae_attendances", "org_code": second, "month_date": MONTHS[5].date(),
             "method": "pelt_changepoint", "signal_type": "changepoint", "detail": detail},
            {"dataset": "rtt_waits", "org_code": second, "month_date": MONTHS[6].date(),
             "method": "spc_rule1_3sigma", "signal_type": "beyond_3sigma_above",
             "detail": "value=40.00, mean=30.00, UCL=38.00, min_monthly_volume=500"},
        ])
        conn.register("_signals", signals)
        conn.execute("INSERT INTO cp_signals SELECT * FROM _signals")

        sub_icbs, prescribing, dental = build_primary_care(trusts, rng)
        tier3a, tier3b = build_tier3(trusts)
        for table, frame in (
            ("dim_sub_icb", sub_icbs), ("fact_prescribing", prescribing), ("fact_dental_uda", dental),
            ("corr_signals", build_corr_signals(trusts)), ("corr_signals_tier3a", tier3a),
            ("corr_signals_tier3b", tier3b), ("pipeline_runs", build_pipeline_runs()),
        ):
            conn.register("_frame", frame)
            conn.execute(f"INSERT INTO {table} SELECT * FROM _frame")
            conn.unregister("_frame")
        tables = ("dim_organisation", "fact_ae_attendances", "fact_rtt_waits", "dq_flags", "cp_signals",
                  "fact_prescribing", "fact_dental_uda", "corr_signals", "pipeline_runs")
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        conn.close()
    return counts


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "data/processed/fixture.duckdb"
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    print(f"[OK] Built fixture DuckDB {target}: {build_fixture_db(target)}")
