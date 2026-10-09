r"""Apply sql/run_all.sql to PostgreSQL without needing the psql client.

run_all.sql is a psql script made of "\ir <file>" lines; this runs those files in order
inside one transaction, so a failing view leaves the previous set of views untouched.
Settings come from .env exactly as in reporting/load_postgres.py.
"""
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from reporting.load_postgres import SCHEMA, load_env  # noqa: E402

SQL_DIR = _ROOT / "sql"


def view_files(run_all: Path = SQL_DIR / "run_all.sql") -> list[Path]:
    """The .sql files named by `\\ir` lines in run_all.sql, in order."""
    return [run_all.parent / m.group(1) for m in re.finditer(r"^\\ir\s+(\S+)", run_all.read_text(), re.M)]


def apply_views(conn, schema: str = SCHEMA):
    """Execute every view file on `conn`; `schema` lets tests build into a scratch schema."""
    with conn.cursor() as cur:
        for path in view_files():
            sql = path.read_text(encoding="utf-8")
            if schema != SCHEMA:
                sql = sql.replace(f"{SCHEMA}.", f"{schema}.")
            cur.execute(sql)


def main():
    import psycopg2

    s = load_env()
    conn = psycopg2.connect(host=s["PGHOST"], port=s["PGPORT"], dbname=s["PGDATABASE"],
                            user=s["PGUSER"], password=s["PGPASSWORD"])
    try:
        with conn:  # one transaction
            apply_views(conn)
        print(f"[OK] Applied {len(view_files())} view files to schema '{SCHEMA}'")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
