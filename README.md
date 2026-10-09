# NHS Monitoring Copilot

A pipeline that pulls together four public NHS datasets and automatically flags things worth looking at: data quality problems, sudden performance shifts, and patterns that show up across datasets at the same time.

Built with Python and DuckDB, with a Streamlit dashboard on top.

## What A&E is

A&E stands for Accident & Emergency. It's the hospital department you turn up to without an appointment, or get taken to by ambulance. It can't turn people away and it can't plan its own workload, which is part of why it's worth measuring.

The main measure is the four-hour standard: what share of patients get admitted, transferred or discharged within four hours of arriving. Anything over that counts as a breach. That's the `breach_pct` column in this pipeline, worked out per trust per month across all three department types (Type 1 major units, Type 2 single-specialty units, and Type 3 minor injury and urgent treatment centres).

## Why I picked A&E

I wanted something where a change in the numbers actually means something, and A&E sits downstream of most of the rest of the system. If people can't get a GP appointment, some of them end up in A&E. If the wards are full, admitted patients wait on trolleys in A&E. If social care can't take people home, beds stay occupied and it backs up into A&E. So when A&E performance moves, it's often not really about A&E. It's the first place some other problem turns into a number I can actually see.

The waits matter clinically as well, not just as a target. Long waits in emergency departments are linked to worse outcomes, and it gets worse the longer people wait. A four-hour breach rate is a blunt measure, but it's standing in for something real.

There's also enough movement in the data to make it worth analysing. Over the 13 months I've loaded (March 2024 to March 2025) there are 28.7 million attendances across 200 trusts, and 7.6 million of those waited longer than four hours. That's a national breach rate of 26.6%, and it swings with the season: 24.2% in August 2024, 29.5% in December. A single trust shifting by a few points disappears completely in that average, which is why I went trust by trust rather than looking at the headline.

## Why I built this

"A&E got worse at this trust" isn't an answer, it's the start of one. The next question is what else changed at the same time, and that turned out to be annoying to answer even though all the data is public.

It's published, but it's scattered across different formats (CSV, Excel, JSON) and none of it lines up. A&E and RTT report by hospital trust, GP prescribing reports by practice, dental reports by contract. So asking something like "did prescribing shift around the time A&E got worse here" means joining several spreadsheets by hand and hoping the geography matches. I got tired of doing that manually.

So this does the joining, then runs the statistics on top, and I get to start from a short list of signals instead of four unrelated files. Three things I wanted it to tell me:

1. Did something actually change, or is it just noise? That's what the changepoint detection and the process control rules are for. I didn't want to be eyeballing charts and talking myself into seeing a pattern.
2. When A&E moved, did anything else move at the same time in the same area: prescribing, dental activity, elective waiting lists?
3. Can I trust the numbers underneath, or did a trust quietly stop reporting for a month? That last one isn't hypothetical, it actually happened (see Stage 1).

One thing worth saying plainly: this is all correlation on observational data. It can tell me two things moved together, and I've tried to be conservative about even claiming that much. It can't tell me one caused the other.

## What it found

- **Most A&E performance shifts don't line up with a primary care change.** Out of 80 cases checked, 10 showed a statistically significant link (p ≤ 0.05). So most A&E shifts look like they start inside the hospital, but not all of them do.
- **Some trusts show A&E and elective waiting times moving together.** A handful of trusts have a strong correlation (r > 0.6) between A&E breaches and elective waiting list breaches. That fits with a shared bed or staffing problem, but correlation alone doesn't prove it.
- **Picking the right changepoint model made a real difference.** Two cost models were tested against each other across 10 trusts. One of them (l2 with a BIC penalty) won 3, tied 4, and never lost against the other (rbf with a fixed penalty). Full comparison in `cp/changepoints.py`.
- **A single trust's data going missing for a month was easy to miss.** A dataset wide check looks at total row counts, so one trust dropping out for a month barely moves the number. A separate per organisation check catches this instead, and it found a real case (see Stage 1 below).
- **The Feb 2026 A&E file marks its totals row differently.** It has `Org Code = TOTAL` with a normal Period, so the old filter loaded it as a fake organisation and doubled that month's national attendances (4.09M instead of 2.04M). The parser now drops totals by either column, and a Stage 1 check fails if any org code or name is `TOTAL`.

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
├── app/                     # Streamlit dashboard and plotting
│   ├── dashboard.py         # Streamlit dashboard
│   ├── dashboard_queries.py # Database queries the dashboard uses
│   └── generate_plots.py    # Scratch script for the model comparison plots
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

Launch the dashboard:

```bash
streamlit run app/dashboard.py
```

## Status

- [x] ETL and database
- [x] Data quality checks
- [x] Changepoint detection
- [x] Cross dataset correlation
- [x] Dashboard
