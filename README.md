# NHS Monitoring Copilot

A pipeline that pulls together four public NHS datasets and automatically flags things worth looking at: data quality problems, sudden performance shifts, and patterns that show up across datasets at the same time.

Built with Python and DuckDB, with a Streamlit dashboard on top. No AI or language models are used for detection. Every signal comes from a statistical test, not a model guess.

## Why

NHS data is public, but it's spread across different sources in different formats (CSV, Excel, JSON), and each one organises hospitals and clinics differently. Answering a simple question, like "did GP prescribing change around the same time A&E got worse in this area", normally means combining several spreadsheets by hand. This project does that automatically and runs statistics on top, so you start from a short list of signals instead of four unrelated files.

## What it found

- **Most A&E performance shifts don't line up with a primary care change.** Out of 80 cases checked, 10 showed a statistically significant link (p ≤ 0.05). So most A&E shifts look like they start inside the hospital, but not all of them do.
- **Some trusts show A&E and elective waiting times moving together.** A handful of trusts have a strong correlation (r > 0.6) between A&E breaches and elective waiting list breaches. That fits with a shared bed or staffing problem, but correlation alone doesn't prove it.
- **Picking the right changepoint model made a real difference.** Two cost models were tested against each other across 10 trusts. One of them (l2 with a BIC penalty) won 3, tied 4, and never lost against the other (rbf with a fixed penalty). Full comparison in `cp/changepoints.py`.
- **A single trust's data going missing for a month was easy to miss.** A dataset wide check looks at total row counts, so one trust dropping out for a month barely moves the number. A separate per organisation check catches this instead, and it found a real case (see Stage 1 below).

## How it works

Four stages, each one building on the last, plus a dashboard.

### 1. ETL (`etl/`)

Loads the four raw datasets into a shared DuckDB database. Handles real messiness along the way: a dental billing code that split into three sub codes partway through 2022, and GP practices using a different area code system than hospitals do (a lookup table bridges the two). Each script can be re-run safely without creating duplicate rows.

### 2. Data quality checks (`dq/`)

Flags months where row counts, missing values, or out of range numbers jump compared to the month before. Also checks per organisation, not just per dataset. A trust called The Walton Centre dropped out of the A&E data for one month. The dataset wide check missed it, since it was under 1% of total rows. The per organisation check caught it.

### 3. Changepoint detection (`cp/`)

Looks for real shifts in A&E performance using two methods: simple statistical process control rules, and a changepoint algorithm (PELT) tuned by testing two cost models against each other on real data instead of guessing.

### 4. Cross dataset correlation (`corr/`)

Checks whether A&E, RTT, prescribing, and dental data move together, at three levels: by region, by trust, and (only for trusts where a changepoint was already found) by the trust's local ICB. Matching trusts to ICBs took extra care, since NHS boundaries changed partway through this project. Details are in the code.

### Dashboard

A Streamlit app for browsing all of the above, with a trust picker in the sidebar.

**One known gap:** two of the dashboard's database views aren't created by any script in this repo yet. If you're setting this up from scratch, run these two commands before opening the dashboard's first page:

```sql
CREATE VIEW corr_signals_tier1 AS SELECT * FROM corr_signals WHERE tier = 'tier1_region';
CREATE VIEW corr_signals_tier2 AS SELECT * FROM corr_signals WHERE tier = 'tier2_trust';
```

## Datasets

1. A&E attendances, monthly, by hospital trust
2. RTT waiting times, monthly, by hospital and specialty
3. GP prescribing, monthly, by practice (via the OpenPrescribing API)
4. Dental activity, by contract

## Project layout

```
NHS/
├── data/                    # Raw downloads & the DuckDB database (not in git)
├── etl/                     # Stage 1: load and clean the raw data
│   └── reference/           # Lookup tables used during loading
├── dq/                      # Stage 2: data quality checks
├── cp/                      # Stage 3: changepoint detection
├── corr/                    # Stage 4: cross dataset correlation
│   └── reference/           # Lookup tables used for correlation
├── docs/                    # Extra notes, including exact column mappings
├── reports/plots/           # Scratch comparison plots
├── tests/                   # Test suite, one file per package
├── dashboard.py             # Streamlit dashboard
├── dashboard_queries.py     # Database queries the dashboard uses
├── generate_plots.py        # Scratch script for the model comparison plots
├── requirements.txt
└── README.md
```

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the pipeline, in order:

```bash
python etl/run_all.py
python dq/run_all.py
python cp/run_all.py
python corr/run_all.py
```

Then match trusts to ICBs and run the last stage. This step calls the NHS ODS API once per trust, so it needs internet access and takes a few minutes:

```bash
python corr/reference/trust_to_icb.py
python corr/tier3_signal_driven.py
```

Run the tests:

```bash
python -m pytest tests/ -q
```

Launch the dashboard (see the known gap note above first):

```bash
streamlit run dashboard.py
```

## Status

- [x] ETL and database
- [x] Data quality checks
- [x] Changepoint detection
- [x] Cross dataset correlation
- [x] Dashboard
