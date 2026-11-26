# NHS Monitoring Copilot: executive findings

**As of August 2025** · generated from commit `fixture` · figures are produced by `reporting/generate_executive_brief.py`; do not edit by hand.

## Data window

| Dataset | First month | Latest month | Months loaded |
|---|---|---|---|
| A&E | March 2024 | August 2025 | 18 |
| RTT | March 2024 | August 2025 | 18 |
| GP prescribing | March 2024 | March 2025 | 13 |
| NHS dental | March 2024 | August 2025 | 18 |

Primary-care data ends before the A&E and RTT data, so any comparison that involves prescribing or dental activity can only use the months where both sides have data.

## 1. A&E 4-hour performance against the target

- England-wide, 74.5% of A&E attendances in August 2025 were seen within 4 hours, against a national target of 78% and the constitutional standard of 95%.
- That is about 2,686 breaches above what the target allows in the month (11 of 12 reporting providers were over their allowance), and 20,000 over the last 12 months.

Providers with the most breaches above target allowance in August 2025:

| Provider | Attendances | Breaches | Above allowance |
|---|---|---|---|
| FIXTURE TRUST RAE (RAE) | 9,921 | 2,737 | 554 |
| FIXTURE TRUST R0B (R0B) | 9,360 | 2,513 | 454 |
| FIXTURE TRUST R1D (R1D) | 7,674 | 2,026 | 338 |
| FIXTURE TRUST RA7 (RA7) | 5,428 | 1,473 | 279 |
| FIXTURE TRUST R1A (R1A) | 7,113 | 1,834 | 269 |
| FIXTURE TRUST R1F (R1F) | 6,552 | 1,653 | 212 |
| FIXTURE TRUST RBL (RBL) | 8,797 | 2,129 | 194 |
| FIXTURE TRUST R1C (R1C) | 5,989 | 1,473 | 155 |
| FIXTURE TRUST R0A (R0A) | 8,236 | 1,913 | 101 |
| FIXTURE TRUST RAP (RAP) | 3,312 | 815 | 86 |

## 2. Elective waits: where the 52+ week waiters are

- In August 2025, 66.1% of the 68,462 patients on a waiting list had waited under 18 weeks (standard 92%); 1,139 had waited 52 weeks or more.
- 3 treatment functions account for 100.0% of specialty-level 52+ week waits.

| Treatment function | 52+ week waiters | Share % | Cumulative % |
|---|---|---|---|
| ENT | 402 | 35.3 | 35.3 |
| Trauma & Orthopaedics | 384 | 33.7 | 69.0 |
| General Surgery | 353 | 31.0 | 100.0 |

## 3. Signals worth a conversation

- **Shifts in a trust's own A&E performance** (last 6 months): 0 changepoints on the trust's own series, of which 0 are also seen once the national pattern is removed (trust minus national). Those are less likely to be a shared seasonal or national movement.

- **A&E and RTT moving together:** 12 trusts were tested for a link between month-on-month changes in A&E breaches and in the RTT 18-week share (window ending August 2025). 1 pass an uncorrected p < 0.05, but with that many tests about 1 would pass by chance alone. After false-discovery-rate control (Benjamini-Hochberg, q <= 0.05) 1 remain.

| Trust | r | 95% interval | q |
|---|---|---|---|
| FIXTURE TRUST RAP (RAP) | 0.70 | -0.80 to 0.90 | 0.001 |

- **Regions:** 0 of 18 region-level comparisons of A&E/RTT with prescribing and dental activity survive false-discovery-rate control, so no region-level link between acute pressure and primary-care activity is supported by this much data.

## 4. Admission conversion and breaches

Across 10 providers with Type 1 A&E over the last 12 months, the share of attendances admitted has a Spearman rank correlation of -0.30 (p = 0.405) with the share of attendances breaching 4 hours. There is no clear association at this sample size.

## Limitations

- **Short series.** Monthly series are short (see the data window), so changepoint detection has limited power and correlation windows contain 6 paired changes; confidence intervals are wide.
- **Association, not cause.** Everything above describes co-movement. None of it shows that one service's pressure causes another's.
- **Ecological data.** Figures are provider or ICB aggregates; they say nothing about individual patients.
- **Boundary changes.** Provider mergers, ICB reorganisations and changes to reporting rules can look like real shifts; check the data-quality flags before acting on a signal.
- **Multiple testing.** Thousands of comparisons are run; only results that survive false-discovery-rate control are described as supported.
