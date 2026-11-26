# Power BI Reporting Layer

Connects Power BI directly to the PostgreSQL `reporting` star schema built from the DuckDB pipeline, providing executive KPI scorecards, regional/national benchmarking, NHS *Making Data Count* XmR control charts, and RTT specialty backlog drill-downs.

## Contents

- [measures.dax](file:///e:/Coding%20Projects/NHS/powerbi/measures.dax): Production DAX measure library (~30 measures across 6 display folders) using volume-weighted ratios (`DIVIDE(SUM(...), SUM(...))`) and dynamic `vw_dim_date` time intelligence (`DATESINPERIOD`, `SAMEPERIODLASTYEAR`, `DATESYTD`, `RANKX`).
- [nhs_theme.json](file:///e:/Coding%20Projects/NHS/powerbi/nhs_theme.json): Official NHS Blue (`#005EB8` / `#003087`) report theme with accessible RAG status colours.
- [DATA_DICTIONARY.md](file:///e:/Coding%20Projects/NHS/powerbi/DATA_DICTIONARY.md): Star-schema Mermaid ERD, view grain contracts, `C_999` non-additivity rules, Row-Level Security (RLS) roles, and `.pbip` (TMDL) source-control guide.
- `*.pbip` / `*.Report/` / `*.SemanticModel/`: Diffable Power BI Project format (binary `*.pbix` files and `.pbi/cache.abf` are gitignored).

## End-to-End Refresh

1. Copy `.env.example` to `.env` with your PostgreSQL credentials.
2. Load DuckDB tables into PostgreSQL (`reporting.*`) and build all reporting views atomically:
   ```bash
   make reporting
   ```
   *(Or run `python reporting/load_postgres.py` — views in `sql/` are re-applied in the same transaction; `make views` refreshes views alone.)*
3. In Power BI Desktop, connect via **PostgreSQL database** (`localhost:5432`, database `nhs`) and import the views in `reporting`:

| View | Role |
| :--- | :--- |
| `vw_dim_date`, `vw_dim_trust` | Conformed date (April–March NHS financial year) and provider/ICB/region dimensions |
| `vw_ae_performance`, `vw_rtt_performance` | Provider × month facts (`vw_rtt_performance` uses `C_999` provider totals only) |
| `vw_rtt_specialty` | Provider × treatment function × month (`C_999` excluded — never sum with `vw_rtt_performance`) |
| `vw_spc_xmr`, `vw_benchmark`, `vw_data_quality` | XmR control charts, volume-weighted regional/national benchmarks & ranks, and DQ health |
| `vw_changepoints` | SPC breaches and PELT level shifts for A&E and RTT, on raw and peer-relative (trust minus national) series. `is_robust` = the peer series also shifts within a month |
| `vw_correlations` | A&E vs RTT / dental / prescribing associations on month-on-month changes, with r, 95% interval, p, Benjamini-Hochberg `q_value` and `finding_category` (`Significant after FDR` etc.). Every row is exploratory (n = 6); association, not cause |
| `vw_primary_care_icb` | ICB x month prescribing and dental UDA context (sources end in different months; NULL is not zero) |
| `vw_pipeline_health` | Pipeline run history with `latest_run`, `is_failure` and `data_loaded_through` for a "data as of" banner |

`vw_ae_targets` (A&E target by financial year) feeds `vw_ae_performance`, which also carries `admissions`, `admission_conversion_pct` and `excess_breaches_vs_target`. It need not be imported.

See [DATA_DICTIONARY.md](file:///e:/Coding%20Projects/NHS/powerbi/DATA_DICTIONARY.md) for relationship setup, DAX import via TMDL View, and RLS role definitions.
