"""Reviewer adapter — two functions per FR3.

score_rubric: per-criterion 1-5 scores on a single candidate.
compare_pairwise: pick candidate vs incumbent vs tie.

Both take the same ReviewerConfig (so a reviewer's identity, rubric
dimensions, and per-call models are configured once) and a Provider.
The orchestrator combines the two outputs into a ReviewerOutput.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ghostwriter.adapters.provider import Provider, prompt_hash
from ghostwriter.types import Pairwise, PairwisePref, RubricScores


_RUBRIC_SYSTEM = (
    "You are a careful reviewer. Score the article on each listed criterion "
    "using a 1-5 integer scale (1=poor, 5=excellent). Output only the JSON "
    "object specified."
)

_RUBRIC_USER_TEMPLATE = """\
Reviewer id: {reviewer_id}

CRITERIA (score each on 1-5 integer scale):
{criteria_block}

ARTICLE:
<<<ARTICLE
{candidate}
ARTICLE>>>

Respond with ONLY a JSON object:
{{"scores": {{"<criterion>": <int 1-5>, ...}}, "rationale": "<1-2 sentences>"}}\
"""

_PAIRWISE_SYSTEM = (
    "You are a careful reviewer comparing two versions of an article. Pick "
    "which is better OVERALL, or 'tie' if genuinely indistinguishable. Output "
    "only the JSON object specified."
)

_PAIRWISE_USER_TEMPLATE = """\
Reviewer id: {reviewer_id}

VERSION A (candidate):
<<<A
{candidate}
A>>>

VERSION B (incumbent):
<<<B
{incumbent}
B>>>

Which version is better OVERALL? Respond with ONLY a JSON object:
{{"pref": "candidate" | "incumbent" | "tie", "rationale": "<1-2 sentences>"}}\
"""


@dataclass(frozen=True)
class ReviewerConfig:
    """One reviewer's wiring. weight is metadata for the orchestrator;
    adapters don't use it but it lives here so the full spec is one object."""

    reviewer_id: str
    weight: float
    rubric_dims: tuple[str, ...]
    rubric_model: str
    pairwise_model: str
    rubric_system: str = _RUBRIC_SYSTEM
    rubric_user_template: str = _RUBRIC_USER_TEMPLATE
    pairwise_system: str = _PAIRWISE_SYSTEM
    pairwise_user_template: str = _PAIRWISE_USER_TEMPLATE
    max_tokens: int = 1024


def _validate_rubric_scores(
    scores: dict[str, float], expected_dims: tuple[str, ...]
) -> dict[str, float]:
    if set(scores.keys()) != set(expected_dims):
        raise ValueError(
            f"rubric dim mismatch: got {sorted(scores)} expected {sorted(expected_dims)}"
        )
    out: dict[str, float] = {}
    for dim, val in scores.items():
        if not isinstance(val, (int, float)) or not (1 <= val <= 5):
            raise ValueError(f"rubric score for {dim!r} out of range [1, 5]: {val!r}")
        out[dim] = float(val)
    return out


async def score_rubric(
    *,
    config: ReviewerConfig,
    candidate_text: str,
    provider: Provider,
) -> RubricScores:
    """Per-criterion 1-5 scores. Aggregate is the equal-weight mean of dims."""
    criteria_block = "\n".join(f"- {d}" for d in config.rubric_dims)
    user = config.rubric_user_template.format(
        reviewer_id=config.reviewer_id,
        criteria_block=criteria_block,
        candidate=candidate_text,
    )
    raw = await provider.complete(
        model=config.rubric_model,
        system=config.rubric_system,
        user=user,
        max_tokens=config.max_tokens,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"rubric reviewer returned non-JSON: {raw!r}") from e
    raw_scores = payload.get("scores")
    if not isinstance(raw_scores, dict):
        raise ValueError(f"rubric JSON missing 'scores' dict: {payload!r}")
    scores = _validate_rubric_scores(raw_scores, config.rubric_dims)
    aggregate = sum(scores.values()) / len(scores)
    return RubricScores(
        scores=scores,
        aggregate=aggregate,
        prompt_hash=prompt_hash(
            model=config.rubric_model, system=config.rubric_system, user=user
        ),
        model=config.rubric_model,
    )


async def compare_pairwise(
    *,
    config: ReviewerConfig,
    candidate_text: str,
    incumbent_text: str,
    provider: Provider,
) -> Pairwise:
    user = config.pairwise_user_template.format(
        reviewer_id=config.reviewer_id,
        candidate=candidate_text,
        incumbent=incumbent_text,
    )
    raw = await provider.complete(
        model=config.pairwise_model,
        system=config.pairwise_system,
        user=user,
        max_tokens=config.max_tokens,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"pairwise reviewer returned non-JSON: {raw!r}") from e
    pref_raw = payload.get("pref")
    if not isinstance(pref_raw, str):
        raise ValueError(f"pairwise JSON missing 'pref' string: {payload!r}")
    try:
        pref = PairwisePref(pref_raw)
    except ValueError as e:
        raise ValueError(
            f"pairwise 'pref' must be candidate/incumbent/tie, got {pref_raw!r}"
        ) from e
    rationale = payload.get("rationale", "")
    if not isinstance(rationale, str):
        rationale = ""
    return Pairwise(
        pref=pref,
        rationale=rationale,
        prompt_hash=prompt_hash(
            model=config.pairwise_model, system=config.pairwise_system, user=user
        ),
        model=config.pairwise_model,
    )
