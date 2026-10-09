"""Copy the reporting tables from DuckDB into PostgreSQL (schema "reporting").

Full refresh in a single transaction: each table is dropped, recreated with explicit
types and keys, and reloaded with COPY. Source and target row counts are compared
before COMMIT; any mismatch raises and rolls the whole refresh back, so readers
(e.g. Power BI) see either the old data or the new data, never a half-loaded schema.

Connection settings come from the environment or a .env file in the repo root:
PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD (see .env.example).
"""
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb  # noqa: E402

SCHEMA = "reporting"
REQUIRED_ENV = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
REFERENCE_DIR = _ROOT / "corr" / "reference"

# Each spec: name, source SELECT (DuckDB), columns with PostgreSQL types, extra DDL
# constraints, and indexes. The column list is the load order and must match the SELECT.
TABLES = [
    {
        "name": "dim_organisation",
        "source": "SELECT org_code, org_name, org_type, parent_org_code, region_name, specialist_no_ae "
                  "FROM dim_organisation",
        "columns": [
            ("org_code", "text NOT NULL"), ("org_name", "text"), ("org_type", "text"),
            ("parent_org_code", "text"), ("region_name", "text"), ("specialist_no_ae", "boolean"),
        ],
        "constraints": ["PRIMARY KEY (org_code)"],
        "indexes": [],
    },
    {
        "name": "dim_date",
        "source": "SELECT CAST(date_key AS DATE) AS date_key, year, month, month_name, quarter, "
                  "financial_year FROM dim_date",
        "columns": [
            ("date_key", "date NOT NULL"), ("year", "integer"), ("month", "integer"),
            ("month_name", "text"), ("quarter", "text"), ("financial_year", "text"),
        ],
        "constraints": ["PRIMARY KEY (date_key)"],
        "indexes": [],
    },
    {
        "name": "fact_ae_attendances",
        "source": "SELECT month_date, org_code, org_name, parent_org, attendances_type1, attendances_type2, "
                  "attendances_other, total_attendances, breaches_4hr_type1, breaches_4hr_type2, "
                  "breaches_4hr_other, total_breaches_4hr, breach_pct, admissions_via_ae "
                  "FROM fact_ae_attendances",
        "columns": [
            ("month_date", "date NOT NULL"), ("org_code", "text NOT NULL"), ("org_name", "text"),
            ("parent_org", "text"), ("attendances_type1", "bigint"), ("attendances_type2", "bigint"),
            ("attendances_other", "bigint"), ("total_attendances", "bigint"),
            ("breaches_4hr_type1", "bigint"), ("breaches_4hr_type2", "bigint"),
            ("breaches_4hr_other", "bigint"), ("total_breaches_4hr", "bigint"),
            ("breach_pct", "double precision"), ("admissions_via_ae", "bigint"),
        ],
        "constraints": ["PRIMARY KEY (month_date, org_code)"],
        "indexes": [("org_code, month_date", "org_month")],
    },
    {
        "name": "fact_rtt_waits",
        "source": "SELECT month_date, org_code, org_name, region_code, specialty_code, specialty_name, "
                  "waiting_list_size, over_18wk_count, over_18wk_pct, median_wait_weeks, over_52wk_count "
                  "FROM fact_rtt_waits",
        "columns": [
            ("month_date", "date NOT NULL"), ("org_code", "text NOT NULL"), ("org_name", "text"),
            ("region_code", "text"), ("specialty_code", "text NOT NULL"), ("specialty_name", "text"),
            ("waiting_list_size", "bigint"), ("over_18wk_count", "bigint"),
            ("over_18wk_pct", "double precision"), ("median_wait_weeks", "double precision"),
            ("over_52wk_count", "bigint"),
        ],
        "constraints": ["PRIMARY KEY (month_date, org_code, specialty_code)"],
        "indexes": [("org_code, month_date", "org_month")],
    },
    {
        "name": "dq_flags",
        "source": "SELECT dataset, month_date, check_name, status, detail FROM dq_flags",
        "columns": [
            ("dataset", "text NOT NULL"), ("month_date", "date NOT NULL"),
            ("check_name", "text NOT NULL"), ("status", "text"), ("detail", "text"),
        ],
        "constraints": ["PRIMARY KEY (dataset, month_date, check_name)"],
        "indexes": [],
    },
    {
        "name": "cp_signals",
        "source": "SELECT dataset, org_code, month_date, method, signal_type, detail FROM cp_signals",
        "columns": [
            ("dataset", "text NOT NULL"), ("org_code", "text NOT NULL"), ("month_date", "date NOT NULL"),
            ("method", "text NOT NULL"), ("signal_type", "text NOT NULL"), ("detail", "text"),
        ],
        "constraints": ["PRIMARY KEY (dataset, org_code, month_date, method, signal_type)"],
        "indexes": [("org_code, month_date", "org_month")],
    },
]

# Reference CSVs are read with every column as text and cast by the DDL types.
CSV_TABLES = [
    {
        "name": "trust_to_icb",
        "csv": REFERENCE_DIR / "trust_to_icb.csv",
        "columns": [
            ("trust_code", "text NOT NULL"), ("icb_code", "text"),
            ("multi_icb", "boolean"), ("resolution_status", "text"),
        ],
        # A trust that maps to several ICBs has several rows, and unresolved trusts have no
        # icb_code, so (trust_code, icb_code) cannot be a PRIMARY KEY (NULL). Use a surrogate
        # key plus a uniqueness guarantee that treats a missing ICB as ''.
        "surrogate_key": True,
        "unique_expr": "trust_code, COALESCE(icb_code, '')",
        "indexes": [("trust_code", "trust")],
    },
    {
        # Every fact-table trust's ICB(s) as at its last reporting month (see
        # corr/reference/trust_icb_current.py); feeds reporting.vw_dim_trust.
        "name": "trust_icb_current",
        "csv": REFERENCE_DIR / "trust_icb_current.csv",
        "columns": [
            ("trust_code", "text NOT NULL"), ("as_of_month", "date"), ("icb_code", "text"),
            ("icb_name", "text"), ("n_icbs", "integer"), ("resolution_status", "text"),
        ],
        "surrogate_key": True,
        "unique_expr": "trust_code, COALESCE(icb_code, '')",
        "indexes": [("trust_code", "trust")],
    },
    {
        "name": "icb_to_region",
        "csv": REFERENCE_DIR / "icb_to_region.csv",
        "columns": [
            ("icb_code", "text NOT NULL"), ("icb_name", "text"),
            ("region_code", "text"), ("region_name", "text"),
        ],
        "constraints": ["PRIMARY KEY (icb_code)"],
        "indexes": [],
    },
]


def load_env(env_path: Path = _ROOT / ".env") -> dict:
    """Return PG* settings from the environment, falling back to the .env file."""
    if env_path.exists():
        from dotenv import dotenv_values

        file_values = dotenv_values(env_path)
    else:
        file_values = {}
    settings = {k: os.environ.get(k) or file_values.get(k) for k in REQUIRED_ENV}
    missing = [k for k, v in settings.items() if not v]
    if missing:
        raise RuntimeError(
            f"Missing PostgreSQL settings: {', '.join(missing)}. "
            "Set them in the environment or in .env (copy .env.example)."
        )
    return settings


def create_table_sql(spec: dict) -> str:
    cols = [f"{name} {typ}" for name, typ in spec["columns"]]
    if spec.get("surrogate_key"):
        cols.insert(0, "row_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY")
    cols += spec.get("constraints", [])
    body = ",\n    ".join(cols)
    return f"CREATE TABLE {SCHEMA}.{spec['name']} (\n    {body}\n)"


def index_sql(spec: dict) -> list[str]:
    stmts = [
        f"CREATE INDEX {spec['name']}_{suffix}_idx ON {SCHEMA}.{spec['name']} ({cols})"
        for cols, suffix in spec["indexes"]
    ]
    if spec.get("unique_expr"):
        stmts.append(
            f"CREATE UNIQUE INDEX {spec['name']}_unique_idx ON {SCHEMA}.{spec['name']} "
            f"({spec['unique_expr']})"
        )
    return stmts


def _export_csv(con, select_sql: str, out_path: Path) -> int:
    """Write a DuckDB query to a headerless CSV with \\N for NULL; return the row count."""
    con.execute(f"COPY ({select_sql}) TO '{out_path.as_posix()}' (FORMAT CSV, HEADER false, NULL '\\N')")
    return con.execute(f"SELECT COUNT(*) FROM ({select_sql})").fetchone()[0]


def _copy_in(cur, spec: dict, csv_path: Path):
    cols = ", ".join(name for name, _ in spec["columns"])
    with open(csv_path, encoding="utf-8", newline="") as fh:
        cur.copy_expert(
            f"COPY {SCHEMA}.{spec['name']} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '\\N')", fh
        )


def verify_counts(results: list[tuple[str, int, int]]):
    """Print source/target counts and raise if any table differs."""
    print(f"{'table':24s} {'source':>10s} {'target':>10s}")
    bad = []
    for name, src, tgt in results:
        mark = "" if src == tgt else "  <-- MISMATCH"
        print(f"{name:24s} {src:>10d} {tgt:>10d}{mark}")
        if src != tgt:
            bad.append(name)
    if bad:
        raise RuntimeError(f"Row count mismatch for: {', '.join(bad)}")


def load_reporting(db_path: str = "data/processed/nhs.duckdb", settings: dict | None = None):
    import psycopg2

    settings = settings or load_env()
    src = duckdb.connect(db_path, read_only=True)
    pg = psycopg2.connect(
        host=settings["PGHOST"], port=settings["PGPORT"], dbname=settings["PGDATABASE"],
        user=settings["PGUSER"], password=settings["PGPASSWORD"],
    )
    try:
        with tempfile.TemporaryDirectory() as tmp, pg:  # `with pg` = one transaction
            cur = pg.cursor()
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
            results = []
            for spec in TABLES + CSV_TABLES:
                name = spec["name"]
                if "csv" in spec:
                    select = (f"SELECT * FROM read_csv('{spec['csv'].as_posix()}', header=true, "
                              "all_varchar=true)")
                else:
                    select = spec["source"]
                out = Path(tmp) / f"{name}.csv"
                src_count = _export_csv(src, select, out)

                # CASCADE drops the sql/ views that depend on the table; they are rebuilt below.
                cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{name} CASCADE")
                cur.execute(create_table_sql(spec))
                _copy_in(cur, spec, out)
                for stmt in index_sql(spec):
                    cur.execute(stmt)
                cur.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{name}")
                results.append((name, src_count, cur.fetchone()[0]))
            verify_counts(results)  # raising here leaves `with pg` to roll back
            from reporting.apply_views import apply_views  # same transaction: never a schema without views

            apply_views(pg)
            print("[OK] Re-applied the sql/ views")
        print(f"[OK] Refreshed schema '{SCHEMA}' ({len(results)} tables)")
    finally:
        pg.close()
        src.close()


if __name__ == "__main__":
    load_reporting(sys.argv[1] if len(sys.argv) > 1 else "data/processed/nhs.duckdb")
