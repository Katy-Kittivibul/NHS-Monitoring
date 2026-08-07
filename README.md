# NHS Monitoring Copilot

A statistics-first monitoring pipeline for UK NHS performance data. It integrates four public NHS datasets into a single DuckDB star schema, then layers three kinds of automated signal detection on top: data quality drift, statistical changepoint detection, and cross-metric/cross-domain correlation. An interactive Streamlit dashboard (`dashboard.py`) sits on top for exploring the results.

This is not an LLM project. No LLM summarisation layer has been built — that was a deliberate scope decision (see [Roadmap](#roadmap)), not an oversight. Every signal in this repository is the output of a statistical test (SPC rules, PELT changepoint detection, Pearson correlation), not a language model.

## Why we built this

NHS operational data is public but fragmented — different domains (NHS England, NHS BSA, OpenPrescribing), different formats (CSV, Excel, JSON), different organisational hierarchies. Understanding whether, say, a rise in A&E breaches coincides with a shift in GP prescribing or dental activity in the same area normally means manually reconciling all of that by hand. This project automates the reconciliation and the statistical signal detection, so the manual work starts from "here are the signals" rather than "here are four unrelated spreadsheets."

## Key Findings & Executive Summary

1. **Most A&E changepoints have no co-occurring primary care shift — but a real minority do (Tier 3 Analysis)**:
   * Of the 80 trust↔ICB "correlated shift" findings in `corr_signals_tier3a` (a trust's A&E changepoint month paired with its ICB's prescribing/dental MoM-delta correlation in the same window), most are not statistically significant. **10 are** (p ≤ 0.05) — e.g. `R1H`↔`QMF` prescribing (r=-0.918, p=0.010), `RCX`↔`QMM` dental (r=-0.983, p=0.000), `RY7`↔`QYG` prescribing (r=0.931, p=0.007).
   * This is evidence, not proof of cause: the *majority* of A&E changepoints aren't accompanied by a statistically significant primary-care shift in the same window, which is consistent with those shifts being internally driven — but it doesn't rule out a relationship the correlation window or metric wasn't sensitive to, and the 10 significant exceptions show it isn't universal either.

2. **Some trusts show strong A&E/RTT co-movement (Tier 2 Correlation)**:
   * A subset of acute trusts show strong positive correlation ($r > 0.6$) between monthly A&E 4-hour breach rates and elective RTT 18+ week waiting-list breaches. One plausible reading is a shared hospital-wide bed/staffing constraint affecting both emergency and elective flow — but the correlation on its own doesn't establish that; it would need corroborating operational data (e.g. occupancy, staffing rotas) to confirm.

3. **l2+BIC outperformed rbf+1.2 on a 10-trust comparison (Stage 2 model selection)**:
   * Benchmarking PELT cost models across 10 acute NHS trusts (R1H, R0A, plus 8 more spanning low/high variance) gave the **l2 cost model with a BIC penalty** (`var(signal) × log(n)`) **3 wins, 4 ties, 0 losses** against a fixed-penalty `rbf` cost (1.2) — rbf+1.2 missed real shifts on R0A, G0Q0L, and R1K. See `cp/changepoints.py`'s docstring for the full comparison.

4. **Organisation-level data quality gap detection**:
   * Generic dataset-wide row-count checks missed single-trust drops (e.g. trust `RET` / The Walton Centre dropping out of A&E data in Sept 2024 was a ~0.5% change — well under dataset thresholds). The per-organisation presence checks (`check_org_missing_vs_prior_month`) added afterwards catch these silent ingestion gaps.

## Pipeline stages

### Stage 0 — ETL & star schema (`etl/`)

Four ETL scripts (`etl/ae_attendances.py`, `etl/rtt_waits.py`, `etl/prescribing.py`, `etl/dental_uda.py`) load into a shared star schema in `data/processed/nhs.duckdb`: two dimensions (`dim_date`, `dim_organisation`) plus two bridge/reference tables built to resolve real ODS-code mismatches discovered while wiring the fact tables together —

- `dim_sub_icb` (`etl/common.py`, sourced from `etl/reference/sub_icb_to_icb.csv`) — bridges GP prescribing's Sub ICB Location codes (e.g. `16C`) to `dim_organisation`'s ICB codes (e.g. `QHM`), since the two code spaces don't overlap directly. Sourced from the ONS Open Geography Portal's April-2024 Sub ICB→ICB→NHS-region lookup, plus one manually-verified exception (`U2G6B`→`S0E4D`, NHS Thames Valley ICB) for a trust merger that postdates that lookup.
- `dim_organisation.specialist_no_ae` (`etl/ae_attendances.py`) — a boolean flag, `TRUE` for trusts with zero A&E attendances across every month they have data for (e.g. orthopaedic-only or oncology-only trusts with no general A&E department), `FALSE` for trusts with real activity, `NULL` for trusts never present in `fact_ae_attendances` at all (e.g. RTT-only trusts). Exists so downstream reporting doesn't fold structurally-inapplicable trusts into the same "0% breach, stable" bucket as genuinely flat ones.

All four ETL scripts are idempotent (delete-then-insert per month) and independently re-runnable.

For the exact raw-column-to-star-schema mapping per dataset (including quirks like the RTT sheet's row-13 header offset and the dental Band 2 pre/post-Nov-2022 split), see `docs/schema_notes.md`.

### Stage 1 — Data quality (`dq/`)

`dq/checks.py` provides four generic, reusable checks, run per dataset via `dq/{ae_attendances,rtt_waits,prescribing,dental_uda}.py` and written to `dq_flags`:

- `check_row_count_jump` — flags a dataset-wide row-count change of more than **15%** (`WARN`) or **30%** (`FAIL`) vs. the prior month present.
- `check_null_rate_jump` — flags a null-rate change of more than **5 percentage points** (`WARN`) or **15 percentage points** (`FAIL`) vs. the prior month, per column.
- `check_out_of_range` — flags any row outside a given `[min, max]` bound (e.g. percentages outside `[0, 100]`).
- `check_org_missing_vs_prior_month` / `check_org_never_reported` — per-organisation checks, added after we confirmed `check_row_count_jump` is dataset-wide only and cannot see a single organisation's rows disappear (a trust dropping from ~200 to ~199 reporting trusts in one month is a ~0.5% change — invisible against a 15% threshold). `check_org_missing_vs_prior_month` flags an org present last month but absent this month; `check_org_never_reported` flags an org that's in `dim_organisation` but has zero rows in the fact table across its entire history. Confirmed catching a real case: trust `RET` (The Walton Centre) dropped out of `fact_ae_attendances` for September 2024 and back the following month — `check_row_count_jump` missed it (198→197 rows, well under threshold); `check_org_missing_vs_prior_month` caught it.

### Stage 2 — Changepoint & SPC detection (`cp/`)

Two independent detection methods on A&E `breach_pct`, written to `cp_signals`:

- **SPC XmR rules** (`cp/spc_baseline.py`) — Rule 1: a point beyond 3 sigma of the mean (sigma estimated from the average moving range, not naive stdev). Rule 2: 7+ consecutive months on the same side of the mean.
- **PELT changepoint detection** (`cp/changepoints.py`, using `ruptures`) — cost model **l2 with a BIC penalty** (`var(signal) × log(n)`), not a fixed penalty. This was chosen after explicitly comparing it against an `rbf` cost with a fixed penalty of 1.2 across 10 trusts (R1H, R0A, plus 8 more spanning low/high variance): **3 wins, 4 ties, 0 losses** for l2+BIC, with rbf+1.2 missing real shifts on R0A, G0Q0L, and R1K. See the docstring in `cp/changepoints.py` for the full comparison.

### Stage 3 — Cross-domain correlation (`corr/`)

Three tiers, each answering a different question, written to `corr_signals` (Tier 1/2) and `corr_signals_tier3a`/`corr_signals_tier3b` (Tier 3):

- **Tier 1 — region level** (`corr/tier1_region.py`): rolling 6-month Pearson correlation between A&E/RTT trust-level trends (rolled up to NHS England region via `dim_organisation.region_name`) and prescribing/dental trends (rolled up via a new ICB→region lookup, `corr/reference/icb_to_region.csv`, built for the same reason as `dim_sub_icb` — no such path existed before). 4 dataset pairs (`ae_prescribing_region`, `ae_dental_region`, `rtt_prescribing_region`, `rtt_dental_region`).
- **Tier 2 — trust level** (`corr/tier2_trust.py`): direct A&E-vs-RTT rolling correlation using the 139 trust codes both datasets share — no geography resolution needed.
- **Tier 3 — signal-driven ICB resolution** (`corr/tier3_signal_driven.py`): only resolves a trust to its ICB when Stage 2 has already flagged something (or, for Question B, deliberately hasn't). ICB resolution (`corr/reference/trust_to_icb.py`) uses NHS ODS's dated `RE8` ("is partner to" an ICB) organisation relationship, time-scoped to the Mar2024–Mar2025 reporting window rather than today's live relationships (NHS England is mid-reorganisation; using current relationships would resolve most trusts to ICB codes that didn't exist during the reporting period). Some trusts genuinely have more than one simultaneously-active ICB partnership (e.g. `RYX`, Central London Community Healthcare, has three) — these are flagged (`multi_icb`) rather than silently assigned to one. Of the 188 trusts needing resolution, 129 resolved to a single ICB, 13 were flagged multi-ICB, 46 had no ICB relationship in the window at all.

  Two separate questions, two separate trust populations:
  - **Question A** (598 signals across 152 trusts, one row per signal month): for every trust with a Stage 2 signal, did its ICB's prescribing/dental month-over-month % change correlate with the trust's own breach_pct month-over-month % change in the same rolling window?
  - **Question B** (73 signals across 36 trusts): for every trust with _no_ Stage 2 signal and real A&E activity (`specialist_no_ae = FALSE`), did its ICB's prescribing/dental series show a PELT changepoint anyway, despite the trust's own A&E series looking flat?

  **On interpreting Tier 2/3 results**: a low or absent correlation between two series is evidence the two didn't move together in the observed window — it is not proof of what _did_ cause either series to move, and it does not rule out a relationship the correlation window or metric choice wasn't sensitive to. Treat correlation findings as inputs to further investigation, not as causal conclusions.

### Dashboard (`dashboard.py`, `dashboard_queries.py`)

A Streamlit app with three native pages (Stage 1/2/3, via `st.navigation`/`st.Page`) and a sidebar trust selector. All queries are extracted into `dashboard_queries.py` (Streamlit-free, directly testable — see `tests/test_dashboard.py`), cached via `st.cache_resource`/`st.cache_data`, and wrapped so a missing table or an empty result shows an inline message instead of crashing the page.

**Known gap**: the dashboard's Tier 1 page queries two views, `corr_signals_tier1` and `corr_signals_tier2` (confirmed as live SQL views — `SELECT * FROM corr_signals WHERE tier = ...` — not materialised snapshots, so they can't go stale). No script in this repository creates them; they exist in the current `nhs.duckdb` because someone ran the `CREATE VIEW` statements directly. A fresh database built purely by running the pipeline steps below will **not** have them, and that page will show an error until they're created:

```sql
CREATE VIEW corr_signals_tier1 AS SELECT * FROM corr_signals WHERE tier = 'tier1_region';
CREATE VIEW corr_signals_tier2 AS SELECT * FROM corr_signals WHERE tier = 'tier2_trust';
```

## Integrated datasets

1. **A&E attendances** — monthly attendances and 4-hour breaches by NHS trust.
2. **RTT waiting times** — waiting list size, 18+ week breaches by provider and specialty.
3. **GP prescribing** — items and cost by GP practice and BNF section (OpenPrescribing).
4. **Dental UDA** — Units of Dental Activity delivered by contract, including the Nov 2022 "Band 2" split into 2a/2b/2c.

## Project structure

```
NHS/
├── data/                    # Raw downloads & DuckDB database (gitignored)
├── etl/                     # Stage 0: ETL scripts + shared dimension builders
│   └── reference/           # Static reference data (Sub ICB → ICB mapping)
├── dq/                      # Stage 1: data quality checks (checks.py + one script per dataset)
├── cp/                      # Stage 2: changepoint (PELT) + SPC signal detection
├── corr/                    # Stage 3: Tier 1/2/3 correlation pipeline
│   └── reference/           # ICB → region lookup, trust → ICB resolver + its cached output
├── docs/                    # docs/schema_notes.md — raw-column-to-star-schema mapping reference
├── reports/plots/           # Scratch comparison plots (see generate_plots.py)
├── tests/                   # pytest suite — one file per package, plus test_dashboard.py
├── dashboard.py             # Streamlit dashboard (UI + navigation + caching)
├── dashboard_queries.py     # Dashboard's query layer (Streamlit-free, directly testable)
├── generate_plots.py        # Scratch script for the Stage 2 cost-model comparison plots
├── requirements.txt
└── README.md
```

## Setup & usage

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Run the ETL pipeline (Stage 0)

```bash
python etl/run_all.py
```

### 3. Run data quality checks (Stage 1)

```bash
python dq/run_all.py
```

### 4. Run changepoint & SPC detection (Stage 2)

```bash
python cp/run_all.py
```

### 5. Run cross-domain correlation (Stage 3, Tier 1 & 2)

```bash
python corr/run_all.py
```

### 6. Resolve trust→ICB and run Tier 3

`corr/reference/trust_to_icb.py` makes a live HTTP request to the NHS ODS API per trust in the Tier 3 universe (~188 requests) — this step needs network access and takes a few minutes. Its output is cached to `corr/reference/trust_to_icb.csv`, so it only needs re-running if the trust universe changes.

```bash
python corr/reference/trust_to_icb.py
python corr/tier3_signal_driven.py
```

### 7. Run the test suite

```bash
python -m pytest tests/ -q
```

### 8. Launch the dashboard

```bash
streamlit run dashboard.py
```

See the **Known gap** note above — `corr_signals_tier1`/`corr_signals_tier2` need to exist before the Tier 1 page will load.

## Roadmap

- [x] Stage 0 — ETL pipeline & DuckDB star schema
- [x] Stage 1 — data quality drift detection, including per-organisation gap checks
- [x] Stage 2 — changepoint (PELT) & SPC signal detection
- [x] Stage 3 — Tier 1 (region), Tier 2 (trust), Tier 3 (signal-driven ICB resolution) correlation
- [x] Interactive dashboard
