"""Tests for ghostwriter.core.bootstrap_ci."""

from __future__ import annotations

import pytest

from ghostwriter.core.bootstrap_ci import PairwiseCI, bootstrap_pairwise_ci

# Encoding from the requirements doc (FR67):
#   P(cand > inc | r) - 0.5  =>  CANDIDATE +0.5, TIE 0.0, INCUMBENT -0.5.
CAND, TIE, INC = 0.5, 0.0, -0.5


def test_unanimous_candidate_collapses_ci():
    """All reviewers pick CANDIDATE: every bootstrap sample is +0.5."""
    out = bootstrap_pairwise_ci(
        [CAND, CAND, CAND], [1.0, 1.0, 1.0], seed=0
    )
    assert out.mean == pytest.approx(0.5)
    assert out.ci_low == pytest.approx(0.5)
    assert out.ci_high == pytest.approx(0.5)


def test_unanimous_incumbent_is_negative():
    out = bootstrap_pairwise_ci(
        [INC, INC, INC], [1.0, 1.0, 1.0], seed=0
    )
    assert out.mean == pytest.approx(-0.5)
    assert out.ci_low == pytest.approx(-0.5)
    assert out.ci_high == pytest.approx(-0.5)


def test_split_panel_straddles_zero():
    """1 candidate, 1 tie, 1 incumbent: mean = 0; CI must include 0."""
    out = bootstrap_pairwise_ci(
        [CAND, TIE, INC], [1.0, 1.0, 1.0], seed=42
    )
    assert out.mean == pytest.approx(0.0)
    assert out.ci_low <= 0.0 <= out.ci_high


def test_majority_candidate_does_not_unanimously_pass_gate():
    """2 of 3 reviewers prefer candidate: mean > 0 but CI lower bound
    can dip below 0 because the bootstrap regularly samples the one
    incumbent reviewer 3x. This is the design-doc "near-unanimous"
    behavior and the reason the gate is conservative.
    """
    out = bootstrap_pairwise_ci(
        [CAND, CAND, INC], [1.0, 1.0, 1.0], seed=0
    )
    assert out.mean == pytest.approx((0.5 + 0.5 - 0.5) / 3)
    assert out.ci_low < 0  # gate would reject — by design


def test_weights_shift_the_mean():
    """A high-weight CANDIDATE outweighs two low-weight INCUMBENTs."""
    out = bootstrap_pairwise_ci(
        [CAND, INC, INC], [10.0, 1.0, 1.0], seed=0
    )
    expected = (10 * CAND + INC + INC) / 12
    assert out.mean == pytest.approx(expected)
    assert out.mean > 0


def test_seed_determinism():
    args = dict(scores=[CAND, INC, TIE, CAND], weights=[1.0, 1.0, 1.0, 1.0])
    a = bootstrap_pairwise_ci(seed=7, **args)
    b = bootstrap_pairwise_ci(seed=7, **args)
    assert a == b


def test_single_reviewer_collapses():
    """One reviewer => every bootstrap sample is the same value."""
    out = bootstrap_pairwise_ci([CAND], [1.0], seed=0)
    assert out.mean == out.ci_low == out.ci_high == pytest.approx(CAND)


def test_ci_brackets_mean_within_noise():
    """For any seed and any input, ci_low <= mean and mean <= ci_high
    (allowing for the discrete bootstrap distribution)."""
    out = bootstrap_pairwise_ci(
        [CAND, INC, TIE, CAND, INC], [1.0, 1.0, 1.0, 1.0, 1.0], seed=99
    )
    assert out.ci_low <= out.mean <= out.ci_high


def test_higher_confidence_widens_ci():
    args = dict(
        scores=[CAND, CAND, INC, TIE, CAND, INC],
        weights=[1.0] * 6,
        seed=2026,
    )
    narrow = bootstrap_pairwise_ci(confidence=0.50, **args)
    wide = bootstrap_pairwise_ci(confidence=0.99, **args)
    assert wide.ci_low <= narrow.ci_low
    assert wide.ci_high >= narrow.ci_high


def test_returns_pairwiseci_dataclass():
    out = bootstrap_pairwise_ci([CAND], [1.0])
    assert isinstance(out, PairwiseCI)


# --- input validation ---


def test_empty_scores_raises():
    with pytest.raises(ValueError, match="non-empty"):
        bootstrap_pairwise_ci([], [])


def test_length_mismatch_raises():
    with pytest.raises(ValueError, match="length mismatch"):
        bootstrap_pairwise_ci([CAND, INC], [1.0])


def test_zero_weight_raises():
    """Reviewers with weight==0 must be filtered by the caller, not
    passed in. A zero weight here would either skew the mean or
    silently no-op; making it loud avoids surprises."""
    with pytest.raises(ValueError, match="strictly positive"):
        bootstrap_pairwise_ci([CAND, INC], [1.0, 0.0])


def test_negative_weight_raises():
    with pytest.raises(ValueError, match="strictly positive"):
        bootstrap_pairwise_ci([CAND, INC], [1.0, -0.5])


def test_invalid_confidence_raises():
    with pytest.raises(ValueError, match="confidence"):
        bootstrap_pairwise_ci([CAND], [1.0], confidence=0.0)
    with pytest.raises(ValueError, match="confidence"):
        bootstrap_pairwise_ci([CAND], [1.0], confidence=1.0)


def test_zero_n_bootstrap_raises():
    with pytest.raises(ValueError, match="n_bootstrap"):
        bootstrap_pairwise_ci([CAND], [1.0], n_bootstrap=0)


def test_arbitrary_score_scale_supported():
    """The module is agnostic to the score encoding. {-1, 0, +1} works
    just as well as {-0.5, 0, +0.5}; the gate `ci_low > 0` is
    scale-invariant."""
    out = bootstrap_pairwise_ci(
        [1.0, 1.0, 1.0], [1.0, 1.0, 1.0], seed=0
    )
    assert out.mean == pytest.approx(1.0)
    assert out.ci_low == pytest.approx(1.0)
