"""Query functions backing dashboard.py — deliberately Streamlit-free so they
can be imported and tested directly (see tests/test_dashboard.py) without
pulling in a Streamlit script-run context.

Each function takes an open DuckDB connection and returns a plain
pandas.DataFrame. Caching and connection lifecycle live in dashboard.py.
"""
import duckdb
import pandas as pd

DB_PATH = "data/processed/nhs.duckdb"


def get_connection(db_path: str = DB_PATH, read_only: bool = True) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(db_path, read_only=read_only)


# --- Shared (sidebar) ---------------------------------------------------------

def get_trust_list(conn) -> pd.DataFrame:
    """All trusts for the sidebar selector."""
    return conn.execute("""
        SELECT DISTINCT org_code, org_name
        FROM dim_organisation
        WHERE org_type = 'trust'
        ORDER BY org_code
    """).df()


# --- Stage 1: Data Quality -----------------------------------------------------

def get_dq_flags(conn) -> pd.DataFrame:
    return conn.execute("""
        SELECT * FROM dq_flags
        ORDER BY month_date DESC, dataset, check_name
    """).df()


def get_dq_volume_trends(conn) -> pd.DataFrame:
    """Monthly record counts per dataset, for the ingestion-consistency chart."""
    return conn.execute("""
        SELECT month_date, 'A&E' AS dataset, COUNT(*) AS record_count
            FROM fact_ae_attendances GROUP BY month_date
        UNION ALL
        SELECT month_date, 'RTT' AS dataset, COUNT(*) AS record_count FROM fact_rtt_waits GROUP BY month_date
        UNION ALL
        SELECT month_date, 'Prescribing' AS dataset, COUNT(*) AS record_count
            FROM fact_prescribing GROUP BY month_date
        UNION ALL
        SELECT month_date, 'Dental' AS dataset, COUNT(*) AS record_count
            FROM fact_dental_uda GROUP BY month_date
        ORDER BY month_date, dataset
    """).df()


# --- Stage 2: Changepoints -------------------------------------------------------

def get_cp_signals(conn, trust_code: str) -> pd.DataFrame:
    return conn.execute("""
        SELECT dataset, month_date, method, signal_type, detail
        FROM cp_signals
        WHERE org_code = ?
        ORDER BY month_date, method
    """, [trust_code]).df()


def get_ae_timeseries(conn, trust_code: str) -> pd.DataFrame:
    return conn.execute("""
        SELECT month_date, breach_pct, total_attendances, total_breaches_4hr
        FROM fact_ae_attendances
        WHERE org_code = ?
        ORDER BY month_date
    """, [trust_code]).df()


def get_rtt_timeseries(conn, trust_code: str) -> pd.DataFrame:
    return conn.execute("""
        SELECT month_date, over_18wk_pct, waiting_list_size
        FROM fact_rtt_waits
        WHERE org_code = ? AND specialty_code = 'C_999'
        ORDER BY month_date
    """, [trust_code]).df()


# --- Stage 3: Correlations -------------------------------------------------------

def get_tier1_correlations(conn) -> pd.DataFrame:
    return conn.execute("SELECT * FROM corr_signals_tier1 ORDER BY month_date, geography_code").df()


def get_tier2_correlations(conn, trust_code: str) -> pd.DataFrame:
    return conn.execute("""
        SELECT * FROM corr_signals
        WHERE tier = 'tier2_trust' AND geography_code = ?
        ORDER BY month_date
    """, [trust_code]).df()


def get_tier3a_findings(conn, trust_code: str) -> pd.DataFrame:
    return conn.execute("SELECT * FROM corr_signals_tier3a WHERE trust_code = ?", [trust_code]).df()


def get_tier3b_findings(conn, trust_code: str) -> pd.DataFrame:
    return conn.execute("SELECT * FROM corr_signals_tier3b WHERE trust_code = ?", [trust_code]).df()


def get_tier3a_national_summary(conn) -> pd.DataFrame:
    """All trusts with a resolved-ICB, correlated (non-'no_correlated_shift') Tier 3a finding."""
    return conn.execute("""
        SELECT * FROM corr_signals_tier3a
        WHERE icb_code != 'UNRESOLVED' AND finding NOT LIKE '%no_correlated_shift%'
    """).df()
