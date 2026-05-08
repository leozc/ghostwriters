"""Acceptance tests A1-A6 from docs/v1-requirements.md.

These are end-to-end scenarios that prove the v1 surface meets the
documented user-facing contract. A1/A2/A3/A6 go through the HTTP API
(the production entry point); A4/A5 drive the Orchestrator directly so
we can express true async concurrency / mid-flight crashes that the
sync TestClient can't.

  A1 Author creates a task, runs the loop, receives final draft +
     lineage + fact-check report.
  A2 Lineage view exposes kept versions AND rejected candidates with
     reasons.
  A3 Limit: loop_limit=N implies at most N iterations.
  A4 Concurrency: two tasks on different articles complete without
     interference.
  A5 Crash: failure mid-iteration leaves no half-kept state; restart
     resumes cleanly.
  A6 Stop: a task that hits target+floor halts before loop_limit.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _helpers import DIMS, _make_orch, make_handler
from ghostwriter.adapters.editor import EditorConfig
from ghostwriter.adapters.fact_checker import FactCheckerConfig
from ghostwriter.adapters.provider import FakeProvider
from ghostwriter.adapters.reviewer import ReviewerConfig
from ghostwriter.api import create_app
from ghostwriter.orchestrator import Orchestrator, OrchestratorConfig
from ghostwriter.store.sqlite import Store
from ghostwriter.types import (
    ReviewerSpec,
    StopReason,
    TaskConfig,
    TaskStatus,
)

# --- shared scaffolding ----------------------------------------------------


def _build_app(
    tmp_path: Path,
    *,
    handler,
    fact_check_status: str = "clean",
    fact_check_claims: list[dict] | None = None,
):
    fact_check_claims = fact_check_claims or []

    def wrapped(model: str, system: str, user: str) -> str:
        if "fact-checker" in system:
            return json.dumps(
                {"status": fact_check_status, "claims": fact_check_claims}
            )
        return handler(model, system, user)

    store = Store(db_path=tmp_path / "g.db", content_root=tmp_path / "content")
    reviewers = {
        rid: ReviewerConfig(
            reviewer_id=rid,
            weight=1.0,
            rubric_dims=DIMS,
            rubric_model="rev-rubric",
            pairwise_model="rev-pair",
        )
        for rid in ("r1", "r2", "r3")
    }
    config = OrchestratorConfig(
        editor=EditorConfig(model="ed"),
        reviewers=reviewers,
        fact_checker=FactCheckerConfig(model="fc"),
    )
    orch = Orchestrator(
        store=store, provider=FakeProvider(handler=wrapped), config=config
    )
    return TestClient(create_app(store=store, orchestrator=orch)), store, orch


# =============================================================================
# A1 — full happy path
# =============================================================================


def test_A1_author_creates_task_runs_loop_gets_full_outputs(tmp_path: Path):
    """Author creates an article, opens a task, runs iterate, then reads
    back: status DONE, final version exists, lineage is non-empty,
    fact-check report is available."""
    handler = make_handler(default_pref="candidate")
    client, _store, _orch = _build_app(
        tmp_path,
        handler=handler,
        fact_check_status="has_findings",
        fact_check_claims=[
            {
                "text": "Ghostwriters originated in 1960.",
                "verdict": "unverified",
                "sources": [],
                "rationale": "no source",
            }
        ],
    )

    art = client.post(
        "/articles", json={"slug": "demo", "content": "# Demo\n\nv0 body"}
    ).json()
    article_id = art["article_id"]

    task = client.post(
        "/tasks",
        json={
            "article_id": article_id,
            "reviewers": [
                {"reviewer_id": "r1", "weight": 1.0},
                {"reviewer_id": "r2", "weight": 1.0},
                {"reviewer_id": "r3", "weight": 1.0},
            ],
            "target_aggregate": 4.0,
            "reviewer_floor": 3.0,
            "loop_limit": 5,
            "must_do_text": "be precise",
            "must_not_do_text": "no fluff",
        },
    ).json()
    task_id = task["task_id"]

    iterate = client.post(f"/tasks/{task_id}/iterate", json={}).json()
    assert iterate["stop_reason"] == "target_reached"

    state = client.get(f"/tasks/{task_id}").json()
    assert state["status"] == "done"
    assert state["final_version_id"] is not None

    final_article = client.get(f"/articles/{article_id}").json()
    assert final_article["incumbent_version_id"] == state["final_version_id"]

    lineage = client.get(f"/tasks/{task_id}/lineage").json()
    assert len(lineage["iterations"]) >= 1
    assert all(
        i["decision"] in {"kept", "rejected"} for i in lineage["iterations"]
    )

    report = client.get(f"/tasks/{task_id}/fact-check-report").json()
    assert report["status"] == "has_findings"
    assert len(report["claims"]) == 1


# =============================================================================
# A2 — lineage exposes both kept and rejected with reasons
# =============================================================================


def test_A2_lineage_shows_kept_and_rejected_with_reasons(tmp_path: Path):
    """Iter 0: split panel => REJECTED. Iter 1: unanimous => KEPT.
    GET /lineage must return both with their decision_reason strings."""
    state = {"i": 0}

    def handler(model: str, system: str, user: str) -> str:
        if "copy editor" in system:
            return json.dumps({"text": f"v{state['i']}", "summary": "tweak"})
        if "fact-checker" in system:
            return json.dumps({"status": "clean", "claims": []})
        # Find which reviewer is being asked
        reviewer_id = None
        for line in user.splitlines():
            if line.startswith("Reviewer id:"):
                reviewer_id = line.split(":", 1)[1].strip()
                break
        if "Score the article" in system:
            return json.dumps(
                {"scores": {"clarity": 4, "accuracy": 4, "voice": 4}, "rationale": "x"}
            )
        # Pairwise: iter 0 split (r1=cand, r2=inc, r3=tie); iter 1 unanimous cand
        if state["i"] == 0:
            pref = {"r1": "candidate", "r2": "incumbent", "r3": "tie"}.get(
                reviewer_id, "tie"
            )
        else:
            pref = "candidate"
        return json.dumps({"pref": pref, "rationale": "x"})

    client, _store, orch = _build_app(tmp_path, handler=handler)

    art = client.post("/articles", json={"slug": "a2", "content": "v0"}).json()
    task = client.post(
        "/tasks",
        json={
            "article_id": art["article_id"],
            "reviewers": [
                {"reviewer_id": "r1", "weight": 1.0},
                {"reviewer_id": "r2", "weight": 1.0},
                {"reviewer_id": "r3", "weight": 1.0},
            ],
            "target_aggregate": 4.0,
            "reviewer_floor": 3.0,
            "loop_limit": 3,
        },
    ).json()
    task_id = task["task_id"]

    # iter 0: rejected. The orchestrator increments state["i"] when running
    # the editor call, so we manually advance after the first iteration to
    # change the per-reviewer pairwise behavior.
    client.post(f"/tasks/{task_id}/iterate", json={"max_iterations": 1})
    state["i"] = 1
    client.post(f"/tasks/{task_id}/iterate", json={})

    lineage = client.get(f"/tasks/{task_id}/lineage").json()
    decisions = [i["decision"] for i in lineage["iterations"]]
    reasons = [i["decision_reason"] for i in lineage["iterations"]]
    assert "rejected" in decisions
    assert "kept" in decisions
    # decision_reason must be non-empty for each
    assert all(r for r in reasons)


# =============================================================================
# A3 — loop_limit caps iterations
# =============================================================================


def test_A3_loop_limit_caps_iterations(tmp_path: Path):
    """With incumbent-preferring reviewers and loop_limit=4, we record
    exactly 4 iteration_records and stop_reason=loop_limit."""
    handler = make_handler(
        default_pref="incumbent",
        default_scores={"clarity": 3, "accuracy": 3, "voice": 3},
    )
    client, store, _orch = _build_app(tmp_path, handler=handler)

    art = client.post("/articles", json={"slug": "a3", "content": "v0"}).json()
    task = client.post(
        "/tasks",
        json={
            "article_id": art["article_id"],
            "reviewers": [
                {"reviewer_id": "r1", "weight": 1.0},
                {"reviewer_id": "r2", "weight": 1.0},
                {"reviewer_id": "r3", "weight": 1.0},
            ],
            "target_aggregate": 5.0,
            "reviewer_floor": 5.0,
            "loop_limit": 4,
        },
    ).json()
    task_id = task["task_id"]

    out = client.post(f"/tasks/{task_id}/iterate", json={}).json()
    assert out["stop_reason"] == "loop_limit"
    assert out["iterations_run"] == 4

    lineage = client.get(f"/tasks/{task_id}/lineage").json()
    assert len(lineage["iterations"]) == 4
    indices = [i["iter_index"] for i in lineage["iterations"]]
    assert indices == [0, 1, 2, 3]


# =============================================================================
# A4 — concurrency
# =============================================================================


async def test_A4_two_tasks_on_different_articles_complete_independently(
    tmp_path: Path,
):
    """Two tasks on two different articles. Run their iterate() loops
    concurrently. Both complete; their lineages do not bleed into each
    other; per-task aggregates differ when their reviewer behavior
    differs."""
    # Two separate orchestrators sharing one Store, mirroring how an HTTP
    # server would handle concurrent requests on a shared connection pool.
    store = Store(db_path=tmp_path / "g.db", content_root=tmp_path / "content")

    # task A: target reached (unanimous candidate, scores 4)
    handler_a = make_handler(
        default_pref="candidate", default_scores={"clarity": 4, "accuracy": 4, "voice": 4}
    )
    # task B: target reached too but distinguishable scores (5)
    handler_b = make_handler(
        default_pref="candidate", default_scores={"clarity": 5, "accuracy": 5, "voice": 5}
    )

    def reviewer_cfgs():
        return {
            rid: ReviewerConfig(
                reviewer_id=rid,
                weight=1.0,
                rubric_dims=DIMS,
                rubric_model="rev-rubric",
                pairwise_model="rev-pair",
            )
            for rid in ("r1", "r2", "r3")
        }

    config_a = OrchestratorConfig(
        editor=EditorConfig(model="ed"), reviewers=reviewer_cfgs()
    )
    config_b = OrchestratorConfig(
        editor=EditorConfig(model="ed"), reviewers=reviewer_cfgs()
    )

    orch_a = Orchestrator(
        store=store, provider=FakeProvider(handler=handler_a), config=config_a
    )
    orch_b = Orchestrator(
        store=store, provider=FakeProvider(handler=handler_b), config=config_b
    )

    art_a, _ = store.create_article(slug="A", content="vA0")
    art_b, _ = store.create_article(slug="B", content="vB0")

    def cfg(article_id):
        return TaskConfig(
            article_id=article_id,
            reviewers=[ReviewerSpec(reviewer_id=k, weight=1.0) for k in ("r1", "r2", "r3")],
            target_aggregate=4.0,
            reviewer_floor=3.0,
            loop_limit=3,
        )

    task_a = store.create_task(cfg(art_a))
    task_b = store.create_task(cfg(art_b))

    res_a, res_b = await asyncio.gather(
        orch_a.iterate(task_a), orch_b.iterate(task_b)
    )

    assert res_a.stop_reason is StopReason.TARGET_REACHED
    assert res_b.stop_reason is StopReason.TARGET_REACHED

    # Lineages are independent and disjoint.
    lin_a = store.get_lineage(task_a)
    lin_b = store.get_lineage(task_b)
    assert {r.task_id for r in lin_a} == {task_a}
    assert {r.task_id for r in lin_b} == {task_b}

    # Aggregates reflect per-task reviewer behavior.
    assert lin_a[-1].aggregate_score == pytest.approx(4.0)
    assert lin_b[-1].aggregate_score == pytest.approx(5.0)


# =============================================================================
# A5 — crash mid-iteration leaves no half-kept state
# =============================================================================


async def test_A5_crash_mid_iteration_leaves_no_half_kept_state(tmp_path: Path):
    """A failure inside _run_one (here: provider raises during a reviewer
    call) must not leave a phantom iteration_records row, must not
    advance article_versions, must not consume notes. A fresh
    orchestrator on the same Store must run iter_index=0 cleanly.
    """
    article_calls = {"editor": 0}

    def crashing_handler(model: str, system: str, user: str) -> str:
        if "copy editor" in system:
            article_calls["editor"] += 1
            return json.dumps({"text": f"v{article_calls['editor']}", "summary": "x"})
        if "Score the article" in system:
            # Crash on the very first rubric call.
            raise RuntimeError("simulated network failure mid-iteration")
        if "comparing two versions" in system:
            return json.dumps({"pref": "candidate", "rationale": "x"})
        if "fact-checker" in system:
            return json.dumps({"status": "clean", "claims": []})
        raise AssertionError(system)

    orch_crash, store, task_id, article_id = _make_orch(
        tmp_path, handler=crashing_handler, loop_limit=3
    )
    note_id = store.add_human_note(task_id, "this note must survive the crash")

    with pytest.raises(RuntimeError, match="simulated"):
        await orch_crash.iterate(task_id)

    # Crash invariants:
    assert store.get_lineage(task_id) == []        # no iteration row
    latest = store.latest_version(article_id)
    assert latest["parent_version_id"] is None    # incumbent unchanged
    assert any(n["id"] == note_id for n in store.pending_notes(task_id))  # note survives
    task = store.get_task(task_id)
    assert task["status"] == TaskStatus.RUNNING.value  # still resumable

    # Restart: a brand-new orchestrator (different provider, no shared
    # in-memory state) on the same Store must succeed from iter_index=0.
    healthy_handler = make_handler(default_pref="candidate")
    orch_healthy = Orchestrator(
        store=store,
        provider=FakeProvider(handler=healthy_handler),
        config=orch_crash.config,
    )
    result = await orch_healthy.iterate(task_id)
    assert result.stop_reason is StopReason.TARGET_REACHED

    lineage = store.get_lineage(task_id)
    assert [r.iter_index for r in lineage] == list(range(len(lineage)))
    assert lineage[0].iter_index == 0
    # The previously-pending note was consumed by the resumed iteration.
    assert store.pending_notes(task_id) == []


# =============================================================================
# A6 — natural stop before loop_limit
# =============================================================================


def test_A6_target_reached_halts_before_loop_limit(tmp_path: Path):
    """target_aggregate=4.0, floor=3.0, loop_limit=10. Unanimous-candidate
    panel hits target on iter 0 => stop_reason=target_reached, exactly 1
    iteration recorded."""
    handler = make_handler(
        default_pref="candidate",
        default_scores={"clarity": 4, "accuracy": 4, "voice": 4},
    )
    client, _store, _orch = _build_app(tmp_path, handler=handler)

    art = client.post("/articles", json={"slug": "a6", "content": "v0"}).json()
    task = client.post(
        "/tasks",
        json={
            "article_id": art["article_id"],
            "reviewers": [
                {"reviewer_id": "r1", "weight": 1.0},
                {"reviewer_id": "r2", "weight": 1.0},
                {"reviewer_id": "r3", "weight": 1.0},
            ],
            "target_aggregate": 4.0,
            "reviewer_floor": 3.0,
            "loop_limit": 10,
        },
    ).json()
    task_id = task["task_id"]

    out = client.post(f"/tasks/{task_id}/iterate", json={}).json()
    assert out["stop_reason"] == "target_reached"
    assert out["iterations_run"] == 1

    lineage = client.get(f"/tasks/{task_id}/lineage").json()
    assert len(lineage["iterations"]) == 1  # halted well before loop_limit=10
    assert lineage["iterations"][0]["decision"] == "kept"
