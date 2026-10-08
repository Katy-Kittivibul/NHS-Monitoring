import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

# `streamlit run app/dashboard.py` only puts app/ on sys.path; add the repo root
# so the `app` package imports resolve.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import dashboard_queries as q  # noqa: E402

# Set page config
st.set_page_config(
    page_title="NHS Monitoring Copilot Dashboard",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded"
)

DB_PATH = q.DB_PATH

if not Path(DB_PATH).exists():
    st.error(
        f"⚠️ Database not found at `{DB_PATH}`.\n\n"
        "Run the pipeline first: `python etl/run_all.py`, then `python cp/run_all.py`, "
        "then `python corr/run_all.py` and `python corr/tier3_signal_driven.py`."
    )
    st.stop()


# ==============================================================================
# CACHED DATA ACCESS
# ==============================================================================
# get_db_connection is cache_resource'd (one shared connection, not reopened per
# query); each load_* wrapper below is cache_data'd (the query result itself).
# Errors are handled at the call site via safe_load, not inside the cached
# functions — a cached function's st.error() side effect wouldn't replay on a
# cache hit, so the try/except has to live outside the cache boundary instead.

@st.cache_resource
def get_db_connection():
    return q.get_connection(DB_PATH, read_only=True)


def safe_load(loader_fn, *args, context: str = "data") -> pd.DataFrame:
    """Call a cached loader; on failure, show an inline error and return an
    empty DataFrame instead of crashing the page."""
    try:
        return loader_fn(*args)
    except duckdb.Error as e:
        st.error(f"⚠️ Couldn't load {context}: {e}")
        return pd.DataFrame()
    except Exception as e:
        st.error(f"⚠️ Unexpected error loading {context}: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_trust_list():
    return q.get_trust_list(get_db_connection())


@st.cache_data(ttl=60)
def load_dq_flags():
    return q.get_dq_flags(get_db_connection())


@st.cache_data(ttl=60)
def load_dq_volume_trends():
    return q.get_dq_volume_trends(get_db_connection())


@st.cache_data(ttl=60)
def load_cp_signals(trust_code):
    return q.get_cp_signals(get_db_connection(), trust_code)


@st.cache_data(ttl=60)
def load_ae_timeseries(trust_code):
    return q.get_ae_timeseries(get_db_connection(), trust_code)


@st.cache_data(ttl=60)
def load_rtt_timeseries(trust_code):
    return q.get_rtt_timeseries(get_db_connection(), trust_code)


@st.cache_data(ttl=60)
def load_tier1_correlations():
    return q.get_tier1_correlations(get_db_connection())


@st.cache_data(ttl=60)
def load_tier2_correlations(trust_code):
    return q.get_tier2_correlations(get_db_connection(), trust_code)


@st.cache_data(ttl=60)
def load_tier3a_findings(trust_code):
    return q.get_tier3a_findings(get_db_connection(), trust_code)


@st.cache_data(ttl=60)
def load_tier3b_findings(trust_code):
    return q.get_tier3b_findings(get_db_connection(), trust_code)


@st.cache_data(ttl=60)
def load_tier3a_national_summary():
    return q.get_tier3a_national_summary(get_db_connection())


# ==============================================================================
# SHARED SIDEBAR — trust selector + glossary (rendered on every page)
# ==============================================================================
st.sidebar.title("🏥 NHS Copilot")
st.sidebar.markdown("---")

df_trusts = safe_load(load_trust_list, context="trust list")

if df_trusts.empty:
    st.sidebar.warning("No trusts found in dim_organisation.")
    trust_options = {}
    selected_trust = None
    selected_trust_name = None
else:
    trust_options = {
        f"{row['org_code']} - {row['org_name']}": row['org_code'] for _, row in df_trusts.iterrows()
    }
    default_index = list(trust_options.values()).index("RXC") if "RXC" in trust_options.values() else 0

    selected_trust_label = st.sidebar.selectbox(
        "Select NHS Trust for Analysis:",
        options=list(trust_options.keys()),
        index=default_index
    )
    selected_trust = trust_options[selected_trust_label]
    selected_trust_name = (
        selected_trust_label.split(" - ", 1)[1] if " - " in selected_trust_label else selected_trust
    )

    st.sidebar.info(f"Active Trust: **{selected_trust}**\n\n{selected_trust_name}")

with st.sidebar.expander("📖 Glossary & Acronyms", expanded=False):
    st.markdown("""
    *   **A&E**: Accident & Emergency (Emergency Departments).
    *   **RTT**: Referral to Treatment (elective waiting lists; target $<18$ weeks).
    *   **ICB**: Integrated Care Board (regional NHS healthcare governing body).
    *   **BNF**: British National Formulary (GP prescribing classification).
    *   **UDA**: Units of Dental Activity (NHS dental contract delivery metric).
    *   **ODS Code**: Organisation Data Service unique ID (e.g. `RXC`, `R0A`).
    *   **🚩 PELT Shift**: Pruned Exact Linear Time algorithm — flags structural step-changes in mean
        performance level.
    *   **⚠️ 3-Sigma Outlier**: Statistical Process Control (XmR Rule 1) — single-month point beyond 3
        standard deviations from mean.
    *   **🔄 Run of 7**: Statistical Process Control (XmR Rule 2) — 7 consecutive months falling on the same
        side of the mean.
    """)


# ==============================================================================
# STAGE 1: DATA QUALITY DASHBOARD
# ==============================================================================
def page_stage1():
    st.title("🛡️ Stage 1: Data Quality & Schema Integrity")
    st.markdown(
        "Monitors data sanity, row counts, and structural anomalies across all 4 ingested NHS datasets."
    )

    with st.expander("ℹ️ How Stage 1 Works & Terminology"):
        st.markdown("""
        Stage 1 enforces automated data quality checks during ETL ingestion:
        *   **Row Count Jumps**: Flags sudden drops or spikes in ingested row counts month-over-month.
        *   **Null Checks**: Ensures mandatory keys (`org_code`, `month_date`) contain no missing values.
        *   **Valid Range Bounds**: Verifies percentages (e.g. A&E breach %, RTT 18wk+ %) stay within [0,
            100]%.
        """)

    df_dq = safe_load(load_dq_flags, context="data quality flags")

    if df_dq.empty:
        st.info("No data quality flags found — run `python dq/run_all.py` to populate dq_flags.")
        return

    # KPI Metrics
    col1, col2, col3, col4 = st.columns(4)
    total_checks = len(df_dq)
    ok_checks = (df_dq['status'] == 'OK').sum()
    flagged_checks = (df_dq['status'] != 'OK').sum()

    col1.metric("Total DQ Checks", total_checks)
    col2.metric("Passed Checks (OK)", ok_checks)
    col3.metric("Flagged Checks / Warnings", flagged_checks, delta_color="inverse")
    col4.metric("Datasets Monitored", df_dq['dataset'].nunique())

    st.markdown("---")

    # Filter controls
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        dataset_filter = st.multiselect(
            "Filter Datasets:", options=df_dq['dataset'].unique(), default=df_dq['dataset'].unique()
        )
    with col_f2:
        status_filter = st.multiselect(
            "Filter Status:", options=df_dq['status'].unique(), default=df_dq['status'].unique()
        )

    filtered_dq = df_dq[(df_dq['dataset'].isin(dataset_filter)) & (df_dq['status'].isin(status_filter))]

    # Table of DQ Flags
    st.subheader("Data Quality Audit Logs")
    if filtered_dq.empty:
        st.info("No rows match the current filters.")
    else:
        st.dataframe(filtered_dq, use_container_width=True)

    # Underlying Metric Chart with DQ Status
    st.subheader("Data Volume Trends & Integrity Check")
    df_vol = safe_load(load_dq_volume_trends, context="data volume trends")

    if df_vol.empty:
        st.info("No fact table data available to chart volume trends.")
    else:
        fig_vol = px.line(
            df_vol,
            x="month_date",
            y="record_count",
            color="dataset",
            markers=True,
            title="Monthly Record Counts per Dataset (Ingestion Consistency)"
        )
        fig_vol.update_layout(xaxis_title="Month", yaxis_title="Record Count", height=400)
        st.plotly_chart(fig_vol, use_container_width=True)


# ==============================================================================
# STAGE 2: CHANGEPOINT DETECTION DASHBOARD
# ==============================================================================
def page_stage2():
    st.title("📈 Stage 2: Statistical Changepoint & SPC Signal Detection")

    if selected_trust is None:
        st.warning("No trust selected — see the sidebar.")
        return

    st.markdown(
        f"Analyzes structural regime shifts (PELT algorithm) and process anomalies (XmR rules) "
        f"for **{selected_trust_name} ({selected_trust})**."
    )

    with st.expander("ℹ️ How Stage 2 Works & Signal Types"):
        st.markdown("""
        Stage 2 scans monthly performance metrics to detect operational signals without manual thresholds:
        *   **🚩 PELT Shift (Pruned Exact Linear Time)**: Identifies permanent structural step-changes in
            performance level (e.g. A&E breach rate shifting permanently from 25% to 38%).
        *   **⚠️ 3-Sigma Outlier (SPC Rule 1)**: Flags single months falling outside 3 moving-range standard
            deviations from baseline mean (isolated spike/drop).
        *   **🔄 Run of 7 (SPC Rule 2)**: Flags 7 consecutive months on the same side of the mean, signaling a
            process shift.
        """)

    # Load signals for selected trust
    df_cp = safe_load(load_cp_signals, selected_trust, context="changepoint/SPC signals")

    # Load time series for selected trust
    df_ae_ts = safe_load(load_ae_timeseries, selected_trust, context="A&E time series")
    df_rtt_ts = safe_load(load_rtt_timeseries, selected_trust, context="RTT time series")

    # Metrics Summary
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Signals", len(df_cp))
    c2.metric("PELT Changepoints", (df_cp['method'] == 'pelt_changepoint').sum() if not df_cp.empty else 0)
    c3.metric("SPC 3-Sigma Outliers", (df_cp['method'] == 'spc_rule1_3sigma').sum() if not df_cp.empty else 0)
    c4.metric("SPC Run-of-7 Shifts", (df_cp['method'] == 'spc_rule2_run7').sum() if not df_cp.empty else 0)

    st.markdown("---")

    # Time Series Visualization with Marked Signals
    st.subheader(f"Performance Time Series with Marked Signals: {selected_trust}")

    if len(df_ae_ts) > 0:
        fig = make_subplots(specs=[[{"secondary_y": True}]])

        # A&E Line
        fig.add_trace(
            go.Scatter(
                x=df_ae_ts['month_date'],
                y=df_ae_ts['breach_pct'],
                mode='lines+markers',
                name='A&E Breach % (4hr Waits)',
                line=dict(color='#2b5c8f', width=3)
            ),
            secondary_y=False
        )

        # RTT Line if available
        if len(df_rtt_ts) > 0:
            fig.add_trace(
                go.Scatter(
                    x=df_rtt_ts['month_date'],
                    y=df_rtt_ts['over_18wk_pct'],
                    mode='lines+markers',
                    name='RTT 18wk+ Breach %',
                    line=dict(color='#e74c3c', width=2, dash='dot')
                ),
                secondary_y=True
            )

        # Overlay Changepoints and Signals
        for _, sig in df_cp.iterrows():
            m_date = str(sig['month_date'])
            method = sig['method']

            if method == 'pelt_changepoint':
                fig.add_shape(
                    type="line",
                    x0=m_date, x1=m_date, y0=0, y1=1, yref="paper",
                    line=dict(color="darkred", width=2, dash="dash")
                )
                fig.add_annotation(
                    x=m_date, y=1.02, yref="paper",
                    text="🚩 PELT Shift",
                    showarrow=False,
                    font=dict(color="darkred", size=11, family="Arial")
                )
            elif method == 'spc_rule1_3sigma':
                fig.add_shape(
                    type="line",
                    x0=m_date, x1=m_date, y0=0, y1=1, yref="paper",
                    line=dict(color="orange", width=1.5, dash="dot")
                )
                fig.add_annotation(
                    x=m_date, y=0.05, yref="paper",
                    text="⚠️ 3-Sigma",
                    showarrow=False,
                    font=dict(color="orange", size=10, family="Arial")
                )
            elif method == 'spc_rule2_run7':
                fig.add_shape(
                    type="line",
                    x0=m_date, x1=m_date, y0=0, y1=1, yref="paper",
                    line=dict(color="purple", width=1.5, dash="dot")
                )
                fig.add_annotation(
                    x=m_date, y=0.15, yref="paper",
                    text="🔄 Run 7",
                    showarrow=False,
                    font=dict(color="purple", size=10, family="Arial")
                )

        fig.update_layout(
            title=f"A&E and RTT Performance Time Series — {selected_trust_name}",
            xaxis_title="Month",
            height=450,
            hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )
        fig.update_yaxes(title_text="<b>A&E Breach %</b>", secondary_y=False)
        fig.update_yaxes(title_text="<b>RTT 18wk+ %</b>", secondary_y=True)

        st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning(f"No A&E attendance time series data found for {selected_trust}.")

    # Table of Signals
    st.subheader("Detected Signals Table")
    if len(df_cp) > 0:
        st.dataframe(df_cp, use_container_width=True)
    else:
        st.info("No Stage 2 changepoint or SPC signals detected for this trust.")


# ==============================================================================
# STAGE 3: CORRELATIONS DASHBOARD
# ==============================================================================
def page_stage3():
    st.title("🔗 Stage 3: Cross-Metric & Cross-Domain Correlations")
    st.markdown(
        "Investigates cross-dataset interactions across 3 hierarchy tiers: Macro Regional, "
        "Trust Level, and Signal-Driven Primary Care Rollups."
    )

    with st.expander("ℹ️ How Stage 3 Works & Key Findings Guide", expanded=True):
        st.markdown("""
        Stage 3 evaluates whether performance shifts in emergency care (A&E) correlate with elective care
        (RTT) or primary care supply changes (Prescribing & Dental):
        *   **Tier 1 (Macro Regional)**: Evaluates 6-month rolling correlations ($r$) between Regional A&E
            breach rates and GP Prescribing volumes across NHS Regions.
        *   **Tier 2 (Trust Level)**: Directly pairs A&E Breach % against RTT 18wk+ Breach % for each acute
            trust. Strong positive correlation ($r > 0.6$) indicates hospital-wide capacity bottlenecks.
        *   **Tier 3 (Signal-Driven Primary Care)**: Checks if A&E changepoints coincide with primary care
            prescribing or dental activity shifts within the trust's parent ICB.
        """)

    tier_tab = st.radio(
        "Select Correlation Tier:",
        [
            "Tier 1: Regional (Macro)",
            "Tier 2: Trust (A&E vs RTT)",
            "Tier 3: Signal-Driven (Primary vs Secondary Care)",
        ],
        horizontal=True
    )

    st.markdown("---")

    if "Tier 1" in tier_tab:
        st.subheader("Tier 1: Regional Macro Correlations (Rolling r Over Time)")

        df_t1 = safe_load(load_tier1_correlations, context="Tier 1 regional correlations")

        if df_t1.empty:
            st.info(
                "No Tier 1 regional correlation signals found — run `python corr/run_all.py` "
                "to populate them."
            )
            return

        # Map human-readable region names
        region_map = {
            'Y56': 'London (Y56)',
            'Y58': 'South West (Y58)',
            'Y59': 'South East (Y59)',
            'Y60': 'Midlands (Y60)',
            'Y61': 'East of England (Y61)',
            'Y62': 'North West (Y62)',
            'Y63': 'North East & Yorkshire (Y63)'
        }
        df_t1['region_name'] = df_t1['geography_code'].map(region_map).fillna(df_t1['geography_code'])

        # Key Findings Callout Box
        st.success("""
        💡 **Key Findings & Explanation (Tier 1 Macro Analysis)**:
        *   **Interpretation**: Measures rolling 6-month Pearson correlation ($r$) over time between Regional
            Pairs (`ae_prescribing_region`, `ae_dental_region`, `rtt_prescribing_region`,
            `rtt_dental_region`).
        *   **Macro Insight**: Most regions show low-to-moderate monthly correlation, indicating that primary
            care prescribing and dental activity volumes move independently of regional emergency department
            breach rates at the macro geographic scale.
        """)

        # Controls for filtering chart
        col_t1_1, col_t1_2 = st.columns(2)
        with col_t1_1:
            selected_pair_filter = st.multiselect(
                "Filter Dataset Pairs:",
                options=df_t1['dataset_pair'].unique(),
                default=df_t1['dataset_pair'].unique()
            )
        with col_t1_2:
            selected_region_filter = st.multiselect(
                "Filter NHS Regions:",
                options=df_t1['region_name'].unique(),
                default=df_t1['region_name'].unique()
            )

        filtered_t1 = df_t1[
            (df_t1['dataset_pair'].isin(selected_pair_filter)) &
            (df_t1['region_name'].isin(selected_region_filter))
        ]

        if filtered_t1.empty:
            st.info("No rows match the current filters.")
            return

        st.dataframe(filtered_t1, use_container_width=True)

        st.markdown("---")

        # Visualizations: Line Chart over time + Boxplot
        st.subheader("Rolling Correlation Visualizations")

        # Line Chart over time (Faceted small multiples by dataset_pair to prevent clutter)
        fig_line = px.line(
            filtered_t1,
            x="month_date",
            y="correlation_metric",
            color="region_name",
            facet_col="dataset_pair",
            facet_col_wrap=2,
            markers=True,
            title="Rolling Correlation Over Time (Faceted by Dataset Pair)",
            labels={
                "correlation_metric": "Rolling Pearson r",
                "month_date": "Reporting Month",
                "region_name": "NHS Region",
                "dataset_pair": "Pair"
            }
        )
        fig_line.update_layout(height=520, hovermode="x unified", legend=dict(orientation="h", y=-0.2))
        fig_line.update_yaxes(range=[-1.0, 1.0])
        st.plotly_chart(fig_line, use_container_width=True)

        # Existing Boxplot (Overall Spread)
        fig_box = px.box(
            filtered_t1,
            x="geography_code",
            y="correlation_metric",
            color="dataset_pair",
            points="all",
            title="Overall Distribution & Spread of Correlation Coefficients per Region",
            labels={
                "geography_code": "NHS Region Code",
                "correlation_metric": "Pearson r",
                "dataset_pair": "Dataset Pair"
            }
        )
        fig_box.update_layout(
            xaxis_title="NHS Region Code", yaxis_title="Pearson Correlation (r)", height=420
        )
        st.plotly_chart(fig_box, use_container_width=True)

    elif "Tier 2" in tier_tab:
        if selected_trust is None:
            st.warning("No trust selected — see the sidebar.")
            return

        st.subheader(f"Tier 2: Trust-Level A&E vs. RTT Correlation — {selected_trust_name}")

        df_t2 = safe_load(load_tier2_correlations, selected_trust, context="Tier 2 trust correlations")

        # Calculate average trust correlation
        avg_r = df_t2['correlation_metric'].mean() if len(df_t2) > 0 else np.nan

        # Key Findings Callout Box
        if not np.isnan(avg_r):
            if avg_r > 0.6:
                insight = (
                    f"**Strong Positive Correlation (mean r = {avg_r:.2f})**: Emergency A&E breaches "
                    f"and Elective RTT waiting list breaches move together tightly. This indicates a "
                    f"**systemic hospital-wide bed/staffing bottleneck** affecting both emergency "
                    f"admissions and elective surgeries simultaneously."
                )
                box_type = st.warning
            elif avg_r < -0.3:
                insight = (
                    f"**Negative Correlation (mean r = {avg_r:.2f})**: Emergency A&E spikes occur "
                    f"independently or inverse to elective RTT waits, suggesting operational "
                    f"decoupling or resource reallocation between emergency and elective wards."
                )
                box_type = st.info
            else:
                insight = (
                    f"**Moderate/Weak Correlation (mean r = {avg_r:.2f})**: Emergency department "
                    f"performance fluctuates with minimal direct coupling to elective waiting list "
                    f"build-ups for this trust."
                )
                box_type = st.info

            box_type(f"💡 **Key Finding & Interpretation ({selected_trust})**:\n\n{insight}")
        else:
            st.info(
                f"No Tier 2 correlation signals found for {selected_trust} — usually means "
                f"fewer than 6 months of paired A&E/RTT data."
            )

        if not df_t2.empty:
            st.dataframe(df_t2, use_container_width=True)

        # Paired Time Series Chart
        df_ae = safe_load(load_ae_timeseries, selected_trust, context="A&E time series")
        df_rtt = safe_load(load_rtt_timeseries, selected_trust, context="RTT time series")

        if len(df_ae) > 0 and len(df_rtt) > 0:
            df_merged = pd.merge(df_ae, df_rtt, on="month_date", suffixes=('_ae', '_rtt'))

            fig_t2 = make_subplots(specs=[[{"secondary_y": True}]])
            fig_t2.add_trace(
                go.Scatter(
                    x=df_merged['month_date'], y=df_merged['breach_pct'],
                    name="A&E Breach %", line=dict(color='#2980b9', width=3),
                ),
                secondary_y=False,
            )
            fig_t2.add_trace(
                go.Scatter(
                    x=df_merged['month_date'], y=df_merged['over_18wk_pct'],
                    name="RTT 18wk+ Breach %", line=dict(color='#e67e22', width=3),
                ),
                secondary_y=True,
            )

            fig_t2.update_layout(
                title=f"A&E vs. RTT Paired Time Series — {selected_trust_name}",
                height=450, hovermode="x unified",
            )
            fig_t2.update_yaxes(title_text="<b>A&E Breach %</b>", secondary_y=False)
            fig_t2.update_yaxes(title_text="<b>RTT 18wk+ %</b>", secondary_y=True)

            st.plotly_chart(fig_t2, use_container_width=True)
        else:
            st.info("Not enough paired A&E/RTT time series data to chart for this trust.")

    else:
        if selected_trust is None:
            st.warning("No trust selected — see the sidebar.")
            return

        st.subheader("Tier 3: Signal-Driven Primary Care Shift Analysis (Prescribing & Dental)")

        st.info("""
        💡 **Key Finding & Method Explanation (Tier 3)**:
        *   **Goal**: Tests whether secondary care A&E changepoints coincide with statistically significant
            shifts in primary care prescribing (Tier 3a) or dental activity (Tier 3b) within the trust's
            parent ICB.
        *   **Finding**: If `finding = 'no_correlated_shift'`, the secondary care changepoint occurred without
            a matching primary care shift in the surrounding window, confirming the A&E performance shift was
            driven by **internal hospital operational factors** rather than primary care supply changes.
        """)

        col_3a, col_3b = st.columns(2)

        with col_3a:
            st.markdown("#### Tier 3a: Prescribing Shift Findings")
            df_t3a = safe_load(load_tier3a_findings, selected_trust, context="Tier 3a prescribing findings")
            if df_t3a.empty:
                st.info(f"No Tier 3a findings for {selected_trust}.")
            else:
                st.dataframe(df_t3a, use_container_width=True)

        with col_3b:
            st.markdown("#### Tier 3b: Dental UDA Shift Findings")
            df_t3b = safe_load(load_tier3b_findings, selected_trust, context="Tier 3b dental findings")
            if df_t3b.empty:
                st.info(f"No Tier 3b findings for {selected_trust}.")
            else:
                st.dataframe(df_t3b, use_container_width=True)

        # Show all findings across all trusts for summary
        st.markdown("---")
        st.markdown("#### National Summary: All Detected Tier 3 Shift Findings Across Trusts")
        df_all_t3a = safe_load(load_tier3a_national_summary, context="Tier 3a national summary")
        if len(df_all_t3a) > 0:
            st.dataframe(df_all_t3a, use_container_width=True)
        else:
            st.warning(
                "No co-occurring primary care prescribing or dental shifts were detected for "
                "secondary care changepoints (no candidate pair reached |r| >= 0.5). A&E shifts are "
                "predominantly driven by internal trust/hospital capacity dynamics."
            )


# ==============================================================================
# NAVIGATION — native sidebar page selector (st.navigation/st.Page) in place of
# the previous st.sidebar.radio + if/elif routing.
# ==============================================================================
pg = st.navigation([
    st.Page(page_stage1, title="Stage 1: Data Quality", icon="🛡️", url_path="stage1"),
    st.Page(page_stage2, title="Stage 2: Changepoints", icon="📈", url_path="stage2"),
    st.Page(page_stage3, title="Stage 3: Correlations", icon="🔗", url_path="stage3"),
])
pg.run()
