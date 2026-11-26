"""Multiple-testing control for Stage 3 correlations (Benjamini-Hochberg false discovery rate).

Stage 3 tests thousands of (geography, month) correlations. At alpha = 0.05 about 5% of
*independent null* tests come out "significant" by chance, so reading raw p-values off a
list that long is how you find patterns that are not there. Benjamini-Hochberg controls the
expected share of false discoveries among the results we call significant.

The test FAMILY is what BH is applied across, and it has to be a set of tests that answer
the same question. Mixing unrelated questions inflates or deflates q, so families are fixed
and documented here:

* Tier 1 / Tier 2 (rolling first-difference correlation): one family per
  tier x dataset_pair x window-end month, i.e. "of all the regions / trusts tested for this
  pairing this month, which are coupled beyond chance?". Windows of consecutive months
  overlap and are not independent of each other, so months are deliberately NOT pooled.
* Tier 3 Question A (trust vs its ICB's prescribing / dental change): one family per metric
  (prescribing or dental) across every trust and signal month tested.

BH assumes independent or positively dependent tests. Neighbouring trusts share a region and
an ICB, which is positive dependence, so BH is appropriate (the more conservative
Benjamini-Yekutieli is not needed); this is an assumption to state, not one we can verify.
"""
from collections import defaultdict

import numpy as np

ALPHA = 0.05


def benjamini_hochberg(p_values) -> np.ndarray:
    """BH-adjusted q-values for a family of p-values, in the input order.

    q_i = min over ranks j >= rank(i) of p_(j) * m / j, capped at 1, so q >= p always and
    q is monotone in p. NaN p-values stay NaN and are not counted in m.
    """
    p = np.asarray(p_values, dtype=float)
    q = np.full(p.shape, np.nan)
    valid = ~np.isnan(p)
    m = int(valid.sum())
    if m == 0:
        return q
    order = np.argsort(p[valid])
    ranked = p[valid][order] * m / np.arange(1, m + 1)
    adjusted = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.minimum(adjusted, 1.0)
    q[valid] = out
    return q


def annotate_fdr(rows: list[dict], family_keys: tuple[str, ...], alpha: float = ALPHA) -> list[dict]:
    """Add q_value and is_significant_fdr to each row (in place), one BH family per distinct
    tuple of `family_keys` values. Rows without a p_value get q_value None / not significant."""
    families = defaultdict(list)
    for i, row in enumerate(rows):
        families[tuple(row.get(k) for k in family_keys)].append(i)
    for indices in families.values():
        p_values = [rows[i].get("p_value") if rows[i].get("p_value") is not None else np.nan
                    for i in indices]
        for i, q in zip(indices, benjamini_hochberg(p_values)):
            rows[i]["q_value"] = None if np.isnan(q) else float(q)
            rows[i]["is_significant_fdr"] = bool(not np.isnan(q) and q <= alpha)
    for row in rows:
        row.setdefault("q_value", None)
        row.setdefault("is_significant_fdr", False)
    return rows
