# NHS Monitoring Copilot — Confirmed Schema & ETL Specifications

This document records the empirical findings from inspecting raw sample files for all 4 NHS datasets (A&E, RTT, OpenPrescribing, Dental UDA) in Stage 0. It defines the exact row grain, column mappings, org code formats, data quality handling, and star schema specifications for DuckDB table creation in Stage 0.

---

## 1. A&E Attendances Dataset

### Sample Inspected
- File: `data/raw/ae_attendances_202403.csv` (and `ae_attendances_202403.xls`)
- Source: NHS England Monthly A&E Attendances and Emergency Admissions Statistics.

### Raw Data Profile
- **Actual Grain**: Provider (NHS Trust / Walk-in Centre / UTC) × Month.
- **Org Code System**: ODS Provider Code (3-character NHS Trust codes e.g. `RC9`, `RP6`, `RGT` or 5-character ODS provider codes e.g. `AD913`).
- **Parent Org**: NHS England Region name (e.g., `NHS ENGLAND EAST OF ENGLAND`).

### Raw to Star Schema Column Mapping (`fact_ae_attendances`)

| Target Field (`fact_ae_attendances`) | Raw CSV Column Header | Data Type | Notes / Transformations |
| :--- | :--- | :--- | :--- |
| `month_date` | `Period` | `DATE` | Parse `MSitAE-MARCH-2024` → `2024-03-01` |
| `org_code` | `Org Code` | `VARCHAR` | ODS Provider Code (e.g. `RC9`) |
| `org_name` | `Org name` | `VARCHAR` | Provider Name |
| `parent_org` | `Parent Org` | `VARCHAR` | NHS Region Name |
| `attendances_type1` | `A&E attendances Type 1` | `BIGINT` | Major A&E departments |
| `attendances_type2` | `A&E attendances Type 2` | `BIGINT` | Single specialty departments |
| `attendances_other` | `A&E attendances Other A&E Department` | `BIGINT` | Type 3 / Urgent Treatment Centres |
| `total_attendances` | *Derived* | `BIGINT` | `attendances_type1 + attendances_type2 + attendances_other` |
| `breaches_4hr_type1` | `Attendances over 4hrs Type 1` | `BIGINT` | Waits > 4hrs in Type 1 |
| `breaches_4hr_type2` | `Attendances over 4hrs Type 2` | `BIGINT` | Waits > 4hrs in Type 2 |
| `breaches_4hr_other` | `Attendances over 4hrs Other Department` | `BIGINT` | Waits > 4hrs in Type 3/Other |
| `total_breaches_4hr` | *Derived* | `BIGINT` | Sum of Type 1, 2, and Other 4-hr breaches |
| `breach_pct` | *Derived* | `DOUBLE` | `total_breaches_4hr / total_attendances * 100.0` |
| `admissions_via_ae` | `Emergency admissions via A&E - Type 1` + `Type 2` + `Other...` | `BIGINT` | Sum of emergency admissions via A&E |

### Data Quality & Suppression Quirks
- **Null representation**: Missing/unreported values in raw Excel are blank or `'-'`; converted to `0` in raw CSV export.
- **Provider types**: Includes NHS Acute Trusts, Specialist Trusts, and independent Urgent Treatment Centres (UTCs). Filter by acute trust if comparing acute-only performance.

---

## 2. RTT Waiting Times Dataset

### Sample Inspected
- File: `data/raw/rtt_incomplete_provider_202403.xlsx`
- Source: NHS England Consultant-led RTT Waiting Times (Incomplete Pathways by Provider).

### Raw Data Profile
- **Actual Grain**: Provider × Specialty (Treatment Function Code) × Month.
- **Header Offset**: Excel sheet headers start at **Row 13** (0-indexed line 12). Rows 0..11 contain title metadata.
- **Org Code System**: NHS Provider ODS Code (3-character NHS Trust codes e.g. `RP6`, `RGT` or 5-character independent codes e.g. `AY1`).
- **Specialty Codes**: `Treatment Function Code` (e.g. `C_100` General Surgery, `C_101` Urology, `C_110` Trauma & Orthopaedics, `C_999` Total).

### Raw to Star Schema Column Mapping (`fact_rtt_waits`)

| Target Field (`fact_rtt_waits`) | Raw Excel Column Header | Data Type | Notes / Transformations |
| :--- | :--- | :--- | :--- |
| `month_date` | File metadata / filename | `DATE` | Extract month from filename/sheet (e.g. `2024-03-01`) |
| `org_code` | `Provider Code` | `VARCHAR` | ODS Provider Code (e.g. `RP6`) |
| `org_name` | `Provider Name` | `VARCHAR` | Provider Name |
| `region_code` | `Region Code` | `VARCHAR` | Region Code (e.g. `Y56`) |
| `specialty_code` | `Treatment Function Code` | `VARCHAR` | e.g. `C_100` or `C_999` |
| `specialty_name` | `Treatment Function` | `VARCHAR` | e.g. `General Surgery Service` |
| `waiting_list_size` | Sum of week bins / Total column | `BIGINT` | Total incomplete pathways |
| `over_18wk_count` | Sum of bins `>18-19` to `>104` | `BIGINT` | Derived sum of pathways waiting over 18 weeks |
| `over_18wk_pct` | *Derived* | `DOUBLE` | `(over_18wk_count / waiting_list_size) * 100.0` |
| `median_wait_weeks` | Derived / `50th percentile` column | `DOUBLE` | Median wait in weeks |

### Data Quality & Suppression Quirks
- **Metadata rows**: Header reading must skip top 12 rows.
- **Aggregate specialty**: Code `C_999` (name `Total`) is the provider-level total across all specialties and is stored alongside the specialty rows, so summing `waiting_list_size` over every row double counts (14.3M instead of 7.16M for 2024-03). Filter `specialty_code = 'C_999'` for trust totals, or `<> 'C_999'` for specialty detail; never mix them.
- **Suppression**: Unreported cells contain `'-'` or `NaN`; replace with `0` or `NULL` appropriately.

---

## 3. GP Prescribing Dataset

### Sample Inspected
- File: `data/raw/openprescribing_spending_202310.json`
- Source: OpenPrescribing REST API (`https://openprescribing.net/api/1.0/spending_by_practice/`)

### Raw Data Profile
- **Actual Grain**: GP Practice ODS Code × Month × BNF Section.
- **Org Code System**:
  - `row_id`: 6-character GP Practice ODS Code (e.g. `A81001`, `F84001`).
  - `ccg`: 3-character CCG / Sub-ICB Location ODS Code (e.g. `16C`, `00L`).
- **Setting Filter**: `setting == 4` isolates standard GP practices (excluding out-of-hours, walk-in centres, and specialized settings).

### Raw to Star Schema Column Mapping (`fact_prescribing`)

| Target Field (`fact_prescribing`) | API JSON Key | Data Type | Notes / Transformations |
| :--- | :--- | :--- | :--- |
| `month_date` | `date` | `DATE` | Parse `YYYY-MM-DD` (e.g. `2023-10-01`) |
| `practice_code` | `row_id` | `VARCHAR` | Practice ODS Code (e.g. `A81001`) |
| `practice_name` | `row_name` | `VARCHAR` | Practice Name |
| `sub_icb_code` | `ccg` | `VARCHAR` | Sub-ICB / CCG ODS Code (e.g. `16C`) |
| `bnf_section_code` | Query parameter / API code | `VARCHAR` | e.g. `0205` (Hypertension & heart failure) |
| `setting_code` | `setting` | `INTEGER` | Filter for `setting == 4` (GP Practice) |
| `items` | `items` | `BIGINT` | Total prescription items dispensed |
| `quantity` | `quantity` | `DOUBLE` | Total quantity/dose dispensed |
| `actual_cost` | `actual_cost` | `DOUBLE` | Net actual cost in GBP (£) |

### Data Quality & Suppression Quirks
- **Null `ccg`**: ~0.15% of closed/unmapped practices have `ccg: null`. Map to `'UNKNOWN'` or keep `NULL`.
- **API Cleanliness**: OpenPrescribing returns clean numeric floats/ints without text suppression characters.

---

## 4. Dental UDA Activity Dataset

### Sample Inspected
- Files: `data/raw/dental_uda_202211.csv` and `data/raw/dental_uda_202311.csv`
- Source: NHSBSA Open Data Portal (`opendata.nhsbsa.net`, package `english-contractor-monthly-general-dental-activity`).

### Raw Data Profile
- **Actual Grain**: Dental Contract Number × Commissioner (ICB) × Month.
- **Org Code System**:
  - `COMMISSIONER_CODE`: 3-character ICB ODS Code (e.g. `QYG`, `QMM`, `QXU`).
  - `CONTRACT_NUMBER`: 10-digit NHS Dental Contract Number (e.g. `1000080000`).

### Band 2 Regulatory Change (Crucial Finding)
- **Pre-Nov 2022** (`202211` and earlier): Single column `BAND_2_DELIVERED`.
- **Post-Nov 2022** (`202311` and later): Split into 3 columns: `BAND_2A_DELIVERED`, `BAND_2B_DELIVERED`, `BAND_2C_DELIVERED`.
- **Harmonisation Rule**:
  - `band_2_total = BAND_2_DELIVERED` (if present) **OR** `(BAND_2A_DELIVERED + BAND_2B_DELIVERED + BAND_2C_DELIVERED)` (if split).

### Raw to Star Schema Column Mapping (`fact_dental_uda`)

| Target Field (`fact_dental_uda`) | Raw CSV Column Header | Data Type | Notes / Transformations |
| :--- | :--- | :--- | :--- |
| `month_date` | `YEAR_MONTH` | `DATE` | Parse `202211` → `2022-11-01` |
| `icb_code` | `COMMISSIONER_CODE` | `VARCHAR` | ICB ODS Code (e.g. `QYG`) |
| `icb_name` | `COMMISSIONER_NAME` | `VARCHAR` | ICB Name |
| `contract_number` | `CONTRACT_NUMBER` | `VARCHAR` | 10-digit Dental Contract ID |
| `provider_name` | `PROVIDER_NAME` | `VARCHAR` | Dental Provider Name |
| `uda_target` | `UDA_PERF_TARGET` | `DOUBLE` | Annual UDA Target |
| `uda_delivered` | `UDA_DELIVERED` | `DOUBLE` | Units of Dental Activity delivered in month |
| `band_1_delivered` | `BAND_1_DELIVERED` | `DOUBLE` | Band 1 UDA |
| `band_2_delivered` | Derived (`BAND_2_DELIVERED` or `2A+2B+2C`) | `DOUBLE` | Harmonised total Band 2 UDA |
| `band_3_delivered` | `BAND_3_DELIVERED` | `DOUBLE` | Band 3 UDA |
| `band_urgent_delivered`| `BAND_URGENT_DELIVERED` | `DOUBLE` | Urgent Band UDA |

### Data Quality & Suppression Quirks
- **Whitespace in headers**: Column headers in raw CSV have leading spaces (e.g. `' COMMISSIONER_CODE'`). ETL script MUST `.strip()` all column names.
- **Null values**: Non-submitting or inactive contracts show `NaN` for `UDA_DELIVERED` and band columns. Treat `NaN` as `0.0` or `NULL` depending on analysis context.

---

## 5. Star Schema & Organisation Harmonisation Strategy

### Shared Dimension 1: `dim_organisation`
Enables cross-dataset correlation in Stage 3 by linking trusts, practices, and commissioners:

```sql
CREATE TABLE dim_organisation (
    org_code VARCHAR PRIMARY KEY,    -- ODS Code: Trust code (e.g. 'RC9'), Practice code ('A81001'), ICB code ('QYG')
    org_name VARCHAR,                -- Human-readable name
    org_type VARCHAR,                -- 'trust', 'practice', 'icb' — dental only ever contributes 'icb' rows
                                      -- (COMMISSIONER_CODE), not one row per dental contract
    parent_org_code VARCHAR,         -- Parent ICB or NHS Region code
    region_name VARCHAR,             -- NHS Region, canonical full name e.g. 'NHS ENGLAND MIDLANDS'
    specialist_no_ae BOOLEAN,        -- trust-only; see README's Stage 0 section
    region_code VARCHAR              -- NHS Region code, 'Y56'..'Y63' (trusts only)
);
```

**Type precedence.** The same code can legitimately appear in two roles — an A&E walk-in
centre is a `trust` to A&E/RTT and a `practice` in GP prescribing. Every loader writes
through `etl.common.upsert_organisations`, which ranks `trust` > `icb` > `practice` and
never lets a lower-ranked load overwrite a higher-ranked row's type *or* attributes, so the
result no longer depends on load order. (Databases built before this fix may still hold
`practice` for those walk-in centres; `sql/02_dim_trust.sql` derives trust membership from
the fact tables for that reason.)

**Region.** `region_name` used to hold the region *code* when the RTT load ran last and the
full *name* otherwise. Both loads now go through `etl.common.normalise_region` and write the
canonical pair (`region_code`, `region_name`). A reload (`--force`) fixes an older database.

### Run observability: `pipeline_runs` and `ingestion_manifest`
```sql
CREATE TABLE pipeline_runs (        -- one row per (stage, dataset) execution, success or failure
    run_id VARCHAR PRIMARY KEY, stage VARCHAR, dataset VARCHAR,
    started_at TIMESTAMP, finished_at TIMESTAMP,
    status VARCHAR,                  -- running | success | skipped | failed
    rows_processed BIGINT,
    detail VARCHAR                   -- JSON {files_loaded, files_skipped}, or the exception text
);
CREATE TABLE ingestion_manifest (   -- one row per raw file currently reflected in the facts
    file_name VARCHAR PRIMARY KEY, dataset VARCHAR, sha256 VARCHAR,
    loaded_at TIMESTAMP, row_count BIGINT
);
```
A raw file whose sha256 is already in `ingestion_manifest` is skipped on the next run;
`python etl/run_all.py --force` reloads everything. The manifest row is the commit marker:
it is deleted before a file is written and re-inserted only after the write completes, so a
crash mid-load means that file is retried, never skipped. (An explicit transaction would be
simpler, but the DuckDB pinned in `requirements.txt` rejects a same-transaction delete and
re-insert of one primary key.) Loading new data makes `dq_flags`, `cp_signals` and the
correlation tables stale — `etl/run_all.py` says so when any file was loaded.

### Source availability (checked 2026-11-26)
| Dataset | Latest published | Fetched by |
| :--- | :--- | :--- |
| A&E, RTT | 2026-08 | `etl/download.py` (Playwright) |
| NHSBSA dental UDA | 2026-08 | `etl/download.py --datasets dental` (plain HTTP) |
| GP prescribing | OpenPrescribing blocks scripted clients (Cloudflare challenge); the NHSBSA EPD it is built on ends 2025-06 and is ~7 GB/month | manual download |

So GP prescribing stays at 2025-03 and any cross-dataset correlation involving it must be
limited to the months both datasets cover. NHSBSA changed its dental files from 2026: the
period became `YYYY-MM` and `BAND_URGENT_DELIVERED` became `BAND_UNSCHEDULED_DELIVERED`
(monthly totals are continuous across the rename); some months (2025-04, 2025-05) are
Windows-1252, not UTF-8. `etl/dental_uda.py` handles all three.

Two further tables were added later, once real ODS-code mismatches surfaced while wiring
Stage 3's correlation pipeline together — `dim_sub_icb` (bridges GP prescribing's Sub ICB
codes to `dim_organisation`'s ICB codes) and `corr/reference/icb_to_region.csv` (ICB → NHS
region). See the README's Stage 0 and Stage 3 sections for what each one solves and where
it's sourced from — not repeated here since this file predates both.

### Shared Dimension 2: `dim_date`
```sql
CREATE TABLE dim_date (
    date_key DATE PRIMARY KEY,       -- First day of month e.g. 2024-03-01
    year INT,                        -- 2024
    month INT,                       -- 3
    month_name VARCHAR,              -- 'March'
    quarter VARCHAR,                 -- 'Q4'
    financial_year VARCHAR           -- '2023/24'
);
```

## 6. PostgreSQL reporting layer (`reporting/` + `sql/`)

`make reporting` copies these DuckDB tables into the PostgreSQL schema `reporting` in **one
transaction** (a failed refresh leaves the previous schema readable; row counts are checked
before COMMIT) and then applies the views in `sql/run_all.sql`.

| Table | Grain | Rows (real data) | Notes |
|---|---|---|---|
| `dim_organisation`, `dim_date`, `dim_sub_icb` | org / month / sub-ICB | 6.6k / 96 / 107 | `dim_organisation.region_code` is a clean code |
| `fact_ae_attendances` | org × month | ~5.9k | |
| `fact_rtt_waits` | org × specialty × month | ~109k | `C_999` is the per-trust total |
| `fact_prescribing` | month × practice × BNF section × setting | ~82k | one BNF section (0205) today; aggregate in DuckDB before loading if more are added |
| `fact_dental_uda` | month × contract | ~95k | |
| `dq_flags`, `cp_signals` | see Stage 1/2 | | |
| `corr_signals` | tier × dataset pair × geography × month | ~3.4k | Tier 1 + Tier 2 |
| `corr_signals_tier3a`, `corr_signals_tier3b` | trust × ICB × month × finding | ~2.1k / 85 | Tier 3 |
| `pipeline_runs` | one ETL run | | optional tables load **empty** if their stage has not run yet |

| View | Grain | Purpose |
|---|---|---|
| `vw_ae_targets` | financial year | national A&E target by date range (a parameter view, not a literal) |
| `vw_dim_date`, `vw_dim_trust` | month / provider | conformed dimensions |
| `vw_ae_performance` | trust × month | now also `admissions`, `admission_conversion_pct`, `excess_breaches_vs_target` |
| `vw_rtt_performance`, `vw_rtt_specialty` | trust × month (× specialty) | never add them together |
| `vw_spc_xmr`, `vw_benchmark`, `vw_data_quality` | | control charts, benchmarks, DQ |
| `vw_changepoints` | one `cp_signals` row | Stage 2 signals with trust/ICB attributes and parsed values |
| `vw_correlations` | tier × pair × geography × month | Tier 1/2/3 unified; r, p, n parsed from `detail`; **exploratory only** (n = 6, p-values uncorrected) |
| `vw_primary_care_icb` | ICB × month | prescribing and dental side by side; either side can be NULL for a month |
| `vw_pipeline_health` | pipeline run | duration, failure flag, latest data month per dataset |

`excess_breaches_vs_target` is the number of breaches above what the target allows, floored at
0. `vw_correlations.finding_category = 'Correlated'` means |r| >= 0.5, **not** statistical
significance; Phase 2 will add a multiple-testing-corrected `q_value`.

## 7. Statistical method (Stage 2 and Stage 3)

**Stage 2 series.** `cp_signals.dataset` now holds four monitored series, each with its own
XmR limits and PELT penalty (nothing is shared between A&E and RTT):

| `dataset` | Series |
|---|---|
| `ae_attendances` | trust A&E breach % (the headline series Tier 3 and the dashboard read) |
| `ae_attendances_peer` | trust breach % minus the national volume-weighted breach % for the month |
| `rtt_waits` | trust RTT % waiting over 18 weeks (`C_999` totals) |
| `rtt_waits_peer` | the same, minus the national weighted value |

*Why peer-relative.* With ~30 months there are only ~2.5 seasonal cycles, so a per-calendar-month
seasonal offset is poorly estimated and can absorb a real shift. Subtracting the same month's
national value removes shared seasonality and national shocks without a seasonal model.
`vw_changepoints.is_robust` is TRUE for a raw changepoint when the peer-relative series has one
for the same trust within a month. Trusts whose **median monthly volume** is below
`cp.signals.MIN_MONTHLY_VOLUME` (A&E 1,000 attendances, RTT 500 pathways) are not monitored, and
the floor is written into each signal's `detail`.

*PELT penalty.* The BIC penalty is `2 x var x log(n)`. The previous `1 x var x log(n)` reported a
spurious changepoint in 50-60% of pure-noise series (seeded simulation in
`tests/test_cp.py::TestPenaltyCalibration`); with the factor 2 that falls to under 10% while every
2-sigma shift is still found. Existing signals are therefore fewer than in earlier versions.

**Stage 3 correlation.**
- *Basis:* month-on-month **changes** (first differences), 6 paired changes per window (7 months of
  levels). Levels are not used: trending series correlate spuriously and their p-values are
  over-confident (the simulation test shows ~4x the nominal false-positive rate).
- *Stored per row:* `p_value`, `n_obs`, `ci_low`/`ci_high` (95% Fisher-z interval), `q_value`
  (Benjamini-Hochberg) and `is_significant_fdr` (`q <= 0.05`), on `corr_signals` and
  `corr_signals_tier3a`.
- *Test families* (BH is only meaningful within one family; `corr/inference.py`): Tier 1/2 =
  tier x dataset pair x window-end month (windows overlap, so months are not pooled); Tier 3A =
  one family per metric (prescribing / dental) across all trusts and signal months. BH assumes
  independent or positively dependent tests; neighbouring trusts share ICBs, which is positive
  dependence.
- *Not done:* a block-permutation p-value. With 6 paired changes it adds little over the
  Pearson/Fisher result; the first-difference transform is what removes the trend problem.
- *Anchor:* Tier 3 fallback months now come from `MAX(month_date)` in `fact_ae_attendances`, not a
  hard-coded `2025-03-01`.

**Executive brief.** `make brief` (or `python reporting/generate_executive_brief.py`) writes
`docs/executive_findings.md`. Every figure is a query; it carries the as-of month, data window and
git commit (`-dirty` when the tree has uncommitted changes) and is byte-reproducible. The A&E
target table in the generator is checked against `sql/00_ae_targets.sql` by a test.
