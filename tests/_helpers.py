"""Shared test helpers.

Importable from any test (configured via `tool.pytest.ini_options.pythonpath`
in pyproject.toml). Leading underscore signals "internal to the test
suite — not a fixture, not a public API."

Used by tests/integration/test_orchestrator.py and
tests/acceptance/test_acceptance.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from ghostwriter.adapters.editor import EditorConfig
from ghostwriter.adapters.provider import FakeProvider
from ghostwriter.adapters.reviewer import ReviewerConfig
from ghostwriter.orchestrator import Orchestrator, OrchestratorConfig
from ghostwriter.store.sqlite import Store
from ghostwriter.types import ReviewerSpec, TaskConfig

DIMS = ("clarity", "accuracy", "voice")


def make_handler(
    *,
    editor_text: str = "candidate body v",
    default_pref: str = "candidate",
    default_scores: dict[str, int] | None = None,
    per_reviewer_pref: dict[str, str] | None = None,
    per_reviewer_scores: dict[str, dict[str, int]] | None = None,
    editor_text_by_iter: list[str] | None = None,
):
    """Build a (model, system, user) -> str handler for FakeProvider that
    routes by adapter-prompt fingerprint and reviewer id."""
    default_scores = default_scores or {"clarity": 4, "accuracy": 4, "voice": 4}
    per_reviewer_pref = per_reviewer_pref or {}
    per_reviewer_scores = per_reviewer_scores or {}
    editor_calls = {"n": 0}

    def find_reviewer_id(user: str) -> str | None:
        for line in user.splitlines():
            if line.startswith("Reviewer id:"):
                return line.split(":", 1)[1].strip()
        return None

    def handler(model: str, system: str, user: str) -> str:
        if "copy editor" in system:
            if editor_text_by_iter is not None:
                idx = min(editor_calls["n"], len(editor_text_by_iter) - 1)
                text = editor_text_by_iter[idx]
            else:
                text = f"{editor_text}{editor_calls['n']}"
            editor_calls["n"] += 1
            return json.dumps({"text": text, "summary": "tweak"})
        rid = find_reviewer_id(user)
        if "Score the article" in system:
            scores = per_reviewer_scores.get(rid or "", default_scores)
            return json.dumps({"scores": scores, "rationale": "x"})
        if "comparing two versions" in system:
            pref = per_reviewer_pref.get(rid or "", default_pref)
            return json.dumps({"pref": pref, "rationale": "x"})
        raise AssertionError(f"unrecognized adapter call: system={system!r}")

    return handler


def _make_orch(
    tmp_path: Path,
    *,
    handler,
    weights: dict[str, float] | None = None,
    target: float = 4.0,
    floor: float = 3.0,
    loop_limit: int = 5,
    must_do: str = "",
    must_not_do: str = "",
) -> tuple[Orchestrator, Store, str, str]:
    weights = weights or {"r1": 1.0, "r2": 1.0, "r3": 1.0}
    store = Store(
        db_path=tmp_path / "g.db", content_root=tmp_path / "content"
    )
    article_id, version_id = store.create_article(
        slug="t", content="# Initial\n\nIncumbent body."
    )
    task_id = store.create_task(
        TaskConfig(
            article_id=article_id,
            reviewers=[ReviewerSpec(reviewer_id=k, weight=v) for k, v in weights.items()],
            target_aggregate=target,
            reviewer_floor=floor,
            loop_limit=loop_limit,
            must_do_text=must_do,
            must_not_do_text=must_not_do,
        )
    )
    reviewer_cfgs = {
        rid: ReviewerConfig(
            reviewer_id=rid,
            weight=w,
            rubric_dims=DIMS,
            rubric_model="rev-rubric",
            pairwise_model="rev-pair",
        )
        for rid, w in weights.items()
    }
    config = OrchestratorConfig(
        editor=EditorConfig(model="ed"),
        reviewers=reviewer_cfgs,
    )
    orch = Orchestrator(
        store=store, provider=FakeProvider(handler=handler), config=config
    )
    return orch, store, task_id, article_id
