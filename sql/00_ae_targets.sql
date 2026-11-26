-- Purpose: the A&E 4-hour national target in force for each month, as a parameter view (one row per financial year) so a target change is a one-row edit here, not a change to every view that uses it.
-- Grain: one row per financial year (valid_from .. valid_to are the first and last month it applies to).
-- Sources: Target by financial year, applied to every month of that year (the guidance sets it "by/in March"); the constitutional standard is 95% throughout.
--   FY2023/24 76%: NHS England, "Delivery plan for recovering urgent and emergency care services" (Jan 2023): "76% performance in A&E wait times, measured through the 4-hour target" - https://www.england.nhs.uk/long-read/delivery-plan-for-recovering-urgent-and-emergency-care-services/
--   FY2024/25 78%: NHS England, "2024/25 priorities and operational planning guidance": "a minimum of 78% of patients seen within 4 hours in March 2025" - https://www.england.nhs.uk/long-read/2024-25-priorities-and-operational-planning-guidance/
--   FY2025/26 78%: NHS England, "Urgent and emergency care plan 2025/26": "a minimum of 78% of patients who attend A&E ... admitted, transferred or discharged within 4 hours" - https://www.england.nhs.uk/long-read/urgent-and-emergency-care-plan-2025-26/
--   FY2026/27 82%: NHS England, "Medium Term Planning Framework 2026/27 to 2028/29": "Every trust to maintain or improve to 82% by March 2027" - https://www.england.nhs.uk/long-read/medium-term-planning-framework-delivering-change-together-2026-27-to-2028-29/
DROP VIEW IF EXISTS reporting.vw_ae_targets CASCADE;
CREATE VIEW reporting.vw_ae_targets AS
SELECT fy_label, valid_from, valid_to, target_pct, 95.0::numeric AS constitutional_standard_pct
FROM (VALUES
    ('2023/24', DATE '2023-04-01', DATE '2024-03-01', 76.0),
    ('2024/25', DATE '2024-04-01', DATE '2025-03-01', 78.0),
    ('2025/26', DATE '2025-04-01', DATE '2026-03-01', 78.0),
    ('2026/27', DATE '2026-04-01', DATE '2027-03-01', 82.0)
) AS t (fy_label, valid_from, valid_to, target_pct);
