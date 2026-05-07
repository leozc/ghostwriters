"""Tests for editor + reviewer adapters with FakeProvider.

The fake provider is the workhorse. Tests assert on:
- the parsed adapter output (correct types, validated fields)
- the prompts that were sent (FR8 verbatim guardrails, FR5 one-shot notes)
- error paths (malformed JSON, out-of-range scores, missing fields)
"""

from __future__ import annotations

import json

import pytest

from ghostwriter.adapters.editor import EditorConfig, propose
from ghostwriter.adapters.provider import FakeProvider, prompt_hash
from ghostwriter.adapters.reviewer import (
    ReviewerConfig,
    compare_pairwise,
    score_rubric,
)
from ghostwriter.types import (
    EditorInput,
    Pairwise,
    PairwisePref,
    ReviewerOutput,
    RubricScores,
)


# ---- helpers ---------------------------------------------------------------


def _editor_cfg(model: str = "fake-editor") -> EditorConfig:
    return EditorConfig(model=model)


def _reviewer_cfg(
    *,
    reviewer_id: str = "r1",
    dims: tuple[str, ...] = ("clarity", "accuracy", "voice"),
) -> ReviewerConfig:
    return ReviewerConfig(
        reviewer_id=reviewer_id,
        weight=1.0,
        rubric_dims=dims,
        rubric_model="fake-rubric",
        pairwise_model="fake-pairwise",
    )


def _editor_input(
    *,
    incumbent: str = "v0 article",
    must_do: str = "",
    must_not_do: str = "",
    prior: list[ReviewerOutput] | None = None,
    notes: list[str] | None = None,
) -> EditorInput:
    return EditorInput(
        incumbent_text=incumbent,
        prior_reviews=prior or [],
        must_do_text=must_do,
        must_not_do_text=must_not_do,
        human_notes=notes or [],
    )


# =============================================================================
# Editor
# =============================================================================


async def test_editor_returns_candidate_with_hash_and_model():
    fake = FakeProvider(
        handler=lambda m, s, u: json.dumps(
            {"text": "v1 article", "summary": "tightened intro"}
        )
    )
    cand = await propose(
        config=_editor_cfg("fake-editor"),
        editor_input=_editor_input(),
        provider=fake,
    )
    assert cand.text == "v1 article"
    assert cand.edit_summary == "tightened intro"
    assert cand.model == "fake-editor"
    assert len(cand.prompt_hash) == 16
    assert len(fake.calls) == 1


async def test_editor_passes_must_do_and_must_not_do_verbatim():
    """FR8: guardrails appear verbatim, not paraphrased."""
    captured = {}

    def handler(m: str, s: str, u: str) -> str:
        captured["user"] = u
        return json.dumps({"text": "ok", "summary": ""})

    await propose(
        config=_editor_cfg(),
        editor_input=_editor_input(
            must_do="Use exactly the phrase 'lineage DAG'",
            must_not_do="Never mention prompt engineering",
        ),
        provider=FakeProvider(handler=handler),
    )
    assert "Use exactly the phrase 'lineage DAG'" in captured["user"]
    assert "Never mention prompt engineering" in captured["user"]


async def test_editor_includes_human_notes_in_prompt():
    """FR5: notes flow into the next-iteration prompt."""
    captured = {}
    fake = FakeProvider(
        handler=lambda m, s, u: (captured.setdefault("u", u), json.dumps({"text": "x", "summary": ""}))[1]
    )
    await propose(
        config=_editor_cfg(),
        editor_input=_editor_input(notes=["sharpen the lede", "drop the bullet list"]),
        provider=fake,
    )
    assert "sharpen the lede" in captured["u"]
    assert "drop the bullet list" in captured["u"]


async def test_editor_renders_empty_prior_reviews_explicitly():
    """First iteration has no prior reviews; the prompt should say so
    instead of leaving an awkward blank, which can confuse some models."""
    captured = {}
    fake = FakeProvider(
        handler=lambda m, s, u: (captured.setdefault("u", u), json.dumps({"text": "x", "summary": ""}))[1]
    )
    await propose(
        config=_editor_cfg(), editor_input=_editor_input(), provider=fake
    )
    assert "first iteration" in captured["u"].lower() or "(none" in captured["u"]


async def test_editor_raises_on_malformed_json():
    fake = FakeProvider(handler=lambda m, s, u: "not json at all")
    with pytest.raises(ValueError, match="non-JSON"):
        await propose(
            config=_editor_cfg(), editor_input=_editor_input(), provider=fake
        )


async def test_editor_raises_on_empty_text():
    fake = FakeProvider(
        handler=lambda m, s, u: json.dumps({"text": "   ", "summary": "x"})
    )
    with pytest.raises(ValueError, match="missing/empty 'text'"):
        await propose(
            config=_editor_cfg(), editor_input=_editor_input(), provider=fake
        )


# =============================================================================
# Reviewer — rubric
# =============================================================================


async def test_score_rubric_parses_and_aggregates():
    fake = FakeProvider(
        handler=lambda m, s, u: json.dumps(
            {"scores": {"clarity": 4, "accuracy": 5, "voice": 3}, "rationale": "ok"}
        )
    )
    out = await score_rubric(
        config=_reviewer_cfg(), candidate_text="article body", provider=fake
    )
    assert isinstance(out, RubricScores)
    assert out.scores == {"clarity": 4.0, "accuracy": 5.0, "voice": 3.0}
    assert out.aggregate == pytest.approx((4 + 5 + 3) / 3)
    assert out.model == "fake-rubric"


async def test_score_rubric_raises_on_dim_mismatch():
    """Reviewer returned a key we didn't ask about (or missed one) — surface
    loudly. Silently dropping the extra key would inflate the aggregate
    in confusing ways."""
    fake = FakeProvider(
        handler=lambda m, s, u: json.dumps(
            {"scores": {"clarity": 4, "accuracy": 5}, "rationale": "ok"}
        )
    )
    with pytest.raises(ValueError, match="dim mismatch"):
        await score_rubric(
            config=_reviewer_cfg(), candidate_text="x", provider=fake
        )


async def test_score_rubric_raises_on_out_of_range_score():
    fake = FakeProvider(
        handler=lambda m, s, u: json.dumps(
            {"scores": {"clarity": 4, "accuracy": 7, "voice": 3}}
        )
    )
    with pytest.raises(ValueError, match="out of range"):
        await score_rubric(
            config=_reviewer_cfg(), candidate_text="x", provider=fake
        )


async def test_score_rubric_raises_on_non_numeric_score():
    fake = FakeProvider(
        handler=lambda m, s, u: json.dumps(
            {"scores": {"clarity": "good", "accuracy": 5, "voice": 3}}
        )
    )
    with pytest.raises(ValueError, match="out of range"):
        await score_rubric(
            config=_reviewer_cfg(), candidate_text="x", provider=fake
        )


# =============================================================================
# Reviewer — pairwise
# =============================================================================


@pytest.mark.parametrize(
    "pref_str,expected",
    [
        ("candidate", PairwisePref.CANDIDATE),
        ("incumbent", PairwisePref.INCUMBENT),
        ("tie", PairwisePref.TIE),
    ],
)
async def test_compare_pairwise_parses_each_pref(pref_str: str, expected: PairwisePref):
    fake = FakeProvider(
        handler=lambda m, s, u: json.dumps({"pref": pref_str, "rationale": "x"})
    )
    out = await compare_pairwise(
        config=_reviewer_cfg(),
        candidate_text="cand",
        incumbent_text="inc",
        provider=fake,
    )
    assert isinstance(out, Pairwise)
    assert out.pref is expected
    assert out.rationale == "x"
    assert out.model == "fake-pairwise"


async def test_compare_pairwise_passes_both_versions_in_prompt():
    """Sanity: both texts reach the prompt and they're labeled."""
    captured = {}

    def handler(m: str, s: str, u: str) -> str:
        captured["u"] = u
        return json.dumps({"pref": "candidate", "rationale": "ok"})

    await compare_pairwise(
        config=_reviewer_cfg(),
        candidate_text="THE CANDIDATE TEXT",
        incumbent_text="THE INCUMBENT TEXT",
        provider=FakeProvider(handler=handler),
    )
    assert "THE CANDIDATE TEXT" in captured["u"]
    assert "THE INCUMBENT TEXT" in captured["u"]


async def test_compare_pairwise_rejects_invalid_pref():
    fake = FakeProvider(
        handler=lambda m, s, u: json.dumps({"pref": "neither", "rationale": "x"})
    )
    with pytest.raises(ValueError, match="candidate/incumbent/tie"):
        await compare_pairwise(
            config=_reviewer_cfg(),
            candidate_text="c",
            incumbent_text="i",
            provider=fake,
        )


async def test_compare_pairwise_tolerates_missing_rationale():
    fake = FakeProvider(handler=lambda m, s, u: json.dumps({"pref": "tie"}))
    out = await compare_pairwise(
        config=_reviewer_cfg(),
        candidate_text="c",
        incumbent_text="i",
        provider=fake,
    )
    assert out.rationale == ""


# =============================================================================
# Provider basics
# =============================================================================


def test_prompt_hash_is_stable_and_truncated():
    a = prompt_hash(model="m", system="s", user="u")
    b = prompt_hash(model="m", system="s", user="u")
    c = prompt_hash(model="m", system="s", user="u2")
    assert a == b
    assert a != c
    assert len(a) == 16


async def test_fake_provider_records_calls():
    fake = FakeProvider(handler=lambda m, s, u: "ok")
    await fake.complete(model="m1", system="sys", user="u1")
    await fake.complete(model="m2", system="sys", user="u2")
    assert [c.model for c in fake.calls] == ["m1", "m2"]
    assert [c.user for c in fake.calls] == ["u1", "u2"]


async def test_fake_provider_accepts_async_handler():
    async def handler(m: str, s: str, u: str) -> str:
        return "async-ok"

    fake = FakeProvider(handler=handler)
    out = await fake.complete(model="m", system="s", user="u")
    assert out == "async-ok"
