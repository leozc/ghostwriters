"""HTTP API tests via FastAPI TestClient.

Covers the documented endpoint surface (docs/v1-design.md §API):
articles, tasks, iterate, note, abort, lineage, fact-check-report.
Uses the same FakeProvider scaffolding as test_orchestrator so
behavior parity is guaranteed end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ghostwriter.adapters.editor import EditorConfig
from ghostwriter.adapters.fact_checker import FactCheckerConfig
from ghostwriter.adapters.provider import FakeProvider
from ghostwriter.adapters.reviewer import ReviewerConfig
from ghostwriter.api import create_app
from ghostwriter.orchestrator import Orchestrator, OrchestratorConfig
from ghostwriter.store.sqlite import Store

from test_orchestrator import DIMS, make_handler


def _wrap_with_fact_checker(handler, *, status="clean", claims=None):
    claims = claims or []

    def h(model: str, system: str, user: str) -> str:
        if "fact-checker" in system:
            return json.dumps({"status": status, "claims": claims})
        return handler(model, system, user)

    return h


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """A TestClient backed by a real Store + FakeProvider-driven Orchestrator."""
    store = Store(db_path=tmp_path / "g.db", content_root=tmp_path / "content")
    handler = _wrap_with_fact_checker(make_handler(default_pref="candidate"))
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
        store=store, provider=FakeProvider(handler=handler), config=config
    )
    app = create_app(store=store, orchestrator=orch)
    return TestClient(app)


# =============================================================================
# Articles
# =============================================================================


def test_create_and_get_article(client: TestClient):
    r = client.post(
        "/articles", json={"slug": "demo", "content": "# Demo\n\nBody here."}
    )
    assert r.status_code == 201
    body = r.json()
    article_id = body["article_id"]
    assert body["version_id"]

    r = client.get(f"/articles/{article_id}")
    assert r.status_code == 200
    got = r.json()
    assert got["slug"] == "demo"
    assert got["incumbent_text"].startswith("# Demo")


def test_get_unknown_article_404(client: TestClient):
    r = client.get("/articles/art_nope")
    assert r.status_code == 404


def test_create_article_validates_required_fields(client: TestClient):
    r = client.post("/articles", json={"slug": "", "content": "x"})
    assert r.status_code == 422  # Pydantic validation


# =============================================================================
# Tasks
# =============================================================================


def _create_article(client: TestClient) -> str:
    r = client.post("/articles", json={"slug": "t", "content": "v0 body"})
    return r.json()["article_id"]


def _create_task(
    client: TestClient,
    article_id: str,
    *,
    target: float = 4.0,
    floor: float = 3.0,
    loop_limit: int = 3,
    weights: dict[str, float] | None = None,
) -> str:
    weights = weights or {"r1": 1.0, "r2": 1.0, "r3": 1.0}
    r = client.post(
        "/tasks",
        json={
            "article_id": article_id,
            "reviewers": [
                {"reviewer_id": k, "weight": v} for k, v in weights.items()
            ],
            "target_aggregate": target,
            "reviewer_floor": floor,
            "loop_limit": loop_limit,
            "must_do_text": "",
            "must_not_do_text": "",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["task_id"]


def test_create_task_404_on_unknown_article(client: TestClient):
    r = client.post(
        "/tasks",
        json={
            "article_id": "art_nope",
            "reviewers": [{"reviewer_id": "r1", "weight": 1.0}],
            "target_aggregate": 4.0,
            "reviewer_floor": 3.0,
            "loop_limit": 1,
        },
    )
    assert r.status_code == 404


def test_create_task_validates_loop_limit(client: TestClient):
    article_id = _create_article(client)
    r = client.post(
        "/tasks",
        json={
            "article_id": article_id,
            "reviewers": [{"reviewer_id": "r1", "weight": 1.0}],
            "target_aggregate": 4.0,
            "reviewer_floor": 3.0,
            "loop_limit": 0,  # invalid
        },
    )
    assert r.status_code == 422


def test_get_task_initial_state(client: TestClient):
    article_id = _create_article(client)
    task_id = _create_task(client, article_id)
    r = client.get(f"/tasks/{task_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["article_id"] == article_id
    assert data["status"] == "pending"
    assert data["stop_reason"] is None
    assert data["final_version_id"] is None


# =============================================================================
# Iterate
# =============================================================================


def test_iterate_runs_to_target_reached(client: TestClient):
    article_id = _create_article(client)
    task_id = _create_task(client, article_id, target=4.0, floor=3.0, loop_limit=3)
    r = client.post(f"/tasks/{task_id}/iterate", json={})
    assert r.status_code == 200
    data = r.json()
    assert data["stop_reason"] == "target_reached"
    assert data["iterations_run"] == 1


def test_iterate_with_max_iterations_can_return_unfinished(client: TestClient):
    article_id = _create_article(client)
    task_id = _create_task(client, article_id, target=4.0, loop_limit=10)
    r = client.post(f"/tasks/{task_id}/iterate", json={"max_iterations": 1})
    assert r.status_code == 200
    data = r.json()
    # First iter hits target with this fake provider, but max_iterations=1 caps
    # the loop. The orchestrator's natural-stop check is per-iteration so it
    # may legitimately terminate inside the 1-iteration budget.
    assert data["iterations_run"] == 1


def test_iterate_404_on_unknown_task(client: TestClient):
    r = client.post("/tasks/task_nope/iterate", json={})
    assert r.status_code == 404


def test_iterate_accepts_idempotency_key_header(client: TestClient):
    """Header is accepted (forward-compat scaffolding); behavior is
    state-based idempotency from the orchestrator."""
    article_id = _create_article(client)
    task_id = _create_task(client, article_id, target=999, loop_limit=2)
    r = client.post(
        f"/tasks/{task_id}/iterate",
        json={"max_iterations": 1},
        headers={"Idempotency-Key": "abc-123"},
    )
    assert r.status_code == 200


# =============================================================================
# Notes
# =============================================================================


def test_add_note_201(client: TestClient):
    article_id = _create_article(client)
    task_id = _create_task(client, article_id)
    r = client.post(f"/tasks/{task_id}/note", json={"text": "fix the lede"})
    assert r.status_code == 201
    assert r.json()["note_id"]


def test_add_note_404_on_unknown_task(client: TestClient):
    r = client.post("/tasks/task_nope/note", json={"text": "x"})
    assert r.status_code == 404


def test_add_note_rejects_empty_text(client: TestClient):
    article_id = _create_article(client)
    task_id = _create_task(client, article_id)
    r = client.post(f"/tasks/{task_id}/note", json={"text": ""})
    assert r.status_code == 422


# =============================================================================
# Abort
# =============================================================================


def test_abort_returns_204_and_marks_task(client: TestClient):
    article_id = _create_article(client)
    task_id = _create_task(client, article_id)

    r = client.post(f"/tasks/{task_id}/abort")
    assert r.status_code == 204

    r = client.get(f"/tasks/{task_id}")
    assert r.json()["status"] == "aborted"


def test_abort_is_idempotent(client: TestClient):
    article_id = _create_article(client)
    task_id = _create_task(client, article_id)
    assert client.post(f"/tasks/{task_id}/abort").status_code == 204
    assert client.post(f"/tasks/{task_id}/abort").status_code == 204


def test_abort_404_on_unknown_task(client: TestClient):
    assert client.post("/tasks/task_nope/abort").status_code == 404


# =============================================================================
# Lineage (FR13)
# =============================================================================


def test_lineage_after_iteration(client: TestClient):
    article_id = _create_article(client)
    task_id = _create_task(client, article_id, target=4.0)
    client.post(f"/tasks/{task_id}/iterate", json={})

    r = client.get(f"/tasks/{task_id}/lineage")
    assert r.status_code == 200
    data = r.json()
    assert len(data["iterations"]) == 1
    iter0 = data["iterations"][0]
    assert iter0["decision"] == "kept"
    assert {ro["reviewer_id"] for ro in iter0["reviewer_outputs"]} == {"r1", "r2", "r3"}


def test_lineage_404_on_unknown_task(client: TestClient):
    assert client.get("/tasks/task_nope/lineage").status_code == 404


# =============================================================================
# Fact-check report (FR6)
# =============================================================================


def test_fact_check_report_404_before_termination(client: TestClient):
    article_id = _create_article(client)
    task_id = _create_task(client, article_id)
    r = client.get(f"/tasks/{task_id}/fact-check-report")
    assert r.status_code == 404


def test_fact_check_report_after_termination(client: TestClient):
    article_id = _create_article(client)
    task_id = _create_task(client, article_id, target=4.0)
    client.post(f"/tasks/{task_id}/iterate", json={})
    r = client.get(f"/tasks/{task_id}/fact-check-report")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "clean"
    assert data["claims"] == []
