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
