"""Iteration loop coordinator (FR3, FR4, FR5, FR9, FR10, FR12).

One Orchestrator instance owns one Store + one Provider + a config bundle
(editor + reviewer configs). Each `iterate(task_id)` call runs zero or
more iterations under a per-task asyncio lock; iterations within a task
are serialized, iterations across distinct tasks run concurrently.

Per FR12 each iteration is exactly one Store transaction and the
orchestrator carries no durable in-memory state — restarting the process
replays from the database via `next_iter_index = max(iter_index) + 1`.

Per FR9 the orchestrator derives a deterministic per-iteration
idempotency key (`task_id:iter_index`); a retry of the exact same step
reuses the prior result via Store.record_iteration's fast path. The same
string also seeds bootstrap CI so audit replays produce identical bounds.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

from ghostwriter.adapters.editor import EditorConfig, propose
from ghostwriter.adapters.provider import Provider
from ghostwriter.adapters.reviewer import (
    ReviewerConfig,
    compare_pairwise,
    score_rubric,
)
from ghostwriter.core.bootstrap_ci import bootstrap_pairwise_ci
from ghostwriter.store.sqlite import Store
from ghostwriter.types import (
    Decision,
    EditorInput,
    PairwisePref,
    ReviewerOutput,
    StopReason,
    TaskStatus,
)


# Per-reviewer pairwise score per FR67: P(cand>inc) - 0.5.
def _pref_score(pref: PairwisePref) -> float:
    if pref is PairwisePref.CANDIDATE:
        return 0.5
    if pref is PairwisePref.INCUMBENT:
        return -0.5
    return 0.0


def _seed_for(task_id: str, iter_index: int) -> int:
    """Deterministic int seed for bootstrap CI from (task_id, iter_index).

    Pinned so audit replays of the same lineage compute the same CI bounds.
    """
    h = hashlib.sha256(f"{task_id}:{iter_index}".encode("utf-8")).hexdigest()[:8]
    return int(h, 16)


@dataclass(frozen=True)
class OrchestratorConfig:
    editor: EditorConfig
    reviewers: dict[str, ReviewerConfig]
    n_bootstrap: int = 1000
    confidence: float = 0.90


@dataclass(frozen=True)
class IterateResult:
    """Outcome of a single iterate() call. stop_reason=None means we hit
    `max_iterations` before any natural termination — the task remains
    runnable."""

    iterations_run: int
    stop_reason: StopReason | None
    final_aggregate: float | None


class Orchestrator:
    def __init__(
        self,
        *,
        store: Store,
        provider: Provider,
        config: OrchestratorConfig,
    ) -> None:
        self.store = store
        self.provider = provider
        self.config = config
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    async def _lock_for(self, task_id: str) -> asyncio.Lock:
        async with self._registry_lock:
            lock = self._task_locks.get(task_id)
            if lock is None:
                lock = asyncio.Lock()
                self._task_locks[task_id] = lock
            return lock

    async def iterate(
        self, task_id: str, *, max_iterations: int | None = None
    ) -> IterateResult:
        """Run iterations until natural stop, max_iterations reached, or abort.

        Re-entrant on the same task_id: serialized via per-task asyncio lock.
        """
        lock = await self._lock_for(task_id)
        async with lock:
            return await self._iterate_inner(task_id, max_iterations)

    async def abort(self, task_id: str) -> None:
        """Idempotent. Does NOT take the per-task lock — abort during an
        in-flight iteration is the whole point. The loop checks status
        between iterations and exits cleanly on the next boundary; the
        currently-in-flight iteration completes (its LLM calls don't
        unsubscribe) and is recorded as normal.
        """
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"task {task_id} not found")
        if task["status"] in (TaskStatus.DONE.value, TaskStatus.ABORTED.value):
            return
        final_v = self.store.latest_version(task["article_id"])
        self.store.mark_task_terminal(
            task_id=task_id,
            status=TaskStatus.ABORTED,
            stop_reason=StopReason.ABORTED,
            final_version_id=final_v["id"] if final_v else None,
        )

    # ---- internals --------------------------------------------------------

    async def _iterate_inner(
        self, task_id: str, max_iterations: int | None
    ) -> IterateResult:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"task {task_id} not found")

        if task["status"] in (TaskStatus.DONE.value, TaskStatus.ABORTED.value):
            stop = task.get("stop_reason")
            return IterateResult(
                iterations_run=0,
                stop_reason=StopReason(stop) if stop else None,
                final_aggregate=None,
            )

        if task["status"] == TaskStatus.PENDING.value:
            self.store.mark_task_running(task_id)

        active_reviewers = [
            (r["reviewer_id"], r["weight"])
            for r in task["reviewers"]
            if r["weight"] > 0
        ]
        if not active_reviewers:
            raise ValueError(f"task {task_id} has no reviewers with weight > 0")

        loop_limit = task["loop_limit"]
        target = task["target_aggregate"]
        floor = task["reviewer_floor"]
        article_id = task["article_id"]

        lineage = self.store.get_lineage(task_id)
        next_iter_index = lineage[-1].iter_index + 1 if lineage else 0

        iterations_run = 0
        last_aggregate: float | None = None

        while True:
            # FR5 hard ceiling.
            if next_iter_index >= loop_limit:
                return self._terminate(
                    task_id=task_id,
                    article_id=article_id,
                    stop_reason=StopReason.LOOP_LIMIT,
                    iterations_run=iterations_run,
                    final_aggregate=last_aggregate,
                )

            # Caller-imposed budget on this iterate() call.
            if max_iterations is not None and iterations_run >= max_iterations:
                return IterateResult(
                    iterations_run=iterations_run,
                    stop_reason=None,
                    final_aggregate=last_aggregate,
                )

            # External abort (set via .abort() while we were iterating).
            cur_task = self.store.get_task(task_id)
            if cur_task["status"] == TaskStatus.ABORTED.value:
                return IterateResult(
                    iterations_run=iterations_run,
                    stop_reason=StopReason.ABORTED,
                    final_aggregate=last_aggregate,
                )

            decision, aggregate, per_reviewer_aggregates = await self._run_one(
                task_id=task_id,
                task=cur_task,
                article_id=article_id,
                iter_index=next_iter_index,
                active_reviewers=active_reviewers,
            )
            iterations_run += 1
            next_iter_index += 1
            last_aggregate = aggregate

            # FR5 natural termination: kept iteration whose aggregate clears
            # the target AND every active reviewer clears the floor.
            if decision is Decision.KEPT and aggregate >= target and all(
                a >= floor for a in per_reviewer_aggregates
            ):
                return self._terminate(
                    task_id=task_id,
                    article_id=article_id,
                    stop_reason=StopReason.TARGET_REACHED,
                    iterations_run=iterations_run,
                    final_aggregate=aggregate,
                )

    def _terminate(
        self,
        *,
        task_id: str,
        article_id: str,
        stop_reason: StopReason,
        iterations_run: int,
        final_aggregate: float | None,
    ) -> IterateResult:
        final_v = self.store.latest_version(article_id)
        self.store.mark_task_terminal(
            task_id=task_id,
            status=TaskStatus.DONE,
            stop_reason=stop_reason,
            final_version_id=final_v["id"] if final_v else None,
        )
        return IterateResult(
            iterations_run=iterations_run,
            stop_reason=stop_reason,
            final_aggregate=final_aggregate,
        )

    async def _run_one(
        self,
        *,
        task_id: str,
        task: dict,
        article_id: str,
        iter_index: int,
        active_reviewers: list[tuple[str, float]],
    ) -> tuple[Decision, float, list[float]]:
        incumbent = self.store.latest_version(article_id)
        if incumbent is None:
            raise ValueError(f"article {article_id} has no versions")
        incumbent_text = self.store.read_version_text(incumbent["id"])

        # Per design "Context management": only the most recent iteration's
        # reviewer outputs flow into the next editor prompt. Older feedback
        # is in the lineage but not the prompt.
        lineage = self.store.get_lineage(task_id)
        prior_reviews = lineage[-1].reviewer_outputs if lineage else []

        # FR5 one-shot human notes: collected here, marked consumed inside
        # record_iteration's transaction.
        pending = self.store.pending_notes(task_id)
        notes_text = [n["note_text"] for n in pending]
        note_ids = [n["id"] for n in pending]

        editor_input = EditorInput(
            incumbent_text=incumbent_text,
            prior_reviews=prior_reviews,
            must_do_text=task["must_do_text"] or "",
            must_not_do_text=task["must_not_do_text"] or "",
            human_notes=notes_text,
        )
        candidate = await propose(
            config=self.config.editor,
            editor_input=editor_input,
            provider=self.provider,
        )

        async def review_one(reviewer_id: str, weight: float) -> ReviewerOutput:
            cfg = self.config.reviewers.get(reviewer_id)
            if cfg is None:
                raise ValueError(
                    f"task references reviewer_id={reviewer_id!r} but no "
                    f"ReviewerConfig was provided"
                )
            rubric, pairwise = await asyncio.gather(
                score_rubric(
                    config=cfg,
                    candidate_text=candidate.text,
                    provider=self.provider,
                ),
                compare_pairwise(
                    config=cfg,
                    candidate_text=candidate.text,
                    incumbent_text=incumbent_text,
                    provider=self.provider,
                ),
            )
            return ReviewerOutput(
                reviewer_id=reviewer_id,
                weight=weight,
                rubric=rubric,
                pairwise=pairwise,
            )

        reviewer_outputs = list(
            await asyncio.gather(
                *(review_one(rid, w) for rid, w in active_reviewers)
            )
        )

        total_w = sum(ro.weight for ro in reviewer_outputs)
        aggregate = (
            sum(ro.weight * ro.rubric.aggregate for ro in reviewer_outputs) / total_w
        )
        per_reviewer_aggregates = [ro.rubric.aggregate for ro in reviewer_outputs]

        ci = bootstrap_pairwise_ci(
            scores=[_pref_score(ro.pairwise.pref) for ro in reviewer_outputs],
            weights=[ro.weight for ro in reviewer_outputs],
            n_bootstrap=self.config.n_bootstrap,
            confidence=self.config.confidence,
            seed=_seed_for(task_id, iter_index),
        )

        if ci.ci_low > 0:
            decision = Decision.KEPT
            decision_reason = "pref_ci_low > 0"
        else:
            decision = Decision.REJECTED
            decision_reason = "no_pref_improvement"

        self.store.record_iteration(
            task_id=task_id,
            article_id=article_id,
            iter_index=iter_index,
            parent_version_id=incumbent["id"],
            decision=decision,
            decision_reason=decision_reason,
            candidate_text=candidate.text,
            edit_summary=candidate.edit_summary,
            editor_prompt_hash=candidate.prompt_hash,
            editor_model=candidate.model,
            aggregate_score=aggregate,
            pref_delta=ci.mean,
            pref_ci_low=ci.ci_low,
            pref_ci_high=ci.ci_high,
            reviewer_outputs=reviewer_outputs,
            consume_note_ids=note_ids,
            idempotency_key=f"{task_id}:{iter_index}",
        )

        return decision, aggregate, per_reviewer_aggregates
