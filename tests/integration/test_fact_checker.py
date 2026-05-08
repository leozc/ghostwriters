"""Tests for the fact-checker adapter and orchestrator integration (FR6).

The adapter parses + validates the LLM response. The orchestrator runs
the adapter exactly once per task at termination, regardless of stop
reason, and writes via Store.save_fact_check (already idempotent per
task_id).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ghostwriter.adapters.fact_checker import FactCheckerConfig, fact_check
from ghostwriter.adapters.provider import FakeProvider
from ghostwriter.types import (
    ClaimVerdict,
    FactCheckReport,
    FactCheckStatus,
)

# --- adapter --------------------------------------------------------------


async def test_fact_check_parses_clean_report():
    fake = FakeProvider(
        handler=lambda m, s, u: json.dumps(
            {
                "status": "clean",
                "claims": [
                    {
                        "text": "The Earth orbits the Sun.",
                        "verdict": "verified",
                        "sources": ["https://nasa.gov"],
                        "rationale": "Standard astronomical fact.",
                    }
                ],
            }
        )
    )
    out = await fact_check(
        config=FactCheckerConfig(model="fc"),
        article_text="some article",
        provider=fake,
    )
    assert isinstance(out, FactCheckReport)
    assert out.status is FactCheckStatus.CLEAN
    assert len(out.claims) == 1
    assert out.claims[0].verdict is ClaimVerdict.VERIFIED
    assert out.claims[0].sources == ["https://nasa.gov"]
    assert out.model == "fc"
    assert len(out.prompt_hash) == 16


async def test_fact_check_parses_findings_report():
    fake = FakeProvider(
        handler=lambda m, s, u: json.dumps(
            {
                "status": "has_findings",
                "claims": [
                    {
                        "text": "The Eiffel Tower is 1000m tall.",
                        "verdict": "contradicted",
                        "sources": [],
                        "rationale": "Actually 330m.",
                    },
                    {
                        "text": "Paris is the best city.",
                        "verdict": "subjective",
                        "sources": [],
                        "rationale": "",
                    },
                ],
            }
        )
    )
    out = await fact_check(
        config=FactCheckerConfig(model="fc"),
        article_text="x",
        provider=fake,
    )
    assert out.status is FactCheckStatus.HAS_FINDINGS
    assert [c.verdict for c in out.claims] == [
        ClaimVerdict.CONTRADICTED,
        ClaimVerdict.SUBJECTIVE,
    ]


async def test_fact_check_empty_claims_is_valid():
    fake = FakeProvider(
        handler=lambda m, s, u: json.dumps({"status": "clean", "claims": []})
    )
    out = await fact_check(
        config=FactCheckerConfig(model="fc"), article_text="x", provider=fake
    )
    assert out.status is FactCheckStatus.CLEAN
    assert out.claims == []


async def test_fact_check_rejects_invalid_status():
    fake = FakeProvider(
        handler=lambda m, s, u: json.dumps({"status": "maybe", "claims": []})
    )
    with pytest.raises(ValueError, match="status"):
        await fact_check(
            config=FactCheckerConfig(model="fc"), article_text="x", provider=fake
        )


async def test_fact_check_rejects_invalid_verdict():
    fake = FakeProvider(
        handler=lambda m, s, u: json.dumps(
            {
                "status": "has_findings",
                "claims": [{"text": "X", "verdict": "kinda-true", "sources": []}],
            }
        )
    )
    with pytest.raises(ValueError, match="verdict"):
        await fact_check(
            config=FactCheckerConfig(model="fc"), article_text="x", provider=fake
        )


async def test_fact_check_rejects_missing_text():
    fake = FakeProvider(
        handler=lambda m, s, u: json.dumps(
            {
                "status": "has_findings",
                "claims": [{"verdict": "unverified", "sources": []}],
            }
        )
    )
    with pytest.raises(ValueError, match="text"):
        await fact_check(
            config=FactCheckerConfig(model="fc"), article_text="x", provider=fake
        )


async def test_fact_check_passes_article_text_into_prompt():
    captured = {}
    fake = FakeProvider(
        handler=lambda m, s, u: (captured.setdefault("u", u), json.dumps({"status": "clean", "claims": []}))[1]
    )
    await fact_check(
        config=FactCheckerConfig(model="fc"),
        article_text="THE FULL ARTICLE TEXT",
        provider=fake,
    )
    assert "THE FULL ARTICLE TEXT" in captured["u"]


async def test_fact_check_raises_on_malformed_json():
    fake = FakeProvider(handler=lambda m, s, u: "not json")
    with pytest.raises(ValueError, match="non-JSON"):
        await fact_check(
            config=FactCheckerConfig(model="fc"), article_text="x", provider=fake
        )


# --- orchestrator integration --------------------------------------------


# Reuse the orchestrator test scaffolding by importing its helpers.
from test_orchestrator import _make_orch, make_handler  # noqa: E402


def _with_fact_checker(
    base_handler,
    *,
    fc_status: str = "clean",
    fc_claims: list[dict] | None = None,
):
    """Wrap an orchestrator handler so it ALSO answers fact-checker calls."""
    fc_claims = fc_claims or []

    def handler(model: str, system: str, user: str) -> str:
        if "fact-checker" in system:
            return json.dumps({"status": fc_status, "claims": fc_claims})
        return base_handler(model, system, user)

    return handler


async def test_orchestrator_runs_fact_check_at_target_reached(tmp_path: Path):
    handler = _with_fact_checker(
        make_handler(default_pref="candidate"),
        fc_status="has_findings",
        fc_claims=[
            {"text": "Claim X", "verdict": "unverified", "sources": [], "rationale": "?"}
        ],
    )
    orch, store, task_id, _ = _make_orch(tmp_path, handler=handler)
    object.__setattr__(orch.config, "fact_checker", FactCheckerConfig(model="fc"))

    await orch.iterate(task_id)

    report = store.get_fact_check(task_id)
    assert report is not None
    assert report.status is FactCheckStatus.HAS_FINDINGS
    assert len(report.claims) == 1


async def test_orchestrator_runs_fact_check_at_loop_limit(tmp_path: Path):
    handler = _with_fact_checker(
        make_handler(
            default_pref="incumbent",
            default_scores={"clarity": 3, "accuracy": 3, "voice": 3},
        )
    )
    orch, store, task_id, _ = _make_orch(tmp_path, handler=handler, loop_limit=2)
    object.__setattr__(orch.config, "fact_checker", FactCheckerConfig(model="fc"))

    await orch.iterate(task_id)

    report = store.get_fact_check(task_id)
    assert report is not None
    assert report.status is FactCheckStatus.CLEAN


async def test_orchestrator_runs_fact_check_at_abort(tmp_path: Path):
    handler = _with_fact_checker(make_handler(default_pref="candidate"))
    orch, store, task_id, _ = _make_orch(tmp_path, handler=handler, loop_limit=10)
    object.__setattr__(orch.config, "fact_checker", FactCheckerConfig(model="fc"))

    await orch.abort(task_id)
    await orch.iterate(task_id)

    report = store.get_fact_check(task_id)
    assert report is not None


async def test_orchestrator_skips_fact_check_when_not_terminating(tmp_path: Path):
    """A bounded iterate() call that returns stop_reason=None must not
    run fact-check — the task is not terminal."""
    handler = _with_fact_checker(make_handler(default_pref="incumbent"))
    orch, store, task_id, _ = _make_orch(tmp_path, handler=handler, loop_limit=10)
    object.__setattr__(orch.config, "fact_checker", FactCheckerConfig(model="fc"))

    result = await orch.iterate(task_id, max_iterations=1)
    assert result.stop_reason is None
    assert store.get_fact_check(task_id) is None


async def test_orchestrator_fact_check_is_one_shot_per_task(tmp_path: Path):
    """Calling iterate() on an already-terminal task must not re-run
    fact-check (Store.save_fact_check is idempotent, but we should also
    not even invoke the fact-checker LLM)."""
    handler = _with_fact_checker(make_handler(default_pref="candidate"))
    orch, store, task_id, _ = _make_orch(tmp_path, handler=handler)
    object.__setattr__(orch.config, "fact_checker", FactCheckerConfig(model="fc"))

    await orch.iterate(task_id)
    fc_calls_after_first = sum(
        1 for c in orch.provider.calls if "fact-checker" in c.system  # type: ignore[attr-defined]
    )
    assert fc_calls_after_first == 1

    await orch.iterate(task_id)
    fc_calls_after_second = sum(
        1 for c in orch.provider.calls if "fact-checker" in c.system  # type: ignore[attr-defined]
    )
    assert fc_calls_after_second == 1  # no extra call


async def test_orchestrator_omits_fact_check_when_config_is_none(tmp_path: Path):
    """If the operator hasn't wired a FactCheckerConfig, the orchestrator
    silently skips. This is the test-convenience path used by every
    other orchestrator test."""
    handler = make_handler(default_pref="candidate")
    orch, store, task_id, _ = _make_orch(tmp_path, handler=handler)
    assert orch.config.fact_checker is None

    await orch.iterate(task_id)
    assert store.get_fact_check(task_id) is None
