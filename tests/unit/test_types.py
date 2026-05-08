"""Smoke tests for ghostwriter.types — instantiation + enum semantics.

Just verifies the type definitions hold together. Real behavior tests
arrive with the lineage store (M4).
"""

from datetime import datetime, timezone

from ghostwriter.types import (
    ClaimVerdict,
    Decision,
    EditorInput,
    FactCheckClaim,
    FactCheckReport,
    FactCheckStatus,
    HumanNote,
    IterationRecord,
    Pairwise,
    PairwisePref,
    ReviewerOutput,
    ReviewerSpec,
    RubricScores,
    StopReason,
    TaskConfig,
    TaskStatus,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_task_config_with_must_do_must_not_do():
    cfg = TaskConfig(
        article_id="art_1",
        reviewers=[
            ReviewerSpec(reviewer_id="investor", weight=0.4),
            ReviewerSpec(reviewer_id="engineer", weight=0.3),
            ReviewerSpec(reviewer_id="vp", weight=0.3),
            ReviewerSpec(reviewer_id="legal", weight=0.0),
        ],
        target_aggregate=4.0,
        reviewer_floor=3.0,
        loop_limit=12,
        must_do_text="cite the source for every metric",
        must_not_do_text="no hype; no analogies to cars",
    )
    assert cfg.target_aggregate == 4.0
    assert cfg.reviewer_floor == 3.0
    assert sum(r.weight for r in cfg.reviewers) == 1.0
    excluded = [r for r in cfg.reviewers if r.weight == 0]
    assert len(excluded) == 1 and excluded[0].reviewer_id == "legal"


def test_task_config_defaults_empty_guardrails():
    cfg = TaskConfig(
        article_id="art_1",
        reviewers=[ReviewerSpec("r1", 1.0)],
        target_aggregate=4.0,
        reviewer_floor=3.0,
        loop_limit=5,
    )
    assert cfg.must_do_text == ""
    assert cfg.must_not_do_text == ""


def test_iteration_record_kept():
    rubric = RubricScores(
        scores={"clarity": 4.0, "trust": 3.5},
        aggregate=3.75,
        prompt_hash="rh",
        model="m",
    )
    pw = Pairwise(
        pref=PairwisePref.CANDIDATE,
        rationale="tighter",
        prompt_hash="ph",
        model="m",
    )
    review = ReviewerOutput(reviewer_id="investor", weight=0.5, rubric=rubric, pairwise=pw)

    rec = IterationRecord(
        id="it_1",
        task_id="task_1",
        iter_index=3,
        parent_version_id="v_2",
        candidate_id="v_3",
        decision=Decision.KEPT,
        decision_reason="pref_ci_low > 0",
        aggregate_score=3.9,
        pref_delta=0.4,
        pref_ci_low=0.1,
        pref_ci_high=0.7,
        reviewer_outputs=[review],
        created_at=_now(),
    )
    assert rec.decision is Decision.KEPT
    assert rec.pref_ci_low > 0
    assert len(rec.reviewer_outputs) == 1


def test_iteration_record_rejected_reason():
    rubric = RubricScores(scores={"clarity": 3.0}, aggregate=3.0, prompt_hash="", model="")
    pw = Pairwise(pref=PairwisePref.INCUMBENT, rationale="weaker", prompt_hash="", model="")
    review = ReviewerOutput(reviewer_id="r1", weight=1.0, rubric=rubric, pairwise=pw)
    rec = IterationRecord(
        id="it_1",
        task_id="task_1",
        iter_index=1,
        parent_version_id="v_1",
        candidate_id="rej_1",
        decision=Decision.REJECTED,
        decision_reason="no_pref_improvement",
        aggregate_score=3.0,
        pref_delta=-0.2,
        pref_ci_low=-0.5,
        pref_ci_high=0.1,
        reviewer_outputs=[review],
        created_at=_now(),
    )
    assert rec.decision is Decision.REJECTED
    assert rec.decision_reason == "no_pref_improvement"


def test_fact_check_report_status_values():
    clean = FactCheckReport(
        status=FactCheckStatus.CLEAN, claims=[], prompt_hash="", model=""
    )
    assert clean.status is FactCheckStatus.CLEAN
    assert clean.claims == []

    flagged = FactCheckReport(
        status=FactCheckStatus.HAS_FINDINGS,
        claims=[
            FactCheckClaim(
                text="X grew 50% in 2024",
                verdict=ClaimVerdict.UNVERIFIED,
                sources=[],
                rationale="no source found",
            ),
        ],
        prompt_hash="",
        model="",
    )
    assert flagged.status is FactCheckStatus.HAS_FINDINGS
    assert flagged.claims[0].verdict is ClaimVerdict.UNVERIFIED


def test_human_note_consumed_after_use():
    note = HumanNote(
        id="n1",
        task_id="t1",
        text="rephrase the intro",
        consumed_at=None,
        created_at=_now(),
    )
    assert note.consumed_at is None  # pending


def test_editor_input_carries_only_last_review():
    """Per Context management rule: prior_reviews holds last iter only."""
    rubric = RubricScores(scores={"x": 4.0}, aggregate=4.0, prompt_hash="", model="")
    pw = Pairwise(pref=PairwisePref.TIE, rationale="", prompt_hash="", model="")
    review = ReviewerOutput(reviewer_id="r1", weight=1.0, rubric=rubric, pairwise=pw)
    inp = EditorInput(
        incumbent_text="draft text",
        prior_reviews=[review],
        must_do_text="cite sources",
        must_not_do_text="no hype",
        human_notes=["use the new metric"],
    )
    assert len(inp.prior_reviews) == 1
    assert inp.must_do_text and inp.must_not_do_text


def test_enums_string_values_stable():
    assert TaskStatus.RUNNING.value == "running"
    assert StopReason.LOOP_LIMIT.value == "loop_limit"
    assert PairwisePref.CANDIDATE.value == "candidate"
    assert Decision.KEPT.value == "kept"
    assert FactCheckStatus.HAS_FINDINGS.value == "has_findings"
    assert ClaimVerdict.CONTRADICTED.value == "contradicted"
