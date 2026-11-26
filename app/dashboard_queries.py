"""Query functions backing app/dashboard.py — deliberately Streamlit-free so they
can be imported and tested directly (see tests/test_dashboard.py) without
pulling in a Streamlit script-run context.

Each function takes an open DuckDB connection and returns a plain
pandas.DataFrame. Caching and connection lifecycle live in dashboard.py.
"""
import os
from pathlib import Path

import duckdb
import pandas as pd

# Anchored to the repo root (app/ is one level down) so it resolves from any cwd.
# NHS_DB_PATH lets the dashboard read another DuckDB file (e.g. the synthetic demo fixture:
# `make demo`) without editing code.
DB_PATH = os.environ.get("NHS_DB_PATH") or str(
    Path(__file__).resolve().parents[1] / "data" / "processed" / "nhs.duckdb")


def get_connection(db_path: str = DB_PATH, read_only: bool = True) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(db_path, read_only=read_only)


# --- Shared (sidebar) ---------------------------------------------------------

_REGION_CASE_SQL = """
    CASE UPPER(TRIM(COALESCE(region_raw, '')))
        WHEN 'NHS ENGLAND LONDON' THEN 'London'
        WHEN 'Y56' THEN 'London'
        WHEN 'NHS ENGLAND SOUTH WEST' THEN 'South West'
        WHEN 'Y58' THEN 'South West'
        WHEN 'NHS ENGLAND SOUTH EAST' THEN 'South East'
        WHEN 'Y59' THEN 'South East'
        WHEN 'NHS ENGLAND MIDLANDS' THEN 'Midlands'
        WHEN 'Y60' THEN 'Midlands'
        WHEN 'NHS ENGLAND EAST OF ENGLAND' THEN 'East of England'
        WHEN 'Y61' THEN 'East of England'
        WHEN 'NHS ENGLAND NORTH WEST' THEN 'North West'
        WHEN 'Y62' THEN 'North West'
        WHEN 'NHS ENGLAND NORTH EAST AND YORKSHIRE' THEN 'North East and Yorkshire'
        WHEN 'Y63' THEN 'North East and Yorkshire'
        ELSE 'Unknown'
    END
"""


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [table_name],
    ).fetchone()
    return bool(row and row[0] > 0)


def get_trust_list(conn) -> pd.DataFrame:
    """All trusts for the sidebar selector."""
    return conn.execute("""
        SELECT DISTINCT org_code, org_name
        FROM dim_organisation
        WHERE org_type = 'trust'
        ORDER BY org_code
    """).df()


# --- Executive Overview -------------------------------------------------------

def get_national_monthly_summary(conn) -> pd.DataFrame:
    """Monthly England headline KPIs across A&E (all types + Type 1 + admissions)
    and RTT incomplete pathways (C_999 provider totals only — never specialty rows).
    """
    return conn.execute("""
        WITH standards (fy_start_year, target_pct) AS (
            VALUES (2023, 76.0), (2024, 78.0), (2025, 78.0), (2026, 82.0)
        ),
        ae AS (
            SELECT
                month_date,
                CAST(SUM(total_attendances) AS BIGINT) AS total_attendances,
                CAST(SUM(total_breaches_4hr) AS BIGINT) AS total_breaches_4hr,
                CAST(SUM(total_attendances - total_breaches_4hr) AS BIGINT) AS within_4hr,
                CAST(SUM(attendances_type1) AS BIGINT) AS type1_attendances,
                CAST(SUM(breaches_4hr_type1) AS BIGINT) AS type1_breaches_4hr,
                CAST(SUM(COALESCE(admissions_via_ae, 0)) AS BIGINT) AS admissions_via_ae
            FROM fact_ae_attendances
            GROUP BY month_date
        ),
        rtt AS (
            SELECT
                month_date,
                CAST(SUM(waiting_list_size) AS BIGINT) AS waiting_list_size,
                CAST(SUM(waiting_list_size - over_18wk_count) AS BIGINT) AS within_18wk,
                CAST(SUM(over_18wk_count) AS BIGINT) AS over_18wk_count,
                CAST(SUM(COALESCE(over_52wk_count, 0)) AS BIGINT) AS over_52wk_count
            FROM fact_rtt_waits
            WHERE specialty_code = 'C_999'
            GROUP BY month_date
        ),
        joined AS (
            SELECT
                COALESCE(ae.month_date, rtt.month_date) AS month_date,
                ae.total_attendances,
                ae.total_breaches_4hr,
                ae.within_4hr,
                ae.type1_attendances,
                ae.type1_breaches_4hr,
                ae.admissions_via_ae,
                rtt.waiting_list_size,
                rtt.within_18wk,
                rtt.over_18wk_count,
                rtt.over_52wk_count,
                CASE
                    WHEN EXTRACT(MONTH FROM COALESCE(ae.month_date, rtt.month_date)) >= 4
                    THEN CAST(EXTRACT(YEAR FROM COALESCE(ae.month_date, rtt.month_date)) AS INT)
                    ELSE CAST(EXTRACT(YEAR FROM COALESCE(ae.month_date, rtt.month_date)) AS INT) - 1
                END AS fy_start_year
            FROM ae
            FULL OUTER JOIN rtt ON ae.month_date = rtt.month_date
        )
        SELECT
            j.month_date,
            j.total_attendances,
            j.total_breaches_4hr,
            j.within_4hr,
            CASE WHEN j.total_attendances > 0
                 THEN 100.0 * j.within_4hr / j.total_attendances END AS ae_4hr_pct,
            j.type1_attendances,
            j.type1_breaches_4hr,
            CASE WHEN j.type1_attendances > 0
                 THEN 100.0 * (j.type1_attendances - j.type1_breaches_4hr) / j.type1_attendances
                 END AS ae_type1_4hr_pct,
            j.admissions_via_ae,
            CASE WHEN j.total_attendances > 0
                 THEN 100.0 * j.admissions_via_ae / j.total_attendances
                 END AS admission_conversion_pct,
            COALESCE(s.target_pct, 78.0) AS ae_target_pct,
            CASE WHEN j.total_attendances > 0 THEN
                GREATEST(
                    CAST(0 AS BIGINT),
                    j.total_breaches_4hr - CAST(
                        ROUND(j.total_attendances * (1.0 - COALESCE(s.target_pct, 78.0) / 100.0))
                        AS BIGINT
                    )
                )
            END AS excess_breaches_vs_target,
            j.waiting_list_size,
            j.within_18wk,
            j.over_18wk_count,
            CASE WHEN j.waiting_list_size > 0
                 THEN 100.0 * j.within_18wk / j.waiting_list_size END AS rtt_18wk_pct,
            j.over_52wk_count
        FROM joined j
        LEFT JOIN standards s ON s.fy_start_year = j.fy_start_year
        ORDER BY j.month_date
    """).df()


def get_trust_benchmark_league(conn, month_date=None) -> pd.DataFrame:
    """Trust-level operational league table for `month_date` (defaults to latest A&E month).

    Combines A&E 4-hour performance (all types + Type 1), excess breaches vs operational
    target, YoY delta, emergency admission conversion %, RTT C_999 waiting list metrics,
    national rank among NHS Type 1 trusts, and total Stage 2 signal count.
    """
    if month_date is None:
        row = conn.execute("SELECT MAX(month_date) FROM fact_ae_attendances").fetchone()
        month_date = row[0] if row else None
    if month_date is None:
        return pd.DataFrame(columns=[
            "org_code", "org_name", "region_name", "provider_type", "has_type1_ae",
            "attendances", "breaches_4hr", "ae_4hr_pct", "ae_type1_4hr_pct", "ae_yoy_pp",
            "excess_breaches_vs_target", "admission_conversion_pct", "waiting_list_size",
            "rtt_18wk_pct", "waiters_52wk_plus", "ae_national_rank", "active_signals",
        ])

    cp_cte = (
        "SELECT org_code, CAST(COUNT(*) AS BIGINT) AS active_signals FROM cp_signals "
        "WHERE dataset = 'ae_attendances' GROUP BY org_code"
        if _table_exists(conn, "cp_signals")
        else "SELECT CAST(NULL AS VARCHAR) AS org_code, CAST(0 AS BIGINT) AS active_signals WHERE 1=0"
    )

    sql = f"""
        WITH standards (fy_start_year, target_pct) AS (
            VALUES (2023, 76.0), (2024, 78.0), (2025, 78.0), (2026, 82.0)
        ),
        target_month AS (
            SELECT
                CAST(? AS DATE) AS m_date,
                CASE WHEN EXTRACT(MONTH FROM CAST(? AS DATE)) >= 4
                     THEN CAST(EXTRACT(YEAR FROM CAST(? AS DATE)) AS INT)
                     ELSE CAST(EXTRACT(YEAR FROM CAST(? AS DATE)) AS INT) - 1
                END AS fy_start_year
        ),
        ae_cur AS (
            SELECT
                org_code,
                org_name,
                parent_org AS region_raw,
                total_attendances AS attendances,
                total_breaches_4hr AS breaches_4hr,
                attendances_type1,
                breaches_4hr_type1,
                COALESCE(admissions_via_ae, 0) AS admissions_via_ae,
                CASE WHEN total_attendances > 0
                     THEN 100.0 * (total_attendances - total_breaches_4hr) / total_attendances
                     END AS ae_4hr_pct,
                CASE WHEN attendances_type1 > 0
                     THEN 100.0 * (attendances_type1 - breaches_4hr_type1) / attendances_type1
                     END AS ae_type1_4hr_pct
            FROM fact_ae_attendances
            WHERE month_date = (SELECT m_date FROM target_month)
        ),
        ae_prev AS (
            SELECT
                org_code,
                CASE WHEN total_attendances > 0
                     THEN 100.0 * (total_attendances - total_breaches_4hr) / total_attendances
                     END AS prev_ae_4hr_pct
            FROM fact_ae_attendances
            WHERE month_date = (SELECT CAST(m_date - INTERVAL '1 year' AS DATE) FROM target_month)
        ),
        rtt_cur AS (
            SELECT
                org_code,
                org_name,
                region_code AS region_raw,
                waiting_list_size,
                CASE WHEN waiting_list_size > 0
                     THEN 100.0 * (waiting_list_size - over_18wk_count) / waiting_list_size
                     END AS rtt_18wk_pct,
                COALESCE(over_52wk_count, 0) AS waiters_52wk_plus
            FROM fact_rtt_waits
            WHERE month_date = (SELECT m_date FROM target_month)
              AND specialty_code = 'C_999'
        ),
        cp_cnt AS ({cp_cte}),
        combined AS (
            SELECT
                COALESCE(a.org_code, r.org_code) AS org_code,
                COALESCE(a.org_name, r.org_name) AS org_name,
                COALESCE(a.region_raw, r.region_raw) AS region_raw,
                CASE WHEN regexp_matches(COALESCE(a.org_code, r.org_code), '^R[A-Z0-9]{{2}}$')
                     THEN 'NHS trust' ELSE 'Other provider' END AS provider_type,
                COALESCE(a.attendances_type1, 0) > 0 AS has_type1_ae,
                a.attendances,
                a.breaches_4hr,
                a.ae_4hr_pct,
                a.ae_type1_4hr_pct,
                a.ae_4hr_pct - p.prev_ae_4hr_pct AS ae_yoy_pp,
                CASE WHEN a.attendances > 0 THEN
                    GREATEST(
                        CAST(0 AS BIGINT),
                        a.breaches_4hr - CAST(
                            ROUND(a.attendances * (1.0 - COALESCE(s.target_pct, 78.0) / 100.0))
                            AS BIGINT
                        )
                    )
                END AS excess_breaches_vs_target,
                CASE WHEN a.attendances > 0
                     THEN 100.0 * a.admissions_via_ae / a.attendances
                     END AS admission_conversion_pct,
                r.waiting_list_size,
                r.rtt_18wk_pct,
                r.waiters_52wk_plus,
                COALESCE(c.active_signals, 0) AS active_signals
            FROM ae_cur a
            FULL OUTER JOIN rtt_cur r ON a.org_code = r.org_code
            LEFT JOIN ae_prev p ON COALESCE(a.org_code, r.org_code) = p.org_code
            CROSS JOIN target_month tm
            LEFT JOIN standards s ON s.fy_start_year = tm.fy_start_year
            LEFT JOIN cp_cnt c ON COALESCE(a.org_code, r.org_code) = c.org_code
        ),
        mapped AS (
            SELECT
                org_code,
                org_name,
                {_REGION_CASE_SQL} AS region_name,
                provider_type,
                has_type1_ae,
                attendances,
                breaches_4hr,
                ae_4hr_pct,
                ae_type1_4hr_pct,
                ae_yoy_pp,
                excess_breaches_vs_target,
                admission_conversion_pct,
                waiting_list_size,
                rtt_18wk_pct,
                waiters_52wk_plus,
                active_signals,
                (provider_type = 'NHS trust' AND has_type1_ae AND ae_4hr_pct IS NOT NULL) AS rankable
            FROM combined
        )
        SELECT
            org_code,
            org_name,
            region_name,
            provider_type,
            has_type1_ae,
            attendances,
            breaches_4hr,
            ae_4hr_pct,
            ae_type1_4hr_pct,
            ae_yoy_pp,
            excess_breaches_vs_target,
            admission_conversion_pct,
            waiting_list_size,
            rtt_18wk_pct,
            waiters_52wk_plus,
            CASE WHEN rankable
                 THEN RANK() OVER (PARTITION BY rankable ORDER BY ae_4hr_pct DESC)
                 END AS ae_national_rank,
            active_signals
        FROM mapped
        ORDER BY COALESCE(excess_breaches_vs_target, 0) DESC, org_code
    """
    return conn.execute(sql, [month_date, month_date, month_date, month_date]).df()


def get_regional_summary(conn, month_date=None) -> pd.DataFrame:
    """Weighted NHS England regional benchmark for `month_date` (defaults to latest A&E month)."""
    if month_date is None:
        row = conn.execute("SELECT MAX(month_date) FROM fact_ae_attendances").fetchone()
        month_date = row[0] if row else None
    if month_date is None:
        return pd.DataFrame(columns=[
            "region_name", "trust_count", "attendances", "ae_4hr_pct", "ae_type1_4hr_pct",
            "excess_breaches_vs_target", "waiting_list_size", "rtt_18wk_pct", "waiters_52wk_plus",
        ])

    sql = f"""
        WITH standards (fy_start_year, target_pct) AS (
            VALUES (2023, 76.0), (2024, 78.0), (2025, 78.0), (2026, 82.0)
        ),
        target_month AS (
            SELECT
                CAST(? AS DATE) AS m_date,
                CASE WHEN EXTRACT(MONTH FROM CAST(? AS DATE)) >= 4
                     THEN CAST(EXTRACT(YEAR FROM CAST(? AS DATE)) AS INT)
                     ELSE CAST(EXTRACT(YEAR FROM CAST(? AS DATE)) AS INT) - 1
                END AS fy_start_year
        ),
        ae_reg AS (
            SELECT
                {_REGION_CASE_SQL} AS region_name,
                COUNT(DISTINCT org_code) AS ae_orgs,
                CAST(SUM(total_attendances) AS BIGINT) AS attendances,
                CAST(SUM(total_breaches_4hr) AS BIGINT) AS breaches_4hr,
                CAST(SUM(attendances_type1) AS BIGINT) AS type1_attendances,
                CAST(SUM(breaches_4hr_type1) AS BIGINT) AS type1_breaches_4hr
            FROM (
                SELECT org_code, parent_org AS region_raw, total_attendances,
                       total_breaches_4hr, attendances_type1, breaches_4hr_type1
                FROM fact_ae_attendances
                WHERE month_date = (SELECT m_date FROM target_month)
            ) a
            GROUP BY 1
        ),
        rtt_reg AS (
            SELECT
                {_REGION_CASE_SQL} AS region_name,
                COUNT(DISTINCT org_code) AS rtt_orgs,
                CAST(SUM(waiting_list_size) AS BIGINT) AS waiting_list_size,
                CAST(SUM(waiting_list_size - over_18wk_count) AS BIGINT) AS within_18wk,
                CAST(SUM(COALESCE(over_52wk_count, 0)) AS BIGINT) AS waiters_52wk_plus
            FROM (
                SELECT org_code, region_code AS region_raw, waiting_list_size,
                       over_18wk_count, over_52wk_count
                FROM fact_rtt_waits
                WHERE month_date = (SELECT m_date FROM target_month)
                  AND specialty_code = 'C_999'
            ) r
            GROUP BY 1
        )
        SELECT
            COALESCE(a.region_name, r.region_name) AS region_name,
            GREATEST(COALESCE(a.ae_orgs, 0), COALESCE(r.rtt_orgs, 0)) AS trust_count,
            a.attendances,
            CASE WHEN a.attendances > 0
                 THEN 100.0 * (a.attendances - a.breaches_4hr) / a.attendances
                 END AS ae_4hr_pct,
            CASE WHEN a.type1_attendances > 0
                 THEN 100.0 * (a.type1_attendances - a.type1_breaches_4hr) / a.type1_attendances
                 END AS ae_type1_4hr_pct,
            CASE WHEN a.attendances > 0 THEN
                GREATEST(
                    CAST(0 AS BIGINT),
                    a.breaches_4hr - CAST(
                        ROUND(a.attendances * (1.0 - COALESCE(s.target_pct, 78.0) / 100.0))
                        AS BIGINT
                    )
                )
            END AS excess_breaches_vs_target,
            r.waiting_list_size,
            CASE WHEN r.waiting_list_size > 0
                 THEN 100.0 * r.within_18wk / r.waiting_list_size
                 END AS rtt_18wk_pct,
            r.waiters_52wk_plus
        FROM ae_reg a
        FULL OUTER JOIN rtt_reg r ON a.region_name = r.region_name
        CROSS JOIN target_month tm
        LEFT JOIN standards s ON s.fy_start_year = tm.fy_start_year
        WHERE COALESCE(a.region_name, r.region_name) <> 'Unknown'
        ORDER BY ae_4hr_pct ASC NULLS LAST, region_name
    """
    return conn.execute(sql, [month_date, month_date, month_date, month_date]).df()


def get_rtt_specialty_backlog(conn, month_date=None) -> pd.DataFrame:
    """National RTT backlog by treatment function (`specialty_code <> 'C_999'`) for `month_date`.

    Excludes C_999 provider-total rows so specialty pathways never double-count.
    """
    if month_date is None:
        row = conn.execute("SELECT MAX(month_date) FROM fact_rtt_waits").fetchone()
        month_date = row[0] if row else None
    if month_date is None:
        return pd.DataFrame(columns=[
            "specialty_code", "specialty_name", "waiting_list_size", "over_18wk_count",
            "pct_within_18wk", "waiters_52wk_plus", "share_of_over18wk_pct", "share_of_52wk_pct",
        ])

    return conn.execute("""
        WITH spec AS (
            SELECT
                specialty_code,
                MIN(specialty_name) AS specialty_name,
                CAST(SUM(waiting_list_size) AS BIGINT) AS waiting_list_size,
                CAST(SUM(over_18wk_count) AS BIGINT) AS over_18wk_count,
                CAST(SUM(waiting_list_size - over_18wk_count) AS BIGINT) AS within_18wk,
                CAST(SUM(COALESCE(over_52wk_count, 0)) AS BIGINT) AS waiters_52wk_plus
            FROM fact_rtt_waits
            WHERE month_date = CAST(? AS DATE)
              AND specialty_code <> 'C_999'
            GROUP BY specialty_code
        )
        SELECT
            specialty_code,
            specialty_name,
            waiting_list_size,
            over_18wk_count,
            CASE WHEN waiting_list_size > 0
                 THEN 100.0 * within_18wk / waiting_list_size
                 END AS pct_within_18wk,
            waiters_52wk_plus,
            CASE WHEN SUM(over_18wk_count) OVER () > 0
                 THEN 100.0 * over_18wk_count / SUM(over_18wk_count) OVER ()
                 ELSE 0.0 END AS share_of_over18wk_pct,
            CASE WHEN SUM(waiters_52wk_plus) OVER () > 0
                 THEN 100.0 * waiters_52wk_plus / SUM(waiters_52wk_plus) OVER ()
                 ELSE 0.0 END AS share_of_52wk_pct
        FROM spec
        WHERE waiting_list_size > 0
        ORDER BY over_18wk_count DESC, waiting_list_size DESC
    """, [month_date]).df()


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
