# Power BI reporting

Lives on `feature/powerbi-reporting`; everything here is additive, so the Python
pipeline (`etl/`, `dq/`, `cp/`, `corr/`, `app/`) is unchanged and merges to `main` cleanly.

Planned layout:

- `*.pbip` / `*.Report/` / `*.SemanticModel/`: the report as a PBIP project (plain text, diffable).
  Do not commit `.pbix` files; they are gitignored.
- `theme.json`: report theme.
- `measures.dax`: DAX measures, kept in text for review.

Data flow: the pipeline writes `data/processed/nhs.duckdb`. Power BI has no native DuckDB
connector, so a future `export/` stage will write the star schema (dim_date, dim_organisation,
the fact tables and cp/corr outputs) to `data/exports/` as Parquet or CSV for Power BI to load.
`data/exports/` is gitignored.

## Reporting views

`make reporting` loads the tables into the `reporting` schema and rebuilds the views in `sql/`
(also runnable alone with `make views`). Import these into Power BI:

| View | Role |
|---|---|
| `vw_dim_date`, `vw_dim_trust` | dimensions (month, trust with region/ICB) |
| `vw_ae_performance`, `vw_rtt_performance`, `vw_rtt_specialty` | facts, trust x month (x specialty) |
| `vw_spc_xmr`, `vw_benchmark`, `vw_data_quality` | control charts, region/national benchmark, pipeline health |

`vw_rtt_performance` uses only the `C_999` total rows and `vw_rtt_specialty` only the specialty rows;
never add the two together. Trusts with no ICB are labelled `Unmapped`, never dropped.
