"""Pairwise-preference bootstrap CI (FR3 / FR4 / design §pairwise gate).

Each reviewer r emits a pairwise pref encoded as a numeric per-reviewer
score x_r — typically `P(candidate > incumbent | r) - 0.5`, so
CANDIDATE -> +0.5, TIE -> 0, INCUMBENT -> -0.5 (FR67). This module
takes the numeric scores directly and is agnostic of the enum mapping;
the orchestrator owns the conversion.

Given scores x_1..x_M with weights w_1..w_M (w_r > 0), we compute:

  - point estimate: weighted mean = sum(w * x) / sum(w)
  - non-parametric percentile bootstrap CI: resample reviewer indices
    with replacement B times, recompute the weighted mean each time,
    take the alpha/2 and 1-alpha/2 percentiles.

Defaults match the design doc: B=1000, 90% CI. The keep-vs-revert gate
(FR4) is `ci_low > 0`; it lives in the orchestrator, not here.

Reproducibility (FR11): pass `seed` deterministically — the orchestrator
should derive it from (task_id, iter_index) so audit replays produce
identical CI bounds.

Caveat acknowledged in the design doc: with M=3..8 reviewers the
bootstrap distribution is coarse and CIs are wide. That conservatism is
intentional — `ci_low > 0` requires near-unanimous preference, which is
the right behavior for a quality-gate.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PairwiseCI:
    mean: float
    ci_low: float
    ci_high: float


def bootstrap_pairwise_ci(
    scores: Sequence[float],
    weights: Sequence[float],
    *,
    n_bootstrap: int = 1000,
    confidence: float = 0.90,
    seed: int | None = None,
) -> PairwiseCI:
    """Weighted-mean point estimate plus a percentile bootstrap CI.

    Raises ValueError on invalid input. Returns a PairwiseCI with
    ci_low <= mean <= ci_high (within bootstrap noise).
    """
    if len(scores) == 0:
        raise ValueError("scores must be non-empty")
    if len(scores) != len(weights):
        raise ValueError(
            f"scores and weights length mismatch ({len(scores)} vs {len(weights)})"
        )
    if any(w <= 0 for w in weights):
        raise ValueError("weights must be strictly positive")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0, 1)")
    if n_bootstrap < 1:
        raise ValueError("n_bootstrap must be >= 1")

    n = len(scores)
    scores_t = tuple(float(s) for s in scores)
    weights_t = tuple(float(w) for w in weights)

    def weighted_mean(idxs: Sequence[int]) -> float:
        total_w = 0.0
        total_wx = 0.0
        for i in idxs:
            total_w += weights_t[i]
            total_wx += weights_t[i] * scores_t[i]
        return total_wx / total_w

    point = weighted_mean(range(n))

    rng = random.Random(seed)
    samples = [
        weighted_mean([rng.randrange(n) for _ in range(n)])
        for _ in range(n_bootstrap)
    ]
    samples.sort()

    alpha = 1.0 - confidence
    low_idx = int(alpha / 2 * n_bootstrap)
    high_idx = min(n_bootstrap - 1, int((1 - alpha / 2) * n_bootstrap))

    return PairwiseCI(mean=point, ci_low=samples[low_idx], ci_high=samples[high_idx])
