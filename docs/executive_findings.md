# NHS Monitoring Copilot: executive findings

**As of August 2026** · generated from commit `0dab302-dirty` · figures are produced by `reporting/generate_executive_brief.py`; do not edit by hand.

## Data window

| Dataset | First month | Latest month | Months loaded |
|---|---|---|---|
| A&E | March 2024 | August 2026 | 30 |
| RTT | March 2024 | August 2026 | 30 |
| GP prescribing | October 2023 | March 2025 | 13 |
| NHS dental | November 2022 | March 2025 | 14 |

Primary-care data ends before the A&E and RTT data, so any comparison that involves prescribing or dental activity can only use the months where both sides have data.

## 1. A&E 4-hour performance against the target

- England-wide, 74.5% of A&E attendances in August 2026 were seen within 4 hours, against a national target of 82% and the constitutional standard of 95%.
- That is about 205,079 breaches above what the target allows in the month (109 of 191 reporting providers were over their allowance), and 1,984,386 over the last 12 months.

Providers with the most breaches above target allowance in August 2026:

| Provider | Attendances | Breaches | Above allowance |
|---|---|---|---|
| UNIVERSITY HOSPITALS SUSSEX NHS FOUNDATION TRUST (RYR) | 31,540 | 12,948 | 7,271 |
| UNIVERSITY HOSPITALS BIRMINGHAM NHS FOUNDATION TRUST (RRK) | 45,177 | 14,107 | 5,975 |
| UNIVERSITY HOSPITALS OF DERBY AND BURTON NHS FOUNDATION TRUST (RTG) | 29,098 | 9,771 | 4,533 |
| UNIVERSITY HOSPITALS OF LEICESTER NHS TRUST (RWE) | 24,386 | 8,763 | 4,374 |
| MID AND SOUTH ESSEX NHS FOUNDATION TRUST (RAJ) | 35,180 | 10,490 | 4,158 |
| THE SHREWSBURY AND TELFORD HOSPITAL NHS TRUST (RXW) | 13,729 | 6,618 | 4,147 |
| BARTS HEALTH NHS TRUST (R1H) | 43,986 | 11,830 | 3,913 |
| NOTTINGHAM UNIVERSITY HOSPITALS NHS TRUST (RX1) | 24,831 | 8,216 | 3,746 |
| UNIVERSITY HOSPITALS OF NORTH MIDLANDS NHS TRUST (RJE) | 23,869 | 7,971 | 3,675 |
| YORK AND SCARBOROUGH TEACHING HOSPITALS NHS FOUNDATION TRUST (RCB) | 18,761 | 6,958 | 3,581 |

## 2. Elective waits: where the 52+ week waiters are

- In August 2026, 64.5% of the 6,682,788 patients on a waiting list had waited under 18 weeks (standard 92%); 105,918 had waited 52 weeks or more.
- 10 treatment functions account for 81.5% of specialty-level 52+ week waits.

| Treatment function | 52+ week waiters | Share % | Cumulative % |
|---|---|---|---|
| Trauma and Orthopaedic Service | 18,467 | 17.4 | 17.4 |
| Gynaecology Service | 12,589 | 11.9 | 29.3 |
| Ear Nose and Throat Service | 11,438 | 10.8 | 40.1 |
| Other - Surgical Services | 8,096 | 7.6 | 47.8 |
| General Surgery Service | 7,999 | 7.6 | 55.3 |
| Oral Surgery Service | 7,517 | 7.1 | 62.4 |
| Other - Medical Services | 5,230 | 4.9 | 67.4 |
| Dermatology Service | 5,095 | 4.8 | 72.2 |
| Urology Service | 5,040 | 4.8 | 76.9 |
| Ophthalmology Service | 4,818 | 4.5 | 81.5 |

## 3. Signals worth a conversation

- **Shifts in a trust's own A&E performance** (last 6 months): 28 changepoints on the trust's own series, of which 17 are also seen once the national pattern is removed (trust minus national). Those are less likely to be a shared seasonal or national movement.

| Trust | Month | Breach % before | Breach % after |
|---|---|---|---|
| AIREDALE NHS FOUNDATION TRUST (RCF) | May 2026 | 37.9 | 43.5 |
| EPSOM AND ST HELIER UNIVERSITY HOSPITALS NHS TRUST (RVR) | May 2026 | 26.4 | 33.8 |
| HERTFORDSHIRE COMMUNITY NHS TRUST (RY4) | May 2026 | 3.3 | 8.2 |
| CHIPPENHAM COMMUNITY HOSPITAL (NQT1I) | April 2026 | 1.2 | 2.9 |
| UNIVERSITY HOSPITALS DORSET NHS FOUNDATION TRUST (R0D) | April 2026 | 30.2 | 29.6 |
| MILTON KEYNES UNIVERSITY HOSPITAL NHS FOUNDATION TRUST (RD8) | April 2026 | 25.6 | 30.9 |
| UNIVERSITY HOSPITALS BIRMINGHAM NHS FOUNDATION TRUST (RRK) | April 2026 | 38.4 | 30.1 |
| NORTH CHESHIRE AND MERSEY NHS FOUNDATION TRUST (RWW) | April 2026 | 36.3 | 24.8 |
| BERKSHIRE HEALTHCARE NHS FOUNDATION TRUST (RWX) | April 2026 | 0.9 | 1.4 |
| SOUTH TYNESIDE AND SUNDERLAND NHS FOUNDATION TRUST (R0B) | March 2026 | 30.4 | 22.9 |

- **A&E and RTT moving together:** 123 trusts were tested for a link between month-on-month changes in A&E breaches and in the RTT 18-week share (window ending August 2026). 10 pass an uncorrected p < 0.05, but with that many tests about 6 would pass by chance alone. After false-discovery-rate control (Benjamini-Hochberg, q <= 0.05) 0 remain.

- **Regions:** 0 of 168 region-level comparisons of A&E/RTT with prescribing and dental activity survive false-discovery-rate control, so no region-level link between acute pressure and primary-care activity is supported by this much data.

## 4. Admission conversion and breaches

Across 121 providers with Type 1 A&E over the last 12 months, the share of attendances admitted has a Spearman rank correlation of 0.47 (p < 0.001) with the share of attendances breaching 4 hours. Providers that admit a larger share of their attendees tend to breach more. This is consistent with inpatient bed availability and case mix shaping A&E waits, but the data cannot separate those from other explanations.

## Limitations

- **Short series.** Monthly series are short (see the data window), so changepoint detection has limited power and correlation windows contain 6 paired changes; confidence intervals are wide.
- **Association, not cause.** Everything above describes co-movement. None of it shows that one service's pressure causes another's.
- **Ecological data.** Figures are provider or ICB aggregates; they say nothing about individual patients.
- **Boundary changes.** Provider mergers, ICB reorganisations and changes to reporting rules can look like real shifts; check the data-quality flags before acting on a signal.
- **Multiple testing.** Thousands of comparisons are run; only results that survive false-discovery-rate control are described as supported.
