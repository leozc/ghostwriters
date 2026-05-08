"""Core type definitions for the v1 redesign.

These types are the contract for the orchestrator, adapters, and store.
See docs/v1-design.md for design rationale and docs/v1-requirements.md for FRs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# ---- Enums ----------------------------------------------------------------


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ABORTED = "aborted"


class StopReason(str, Enum):
    TARGET_REACHED = "target_reached"
    LOOP_LIMIT = "loop_limit"
    ABORTED = "aborted"


class Decision(str, Enum):
    KEPT = "kept"
    REJECTED = "rejected"


class PairwisePref(str, Enum):
    CANDIDATE = "candidate"
    INCUMBENT = "incumbent"
    TIE = "tie"


class FactCheckStatus(str, Enum):
    CLEAN = "clean"
    HAS_FINDINGS = "has_findings"


class ClaimVerdict(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    CONTRADICTED = "contradicted"
    SUBJECTIVE = "subjective"


# ---- Task config (input to POST /tasks) -----------------------------------


@dataclass(frozen=True)
class ReviewerSpec:
    """One reviewer in a task. weight=0 excludes from objective AND skips calls (FR11)."""

    reviewer_id: str
    weight: float


@dataclass(frozen=True)
class TaskConfig:
    """Inputs that define a task (FR2)."""

    article_id: str
    reviewers: list[ReviewerSpec]
    target_aggregate: float
    reviewer_floor: float
    loop_limit: int
    must_do_text: str = ""
    must_not_do_text: str = ""


# ---- Adapter outputs ------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """Output of editor.propose() for one iteration."""

    text: str
    edit_summary: str
    prompt_hash: str
    model: str


@dataclass(frozen=True)
class RubricScores:
    """Output of reviewer.score_rubric() for one (reviewer, candidate) pair."""

    scores: dict[str, float]  # dim -> score
    aggregate: float  # weighted average over rubric dims
    prompt_hash: str
    model: str


@dataclass(frozen=True)
class Pairwise:
    """Output of reviewer.compare_pairwise() for one (reviewer, candidate, incumbent)."""

    pref: PairwisePref
    rationale: str
    prompt_hash: str
    model: str


@dataclass(frozen=True)
class ReviewerOutput:
    """Combined per-reviewer output for one iteration (FR3)."""

    reviewer_id: str
    weight: float
    rubric: RubricScores
    pairwise: Pairwise


@dataclass(frozen=True)
class FactCheckClaim:
    """One claim extracted and assessed by the fact-checker."""

    text: str
    verdict: ClaimVerdict
    sources: list[str] = field(default_factory=list)
    rationale: str = ""


@dataclass(frozen=True)
class FactCheckReport:
    """Output of fact_checker.fact_check() — advisory only (FR6).

    Runs exactly once per task at end of loop, regardless of termination reason.
    Never blocks or reverts.
    """

    status: FactCheckStatus
    claims: list[FactCheckClaim]
    prompt_hash: str
    model: str


# ---- Iteration / lineage records -----------------------------------------


@dataclass(frozen=True)
class IterationRecord:
    """One entry in the lineage DAG (FR7).

    candidate_id points to either an article_versions row (kept) or a
    rejected_candidates row (rejected), distinguished by `decision`.
    """

    id: str
    task_id: str
    iter_index: int
    parent_version_id: str
    candidate_id: str
    decision: Decision
    decision_reason: str
    aggregate_score: float
    pref_delta: float
    pref_ci_low: float
    pref_ci_high: float
    reviewer_outputs: list[ReviewerOutput]
    created_at: datetime


@dataclass(frozen=True)
class HumanNote:
    """A human-injected note. Consumed-after-use: marked consumed in the same
    transaction as the iteration that uses it.
    """

    id: str
    task_id: str
    text: str
    consumed_at: datetime | None
    created_at: datetime


# ---- Editor inputs --------------------------------------------------------


@dataclass(frozen=True)
class EditorInput:
    """Bundle passed to editor.propose() each iteration.

    Per the Context management section in v1-design.md:
    - prior_reviews contains ONLY the most recent iteration's reviewer feedback
    - human_notes are consumed after one use; orchestrator marks consumed_at
    """

    incumbent_text: str
    prior_reviews: list[ReviewerOutput]
    must_do_text: str
    must_not_do_text: str
    human_notes: list[str]
