"""End-to-end orchestrator tests using FakeProvider.

The handler factory below routes by adapter system-prompt fingerprint, so
each test can script editor/rubric/pairwise responses independently. We
exercise: target-reached termination, loop-limit termination, rejection,
floor blocking, FR11 zero-weight skip, FR5 one-shot notes, abort,
crash/retry idempotency, and per-task lock serialization.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from _helpers import _make_orch, make_handler
from ghostwriter.adapters.provider import FakeProvider
from ghostwriter.orchestrator import (
    Orchestrator,
    _seed_for,
)
from ghostwriter.types import (
    Decision,
    ReviewerSpec,
    StopReason,
    TaskConfig,
    TaskStatus,
)

# =============================================================================
# Happy paths
# =============================================================================


async def test_target_reached_in_one_iteration(tmp_path: Path):
    """Unanimous CANDIDATE pref + scores at target => one KEPT iteration,
    TARGET_REACHED, task DONE, lineage has one entry."""
    handler = make_handler(default_pref="candidate", default_scores={"clarity": 4, "accuracy": 4, "voice": 4})
    orch, store, task_id, article_id = _make_orch(tmp_path, handler=handler)

    result = await orch.iterate(task_id)

    assert result.iterations_run == 1
    assert result.stop_reason is StopReason.TARGET_REACHED
    assert result.final_aggregate == pytest.approx(4.0)

    lineage = store.get_lineage(task_id)
    assert len(lineage) == 1
    assert lineage[0].decision is Decision.KEPT
    assert lineage[0].iter_index == 0

    task = store.get_task(task_id)
    assert task["status"] == TaskStatus.DONE.value
    assert task["stop_reason"] == StopReason.TARGET_REACHED.value
    assert task["final_version_id"] is not None


async def test_loop_limit_terminates_when_target_unreachable(tmp_path: Path):
    """Mediocre scores never hit target=4.0 + most reviewers prefer
    incumbent so iterations get rejected; we run loop_limit attempts then
    stop with LOOP_LIMIT."""
    handler = make_handler(
        default_pref="incumbent",
        default_scores={"clarity": 3, "accuracy": 3, "voice": 3},
    )
    orch, store, task_id, _ = _make_orch(tmp_path, handler=handler, loop_limit=3)

    result = await orch.iterate(task_id)
    assert result.iterations_run == 3
    assert result.stop_reason is StopReason.LOOP_LIMIT
    assert all(r.decision is Decision.REJECTED for r in store.get_lineage(task_id))


async def test_rejection_keeps_incumbent(tmp_path: Path):
    """Split panel => ci_low <= 0 => REJECTED. Article version count
    must not grow; iter_index still advances."""
    handler = make_handler(
        per_reviewer_pref={"r1": "candidate", "r2": "incumbent", "r3": "tie"},
        default_scores={"clarity": 4, "accuracy": 4, "voice": 4},
    )
    orch, store, task_id, article_id = _make_orch(
        tmp_path, handler=handler, loop_limit=2
    )

    await orch.iterate(task_id)

    lineage = store.get_lineage(task_id)
    assert all(r.decision is Decision.REJECTED for r in lineage)
    # Article still has only the original version.
    assert store.latest_version(article_id)["parent_version_id"] is None


async def test_kept_below_target_keeps_iterating(tmp_path: Path):
    """KEPT iteration whose aggregate is below target should NOT terminate
    with TARGET_REACHED. With only loop_limit=1 we'll hit LOOP_LIMIT."""
    handler = make_handler(
        default_pref="candidate",
        default_scores={"clarity": 3, "accuracy": 3, "voice": 4},  # avg 3.33
    )
    orch, store, task_id, _ = _make_orch(
        tmp_path, handler=handler, target=4.0, loop_limit=1
    )

    result = await orch.iterate(task_id)
    assert result.stop_reason is StopReason.LOOP_LIMIT
    lineage = store.get_lineage(task_id)
    assert len(lineage) == 1
    assert lineage[0].decision is Decision.KEPT
    assert lineage[0].aggregate_score < 4.0


async def test_floor_blocks_termination_even_when_aggregate_clears(tmp_path: Path):
    """Aggregate clears target but one reviewer is below floor => keep going."""
    handler = make_handler(
        default_pref="candidate",
        per_reviewer_scores={
            "r1": {"clarity": 5, "accuracy": 5, "voice": 5},   # 5.0
            "r2": {"clarity": 5, "accuracy": 5, "voice": 5},   # 5.0
            "r3": {"clarity": 2, "accuracy": 2, "voice": 2},   # 2.0 — below floor
        },
    )
    orch, store, task_id, _ = _make_orch(
        tmp_path, handler=handler, target=3.5, floor=3.0, loop_limit=2
    )

    result = await orch.iterate(task_id)
    # Aggregate = (5+5+2)/3 = 4.0 ≥ 3.5, but r3=2.0 < floor=3.0 => continue.
    assert result.stop_reason is StopReason.LOOP_LIMIT
    assert result.iterations_run == 2


# =============================================================================
# FR11 zero-weight reviewers
# =============================================================================


async def test_zero_weight_reviewer_not_called_and_not_in_outputs(tmp_path: Path):
    """A reviewer with weight=0 must not be called by the provider and
    must not appear in iteration_records.reviewer_outputs."""
    handler = make_handler(default_pref="candidate")
    orch, store, task_id, _ = _make_orch(
        tmp_path,
        handler=handler,
        weights={"r1": 1.0, "r2": 0.0, "r3": 1.0},
    )

    await orch.iterate(task_id)

    lineage = store.get_lineage(task_id)
    rids = {ro.reviewer_id for ro in lineage[0].reviewer_outputs}
    assert rids == {"r1", "r3"}

    fake: FakeProvider = orch.provider  # type: ignore[assignment]
    seen_rids = set()
    for c in fake.calls:
        for line in c.user.splitlines():
            if line.startswith("Reviewer id:"):
                seen_rids.add(line.split(":", 1)[1].strip())
    assert "r2" not in seen_rids


# =============================================================================
# Notes (FR5)
# =============================================================================


async def test_human_note_consumed_after_one_iteration(tmp_path: Path):
    handler = make_handler(default_pref="candidate")
    orch, store, task_id, _ = _make_orch(tmp_path, handler=handler, loop_limit=2, target=999)
    note_id = store.add_human_note(task_id, "tighten the lede")

    await orch.iterate(task_id, max_iterations=1)
    assert store.pending_notes(task_id) == []
    note_row = store._conn.execute(
        "SELECT consumed_at FROM human_notes WHERE id = ?", (note_id,)
    ).fetchone()
    assert note_row["consumed_at"] is not None


async def test_human_note_appears_in_editor_prompt(tmp_path: Path):
    captured = {}

    def handler(model: str, system: str, user: str) -> str:
        if "copy editor" in system:
            captured.setdefault("editor_user", user)
            return json.dumps({"text": "x", "summary": ""})
        if "Score the article" in system:
            return json.dumps(
                {"scores": {"clarity": 3, "accuracy": 3, "voice": 3}, "rationale": "x"}
            )
        if "comparing two versions" in system:
            return json.dumps({"pref": "tie", "rationale": "x"})
        raise AssertionError

    orch, store, task_id, _ = _make_orch(tmp_path, handler=handler, loop_limit=1)
    store.add_human_note(task_id, "FOLLOW THIS DIRECTIVE")

    await orch.iterate(task_id, max_iterations=1)
    assert "FOLLOW THIS DIRECTIVE" in captured["editor_user"]


# =============================================================================
# Abort
# =============================================================================


async def test_abort_marks_task_and_iterate_returns_aborted(tmp_path: Path):
    handler = make_handler(
        default_pref="incumbent",
        default_scores={"clarity": 3, "accuracy": 3, "voice": 3},
    )
    orch, store, task_id, _ = _make_orch(tmp_path, handler=handler, loop_limit=10)

    await orch.abort(task_id)
    result = await orch.iterate(task_id)
    assert result.stop_reason is StopReason.ABORTED
    assert result.iterations_run == 0


async def test_abort_during_iteration_exits_at_next_boundary(tmp_path: Path):
    """Abort fires from another coroutine while iterate is in flight.
    The current iteration completes; subsequent iteration loop exits."""
    iter_started = asyncio.Event()
    iter_can_finish = asyncio.Event()

    def handler(model: str, system: str, user: str) -> str:
        # Block the FIRST editor call until aborter has set the abort flag.
        return _block_then_return(model, system, user)

    pending_calls = {"editor_n": 0}

    def _block_then_return(model: str, system: str, user: str) -> str:
        if "copy editor" in system:
            pending_calls["editor_n"] += 1
            if pending_calls["editor_n"] == 1:
                iter_started.set()
                # We can't await inside sync handler; instead use the async
                # variant below.
            return json.dumps({"text": "x", "summary": ""})
        if "Score the article" in system:
            return json.dumps(
                {"scores": {"clarity": 3, "accuracy": 3, "voice": 3}, "rationale": "x"}
            )
        return json.dumps({"pref": "incumbent", "rationale": "x"})

    async def async_handler(model: str, system: str, user: str) -> str:
        if "copy editor" in system:
            pending_calls["editor_n"] += 1
            if pending_calls["editor_n"] == 1:
                iter_started.set()
                await iter_can_finish.wait()
            return json.dumps({"text": "x", "summary": ""})
        if "Score the article" in system:
            return json.dumps(
                {"scores": {"clarity": 3, "accuracy": 3, "voice": 3}, "rationale": "x"}
            )
        return json.dumps({"pref": "incumbent", "rationale": "x"})

    orch, store, task_id, _ = _make_orch(tmp_path, handler=async_handler, loop_limit=10)

    iterate_task = asyncio.create_task(orch.iterate(task_id))
    await iter_started.wait()
    await orch.abort(task_id)
    iter_can_finish.set()
    result = await iterate_task

    # First iteration completed (we let it finish), then the loop saw
    # ABORTED and exited. iterations_run == 1.
    assert result.stop_reason is StopReason.ABORTED
    assert result.iterations_run == 1


# =============================================================================
# Idempotency / restart (FR9, FR12)
# =============================================================================


async def test_resume_after_partial_progress(tmp_path: Path):
    """Run 2 iterations, then call iterate again on the same task. The
    new call should pick up at iter_index=2, not redo the earlier work."""
    handler = make_handler(
        default_pref="incumbent",
        default_scores={"clarity": 3, "accuracy": 3, "voice": 3},
    )
    orch, store, task_id, _ = _make_orch(tmp_path, handler=handler, loop_limit=4)

    r1 = await orch.iterate(task_id, max_iterations=2)
    assert r1.iterations_run == 2
    indices_after_r1 = [r.iter_index for r in store.get_lineage(task_id)]
    assert indices_after_r1 == [0, 1]

    r2 = await orch.iterate(task_id, max_iterations=2)
    assert r2.iterations_run == 2
    indices_after_r2 = [r.iter_index for r in store.get_lineage(task_id)]
    assert indices_after_r2 == [0, 1, 2, 3]


async def test_seed_is_deterministic_per_iteration(tmp_path: Path):
    """Same (task_id, iter_index) => same bootstrap seed => same CI bounds.
    The orchestrator promises this for audit replay."""
    assert _seed_for("t1", 0) == _seed_for("t1", 0)
    assert _seed_for("t1", 0) != _seed_for("t1", 1)
    assert _seed_for("t1", 0) != _seed_for("t2", 0)


# =============================================================================
# Concurrency
# =============================================================================


async def test_concurrent_iterate_calls_on_same_task_serialize(tmp_path: Path):
    """Two coroutines call iterate on the same task. Per-task asyncio
    lock should serialize them; total iterations equal sum of budgets,
    iter_indices are non-overlapping."""
    handler = make_handler(default_pref="incumbent")
    orch, store, task_id, _ = _make_orch(tmp_path, handler=handler, loop_limit=10)

    a, b = await asyncio.gather(
        orch.iterate(task_id, max_iterations=2),
        orch.iterate(task_id, max_iterations=3),
    )
    assert a.iterations_run + b.iterations_run == 5
    indices = sorted(r.iter_index for r in store.get_lineage(task_id))
    assert indices == [0, 1, 2, 3, 4]


async def test_concurrent_iterate_calls_on_different_tasks_run_in_parallel(
    tmp_path: Path,
):
    """Two distinct tasks should not block each other."""
    handler = make_handler(default_pref="candidate")
    orch1, store1, t1, _ = _make_orch(tmp_path / "a", handler=handler, loop_limit=2)
    # Reuse same orchestrator pattern but two stores keep it simple — what
    # we're really testing is that one Orchestrator with two task_ids doesn't
    # share locks.
    orch2 = Orchestrator(
        store=store1, provider=orch1.provider, config=orch1.config
    )
    article_id, _ = store1.create_article(slug="t2", content="another")
    t2 = store1.create_task(
        TaskConfig(
            article_id=article_id,
            reviewers=[ReviewerSpec(reviewer_id=k, weight=1.0) for k in ("r1", "r2", "r3")],
            target_aggregate=4.0,
            reviewer_floor=3.0,
            loop_limit=2,
        )
    )

    a, b = await asyncio.gather(orch1.iterate(t1), orch2.iterate(t2))
    assert a.stop_reason is StopReason.TARGET_REACHED
    assert b.stop_reason is StopReason.TARGET_REACHED


# =============================================================================
# Error paths
# =============================================================================


async def test_iterate_unknown_task_raises(tmp_path: Path):
    orch, *_ = _make_orch(tmp_path, handler=make_handler())
    with pytest.raises(ValueError, match="not found"):
        await orch.iterate("task_does_not_exist")


async def test_no_active_reviewers_raises(tmp_path: Path):
    orch, store, task_id, _ = _make_orch(
        tmp_path, handler=make_handler(), weights={"r1": 0.0, "r2": 0.0}
    )
    # Need to also register r2 in our reviewer config map; _make_orch handles that.
    with pytest.raises(ValueError, match="no reviewers with weight"):
        await orch.iterate(task_id)


async def test_iterate_on_done_task_is_noop(tmp_path: Path):
    handler = make_handler(default_pref="candidate")
    orch, store, task_id, _ = _make_orch(tmp_path, handler=handler)
    await orch.iterate(task_id)  # terminate naturally

    # Second iterate is a no-op.
    result = await orch.iterate(task_id)
    assert result.iterations_run == 0
    assert result.stop_reason is StopReason.TARGET_REACHED
