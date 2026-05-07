"""Tests for ghostwriter.store.sqlite — the lineage store (M4).

Covers:
  - Schema bootstrap (idempotent)
  - Article and task creation
  - Atomic iteration writes (kept and rejected paths)
  - Idempotency lookup (FR9)
  - Human-note consume-after-use (FR15-style)
  - Resume state for crash recovery (FR12)
  - Fact-check save and retrieval (FR6)
  - Lineage read in iter_index order (FR13)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ghostwriter.store.sqlite import Store
from ghostwriter.types import (
    ClaimVerdict,
    Decision,
    FactCheckClaim,
    FactCheckReport,
    FactCheckStatus,
    Pairwise,
    PairwisePref,
    ReviewerOutput,
    ReviewerSpec,
    RubricScores,
    StopReason,
    TaskConfig,
    TaskStatus,
)


# ---- helpers --------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(db_path=tmp_path / "ghostwriter.db", content_root=tmp_path / "content")
    yield s
    s.close()


def _make_review(
    reviewer_id: str,
    weight: float,
    pref: PairwisePref,
    rubric_model: str = "m-rubric",
    pairwise_model: str = "m-pairwise",
) -> ReviewerOutput:
    return ReviewerOutput(
        reviewer_id=reviewer_id,
        weight=weight,
        rubric=RubricScores(
            scores={"clarity": 4.0, "trust": 3.5},
            aggregate=3.75,
            prompt_hash="rh",
            model=rubric_model,
        ),
        pairwise=Pairwise(
            pref=pref,
            rationale="ok",
            prompt_hash="ph",
            model=pairwise_model,
        ),
    )


def _bootstrap_article_and_task(store: Store) -> tuple[str, str, str]:
    article_id, version_id = store.create_article(slug="launch", content="initial draft")
    task_id = store.create_task(
        TaskConfig(
            article_id=article_id,
            reviewers=[
                ReviewerSpec("investor", 0.4),
                ReviewerSpec("engineer", 0.3),
                ReviewerSpec("vp", 0.3),
                ReviewerSpec("legal", 0.0),  # excluded by weight (FR11)
            ],
            target_aggregate=4.0,
            reviewer_floor=3.0,
            loop_limit=5,
            must_do_text="cite sources",
            must_not_do_text="no hype",
        )
    )
    store.mark_task_running(task_id)
    return article_id, version_id, task_id


# ---- schema and basic CRUD ------------------------------------------------


def test_schema_init_is_idempotent(tmp_path: Path):
    db = tmp_path / "g.db"
    s1 = Store(db_path=db, content_root=tmp_path / "c1")
    s1.close()
    # Re-open should not raise; CREATE TABLE IF NOT EXISTS is idempotent.
    s2 = Store(db_path=db, content_root=tmp_path / "c2")
    s2.close()


def test_create_and_get_article(store: Store):
    article_id, version_id = store.create_article(slug="my-post", content="hello world")
    art = store.get_article(article_id)
    assert art is not None and art["slug"] == "my-post"
    ver = store.get_version(version_id)
    assert ver is not None and ver["article_id"] == article_id
    assert store.content.read(ver["content_path"]) == "hello world"


def test_create_task_persists_reviewers_and_guardrails(store: Store):
    article_id, _ = store.create_article(slug="x", content="x")
    task_id = store.create_task(
        TaskConfig(
            article_id=article_id,
            reviewers=[ReviewerSpec("a", 0.6), ReviewerSpec("b", 0.4)],
            target_aggregate=4.0,
            reviewer_floor=3.0,
            loop_limit=10,
            must_do_text="MUST",
            must_not_do_text="DONT",
        )
    )
    task = store.get_task(task_id)
    assert task is not None
    assert task["must_do_text"] == "MUST"
    assert task["must_not_do_text"] == "DONT"
    reviewer_ids = sorted(r["reviewer_id"] for r in task["reviewers"])
    assert reviewer_ids == ["a", "b"]
    assert task["status"] == TaskStatus.PENDING.value


# ---- iteration writes -----------------------------------------------------


def test_record_iteration_kept_writes_version_and_scores(store: Store):
    article_id, parent_id, task_id = _bootstrap_article_and_task(store)

    iteration_id, candidate_id = store.record_iteration(
        task_id=task_id,
        article_id=article_id,
        iter_index=0,
        parent_version_id=parent_id,
        decision=Decision.KEPT,
        decision_reason="pref_ci_low > 0",
        candidate_text="improved draft",
        edit_summary="tightened intro",
        editor_prompt_hash="eh",
        editor_model="claude-sonnet",
        aggregate_score=3.9,
        pref_delta=0.4,
        pref_ci_low=0.1,
        pref_ci_high=0.7,
        reviewer_outputs=[
            _make_review("investor", 0.4, PairwisePref.CANDIDATE),
            _make_review("engineer", 0.3, PairwisePref.CANDIDATE),
            _make_review("vp", 0.3, PairwisePref.TIE),
        ],
    )
    # Version row exists and points at the right parent.
    ver = store.get_version(candidate_id)
    assert ver is not None
    assert ver["parent_version_id"] == parent_id
    assert store.content.read(ver["content_path"]) == "improved draft"

    # Iteration record + reviewer_scores are queryable via lineage.
    lineage = store.get_lineage(task_id)
    assert len(lineage) == 1
    rec = lineage[0]
    assert rec.id == iteration_id
    assert rec.decision is Decision.KEPT
    assert rec.candidate_id == candidate_id
    assert {ro.reviewer_id for ro in rec.reviewer_outputs} == {"investor", "engineer", "vp"}


def test_record_iteration_rejected_writes_to_rejected_table(store: Store):
    article_id, parent_id, task_id = _bootstrap_article_and_task(store)

    _, candidate_id = store.record_iteration(
        task_id=task_id,
        article_id=article_id,
        iter_index=0,
        parent_version_id=parent_id,
        decision=Decision.REJECTED,
        decision_reason="no_pref_improvement",
        candidate_text="worse draft",
        edit_summary="restructured",
        editor_prompt_hash="eh",
        editor_model="claude-sonnet",
        aggregate_score=3.2,
        pref_delta=-0.1,
        pref_ci_low=-0.4,
        pref_ci_high=0.2,
        reviewer_outputs=[_make_review("investor", 0.4, PairwisePref.INCUMBENT)],
    )
    # Should NOT create an article_versions row.
    assert store.get_version(candidate_id) is None
    rej = store.get_rejected(candidate_id)
    assert rej is not None
    assert rej["rejection_reason"] == "no_pref_improvement"

    lineage = store.get_lineage(task_id)
    assert lineage[0].decision is Decision.REJECTED
    assert lineage[0].decision_reason == "no_pref_improvement"


def test_iteration_rollback_on_failure(store: Store):
    """If the iteration tx fails partway, no rows or DB state should leak.

    We trigger failure by violating the (task_id, iter_index) UNIQUE constraint:
    the second record_iteration with the same iter_index must roll back
    everything and leave only the first iteration's rows in place.
    """
    article_id, parent_id, task_id = _bootstrap_article_and_task(store)

    # First iter at index 0 — succeeds.
    store.record_iteration(
        task_id=task_id,
        article_id=article_id,
        iter_index=0,
        parent_version_id=parent_id,
        decision=Decision.KEPT,
        decision_reason="ok",
        candidate_text="v1",
        edit_summary="",
        editor_prompt_hash="",
        editor_model="",
        aggregate_score=3.5,
        pref_delta=0.2,
        pref_ci_low=0.05,
        pref_ci_high=0.4,
        reviewer_outputs=[_make_review("r1", 1.0, PairwisePref.CANDIDATE)],
    )
    assert len(store.get_lineage(task_id)) == 1

    # Second iter also at index 0 — must fail and roll back, not leave a partial row.
    with pytest.raises(Exception):
        store.record_iteration(
            task_id=task_id,
            article_id=article_id,
            iter_index=0,
            parent_version_id=parent_id,
            decision=Decision.KEPT,
            decision_reason="dup",
            candidate_text="v2",
            edit_summary="",
            editor_prompt_hash="",
            editor_model="",
            aggregate_score=4.0,
            pref_delta=0.3,
            pref_ci_low=0.1,
            pref_ci_high=0.5,
            reviewer_outputs=[_make_review("r1", 1.0, PairwisePref.CANDIDATE)],
        )
    # Still exactly one iteration record; the failed write rolled back fully.
    lineage = store.get_lineage(task_id)
    assert len(lineage) == 1
    assert lineage[0].aggregate_score == 3.5


# ---- idempotency (FR9) ----------------------------------------------------


def test_idempotency_lookup_returns_iteration_id(store: Store):
    article_id, parent_id, task_id = _bootstrap_article_and_task(store)
    iteration_id, _ = store.record_iteration(
        task_id=task_id,
        article_id=article_id,
        iter_index=0,
        parent_version_id=parent_id,
        decision=Decision.KEPT,
        decision_reason="ok",
        candidate_text="v",
        edit_summary="",
        editor_prompt_hash="",
        editor_model="",
        aggregate_score=4.0,
        pref_delta=0.3,
        pref_ci_low=0.1,
        pref_ci_high=0.5,
        reviewer_outputs=[_make_review("r1", 1.0, PairwisePref.CANDIDATE)],
        idempotency_key="abc-123",
    )
    found = store.lookup_idempotency(task_id, "abc-123")
    assert found == iteration_id
    assert store.lookup_idempotency(task_id, "other") is None


# ---- human notes ----------------------------------------------------------


def test_human_note_consumed_after_use_atomically(store: Store):
    article_id, parent_id, task_id = _bootstrap_article_and_task(store)
    note_id = store.add_human_note(task_id, "use the new metric")
    pending = store.pending_notes(task_id)
    assert len(pending) == 1 and pending[0]["id"] == note_id

    store.record_iteration(
        task_id=task_id,
        article_id=article_id,
        iter_index=0,
        parent_version_id=parent_id,
        decision=Decision.KEPT,
        decision_reason="ok",
        candidate_text="v",
        edit_summary="",
        editor_prompt_hash="",
        editor_model="",
        aggregate_score=4.0,
        pref_delta=0.3,
        pref_ci_low=0.1,
        pref_ci_high=0.5,
        reviewer_outputs=[_make_review("r1", 1.0, PairwisePref.CANDIDATE)],
        consume_note_ids=[note_id],
    )
    assert store.pending_notes(task_id) == []


# ---- resume / crash recovery (FR12) ---------------------------------------


def test_resume_state_lists_running_tasks(store: Store):
    article_id, parent_id, task_id = _bootstrap_article_and_task(store)
    # Task is running; one iteration written.
    store.record_iteration(
        task_id=task_id,
        article_id=article_id,
        iter_index=0,
        parent_version_id=parent_id,
        decision=Decision.KEPT,
        decision_reason="ok",
        candidate_text="v",
        edit_summary="",
        editor_prompt_hash="",
        editor_model="",
        aggregate_score=3.0,
        pref_delta=0.1,
        pref_ci_low=0.01,
        pref_ci_high=0.3,
        reviewer_outputs=[_make_review("r1", 1.0, PairwisePref.CANDIDATE)],
    )
    state = store.resume_state()
    assert state == [(task_id, 0)]

    # After completion, task should not appear in resume_state.
    store.mark_task_terminal(
        task_id=task_id,
        status=TaskStatus.DONE,
        stop_reason=StopReason.TARGET_REACHED,
        final_version_id=None,
    )
    assert store.resume_state() == []


def test_resume_picks_correct_max_iter_index(store: Store):
    article_id, parent_id, task_id = _bootstrap_article_and_task(store)
    for i in range(3):
        store.record_iteration(
            task_id=task_id,
            article_id=article_id,
            iter_index=i,
            parent_version_id=parent_id,
            decision=Decision.REJECTED,
            decision_reason="no_pref_improvement",
            candidate_text=f"v{i}",
            edit_summary="",
            editor_prompt_hash="",
            editor_model="",
            aggregate_score=3.0,
            pref_delta=-0.1,
            pref_ci_low=-0.3,
            pref_ci_high=0.1,
            reviewer_outputs=[_make_review("r1", 1.0, PairwisePref.INCUMBENT)],
        )
    state = store.resume_state()
    assert state == [(task_id, 2)]


# ---- fact-check (FR6) -----------------------------------------------------


def test_save_and_get_fact_check_report(store: Store):
    article_id, version_id, task_id = _bootstrap_article_and_task(store)
    report = FactCheckReport(
        status=FactCheckStatus.HAS_FINDINGS,
        claims=[
            FactCheckClaim(
                text="X grew 50% in 2024",
                verdict=ClaimVerdict.UNVERIFIED,
                sources=[],
                rationale="no source found",
            ),
            FactCheckClaim(
                text="Y is a public company",
                verdict=ClaimVerdict.VERIFIED,
                sources=["https://example.com/sec"],
                rationale="confirmed via SEC filing",
            ),
        ],
        prompt_hash="fh",
        model="gpt-5",
    )
    store.save_fact_check(task_id=task_id, final_version_id=version_id, report=report)
    got = store.get_fact_check(task_id)
    assert got is not None
    assert got.status is FactCheckStatus.HAS_FINDINGS
    assert len(got.claims) == 2
    verdicts = {c.verdict for c in got.claims}
    assert verdicts == {ClaimVerdict.UNVERIFIED, ClaimVerdict.VERIFIED}


# ---- lineage read order (FR13) -------------------------------------------


def test_lineage_returned_in_iter_index_order(store: Store):
    article_id, parent_id, task_id = _bootstrap_article_and_task(store)
    # Insert out of order to make sure ordering comes from iter_index, not insert time.
    for i in [2, 0, 1]:
        store.record_iteration(
            task_id=task_id,
            article_id=article_id,
            iter_index=i,
            parent_version_id=parent_id,
            decision=Decision.REJECTED,
            decision_reason="no_pref_improvement",
            candidate_text=f"v{i}",
            edit_summary="",
            editor_prompt_hash="",
            editor_model="",
            aggregate_score=float(i),
            pref_delta=0.0,
            pref_ci_low=-0.1,
            pref_ci_high=0.1,
            reviewer_outputs=[_make_review("r1", 1.0, PairwisePref.TIE)],
        )
    lineage = store.get_lineage(task_id)
    assert [r.iter_index for r in lineage] == [0, 1, 2]


# ---- M4.1 hardening tests --------------------------------------------------


def test_lineage_preserves_reviewer_weight(store: Store):
    """Regression: get_lineage previously hardcoded weight=0.0."""
    article_id, parent_id, task_id = _bootstrap_article_and_task(store)
    store.record_iteration(
        task_id=task_id,
        article_id=article_id,
        iter_index=0,
        parent_version_id=parent_id,
        decision=Decision.KEPT,
        decision_reason="ok",
        candidate_text="v",
        edit_summary="",
        editor_prompt_hash="",
        editor_model="",
        aggregate_score=4.0,
        pref_delta=0.3,
        pref_ci_low=0.1,
        pref_ci_high=0.5,
        reviewer_outputs=[
            _make_review("investor", 0.4, PairwisePref.CANDIDATE),
            _make_review("engineer", 0.3, PairwisePref.CANDIDATE),
            _make_review("vp", 0.3, PairwisePref.TIE),
        ],
    )
    lineage = store.get_lineage(task_id)
    assert len(lineage) == 1
    weights = {ro.reviewer_id: ro.weight for ro in lineage[0].reviewer_outputs}
    assert weights == {"investor": 0.4, "engineer": 0.3, "vp": 0.3}


def test_lineage_preserves_distinct_rubric_and_pairwise_models(store: Store):
    """Regression: rubric_model and pairwise_model were collapsed into one column."""
    article_id, parent_id, task_id = _bootstrap_article_and_task(store)
    store.record_iteration(
        task_id=task_id,
        article_id=article_id,
        iter_index=0,
        parent_version_id=parent_id,
        decision=Decision.KEPT,
        decision_reason="ok",
        candidate_text="v",
        edit_summary="",
        editor_prompt_hash="",
        editor_model="",
        aggregate_score=4.0,
        pref_delta=0.3,
        pref_ci_low=0.1,
        pref_ci_high=0.5,
        reviewer_outputs=[
            _make_review(
                "r1",
                1.0,
                PairwisePref.CANDIDATE,
                rubric_model="claude-sonnet-4-6",
                pairwise_model="gpt-5",
            ),
        ],
    )
    rec = store.get_lineage(task_id)[0]
    ro = rec.reviewer_outputs[0]
    assert ro.rubric.model == "claude-sonnet-4-6"
    assert ro.pairwise.model == "gpt-5"


def test_record_iteration_is_idempotent_on_retry(store: Store):
    """FR9: re-calling record_iteration with the same idempotency_key
    returns the original (iteration_id, candidate_id) without writing a
    second record. Models the orchestrator-crash-then-retry path.
    """
    article_id, parent_id, task_id = _bootstrap_article_and_task(store)
    iter1, cand1 = store.record_iteration(
        task_id=task_id,
        article_id=article_id,
        iter_index=0,
        parent_version_id=parent_id,
        decision=Decision.KEPT,
        decision_reason="ok",
        candidate_text="v0",
        edit_summary="",
        editor_prompt_hash="",
        editor_model="",
        aggregate_score=3.0,
        pref_delta=0.1,
        pref_ci_low=0.05,
        pref_ci_high=0.3,
        reviewer_outputs=[_make_review("r1", 1.0, PairwisePref.CANDIDATE)],
        idempotency_key="retry-key",
    )
    assert len(store.get_lineage(task_id)) == 1

    # Retry with the same key — note differing iter_index and content — must
    # return the original IDs and NOT create a second iteration.
    iter2, cand2 = store.record_iteration(
        task_id=task_id,
        article_id=article_id,
        iter_index=99,            # would-be different
        parent_version_id=parent_id,
        decision=Decision.REJECTED,  # would-be different
        decision_reason="anything",
        candidate_text="totally different",
        edit_summary="",
        editor_prompt_hash="",
        editor_model="",
        aggregate_score=4.0,
        pref_delta=0.3,
        pref_ci_low=0.1,
        pref_ci_high=0.5,
        reviewer_outputs=[_make_review("r1", 1.0, PairwisePref.CANDIDATE)],
        idempotency_key="retry-key",
    )
    assert iter2 == iter1
    assert cand2 == cand1
    lineage = store.get_lineage(task_id)
    assert len(lineage) == 1
    assert lineage[0].iter_index == 0
    assert lineage[0].decision is Decision.KEPT


def test_save_fact_check_is_idempotent_per_task(store: Store):
    """FR6: 'exactly once'. A retry with a new report does not overwrite
    the original; the existing report_id is returned.
    """
    article_id, version_id, task_id = _bootstrap_article_and_task(store)
    r1 = FactCheckReport(
        status=FactCheckStatus.CLEAN, claims=[], prompt_hash="h1", model="m1"
    )
    r2 = FactCheckReport(
        status=FactCheckStatus.HAS_FINDINGS,
        claims=[
            FactCheckClaim(
                text="X", verdict=ClaimVerdict.UNVERIFIED, sources=[], rationale=""
            )
        ],
        prompt_hash="h2",
        model="m2",
    )
    id1 = store.save_fact_check(task_id=task_id, final_version_id=version_id, report=r1)
    id2 = store.save_fact_check(task_id=task_id, final_version_id=version_id, report=r2)
    assert id1 == id2
    got = store.get_fact_check(task_id)
    # First report's content is preserved; the retry was a no-op.
    assert got is not None
    assert got.status is FactCheckStatus.CLEAN
    assert got.model == "m1"


def test_consume_already_consumed_note_raises(store: Store):
    """Programming-error guard: passing the same note_id twice raises
    inside the iteration tx and rolls everything back, instead of
    silently no-op-ing."""
    article_id, parent_id, task_id = _bootstrap_article_and_task(store)
    note_id = store.add_human_note(task_id, "rephrase intro")

    # First iteration consumes the note successfully.
    store.record_iteration(
        task_id=task_id,
        article_id=article_id,
        iter_index=0,
        parent_version_id=parent_id,
        decision=Decision.KEPT,
        decision_reason="ok",
        candidate_text="v",
        edit_summary="",
        editor_prompt_hash="",
        editor_model="",
        aggregate_score=4.0,
        pref_delta=0.3,
        pref_ci_low=0.1,
        pref_ci_high=0.5,
        reviewer_outputs=[_make_review("r1", 1.0, PairwisePref.CANDIDATE)],
        consume_note_ids=[note_id],
    )
    assert store.pending_notes(task_id) == []

    # A second iteration that tries to consume the same note must raise
    # AND not write the iteration record.
    with pytest.raises(ValueError, match="already consumed"):
        store.record_iteration(
            task_id=task_id,
            article_id=article_id,
            iter_index=1,
            parent_version_id=parent_id,
            decision=Decision.KEPT,
            decision_reason="ok",
            candidate_text="v2",
            edit_summary="",
            editor_prompt_hash="",
            editor_model="",
            aggregate_score=4.0,
            pref_delta=0.3,
            pref_ci_low=0.1,
            pref_ci_high=0.5,
            reviewer_outputs=[_make_review("r1", 1.0, PairwisePref.CANDIDATE)],
            consume_note_ids=[note_id],
        )
    # Iter 1 must not exist; the rollback was clean.
    lineage = store.get_lineage(task_id)
    assert [r.iter_index for r in lineage] == [0]


def test_concurrent_writes_to_different_tasks_dont_interfere(tmp_path: Path):
    """FR10: two tasks running concurrently must not corrupt each other.

    Models the cross-process case (each tenant/process has its own Store
    pointing at the same DB file). SQLite WAL + busy_timeout handles the
    write serialization. Two threads each own a Store and write to a
    distinct task; we verify each task's lineage is intact.
    """
    import threading

    db_path = tmp_path / "g.db"
    content_root = tmp_path / "c"

    # Bootstrap shared schema + two articles + two tasks via a setup Store.
    setup = Store(db_path=db_path, content_root=content_root)
    art_a, ver_a = setup.create_article(slug="a", content="draft a")
    art_b, ver_b = setup.create_article(slug="b", content="draft b")
    task_a = setup.create_task(
        TaskConfig(
            article_id=art_a,
            reviewers=[ReviewerSpec("r", 1.0)],
            target_aggregate=4.0,
            reviewer_floor=3.0,
            loop_limit=10,
        )
    )
    task_b = setup.create_task(
        TaskConfig(
            article_id=art_b,
            reviewers=[ReviewerSpec("r", 1.0)],
            target_aggregate=4.0,
            reviewer_floor=3.0,
            loop_limit=10,
        )
    )
    setup.mark_task_running(task_a)
    setup.mark_task_running(task_b)
    setup.close()

    N = 5
    errors: list[Exception] = []

    def writer(article_id: str, parent_id: str, task_id: str) -> None:
        store = Store(db_path=db_path, content_root=content_root)
        try:
            for i in range(N):
                store.record_iteration(
                    task_id=task_id,
                    article_id=article_id,
                    iter_index=i,
                    parent_version_id=parent_id,
                    decision=Decision.REJECTED,
                    decision_reason="no_pref_improvement",
                    candidate_text=f"{task_id}-v{i}",
                    edit_summary="",
                    editor_prompt_hash="",
                    editor_model="",
                    aggregate_score=3.0,
                    pref_delta=-0.1,
                    pref_ci_low=-0.3,
                    pref_ci_high=0.1,
                    reviewer_outputs=[
                        _make_review("r", 1.0, PairwisePref.INCUMBENT)
                    ],
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            store.close()

    t1 = threading.Thread(target=writer, args=(art_a, ver_a, task_a))
    t2 = threading.Thread(target=writer, args=(art_b, ver_b, task_b))
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert errors == [], f"writer threads failed: {errors}"

    verifier = Store(db_path=db_path, content_root=content_root)
    try:
        a_lineage = verifier.get_lineage(task_a)
        b_lineage = verifier.get_lineage(task_b)
        assert [r.iter_index for r in a_lineage] == list(range(N))
        assert [r.iter_index for r in b_lineage] == list(range(N))
        for r in a_lineage:
            rej = verifier.get_rejected(r.candidate_id)
            assert verifier.content.read(rej["content_path"]).startswith(task_a)
        for r in b_lineage:
            rej = verifier.get_rejected(r.candidate_id)
            assert verifier.content.read(rej["content_path"]).startswith(task_b)
    finally:
        verifier.close()
