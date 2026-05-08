"""Tests for LiteLLMProvider — wiring only, no real API calls.

We patch `litellm.acompletion` to validate:
- complete() routes (model, messages, max_tokens) through to LiteLLM
- the response text is extracted from the OpenAI-format response
- extra_params merge into the call (cache_control, thinking, reasoning_effort)
- LiteLLM exceptions propagate unchanged
- model strings for Anthropic, OpenAI, and Gemini all pass through

Real-API behavior (rate limits, vendor caching, model behavior) is
LiteLLM's job to test and the operator's to verify in staging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from ghostwriter.adapters import litellm_provider as lp_module
from ghostwriter.adapters.litellm_provider import LiteLLMProvider

# ---- Fake LiteLLM response (mirrors the OpenAI completion shape) ---------


@dataclass
class _FakeMessage:
    content: str | None = "ok"


@dataclass
class _FakeChoice:
    message: _FakeMessage = field(default_factory=_FakeMessage)
    finish_reason: str = "stop"


@dataclass
class _FakeResponse:
    choices: list[_FakeChoice] = field(default_factory=lambda: [_FakeChoice()])


@dataclass
class _Recorder:
    """Records every acompletion invocation; configurable to raise."""

    next_response: _FakeResponse = field(default_factory=_FakeResponse)
    raise_exc: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.next_response


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(lp_module.litellm, "acompletion", rec)
    return rec


# ---- happy paths ---------------------------------------------------------


async def test_complete_returns_text_content(recorder):
    recorder.next_response = _FakeResponse(
        choices=[_FakeChoice(message=_FakeMessage(content="hello world"))]
    )
    out = await LiteLLMProvider().complete(
        model="anthropic/claude-opus-4-7",
        system="sys",
        user="usr",
        max_tokens=512,
    )
    assert out == "hello world"


# ---- request shape -------------------------------------------------------


async def test_complete_passes_model_messages_max_tokens(recorder):
    await LiteLLMProvider().complete(
        model="openai/gpt-5",
        system="sys-prompt",
        user="user-text",
        max_tokens=999,
    )
    [call] = recorder.calls
    assert call["model"] == "openai/gpt-5"
    assert call["max_tokens"] == 999
    assert call["messages"] == [
        {"role": "system", "content": "sys-prompt"},
        {"role": "user", "content": "user-text"},
    ]


async def test_routes_anthropic_openai_gemini(recorder):
    """The model string is passed through unchanged so LiteLLM can route."""
    for model in [
        "anthropic/claude-opus-4-7",
        "openai/gpt-5",
        "gemini/gemini-2.5-pro",
    ]:
        await LiteLLMProvider().complete(model=model, system="s", user="u")
    assert [c["model"] for c in recorder.calls] == [
        "anthropic/claude-opus-4-7",
        "openai/gpt-5",
        "gemini/gemini-2.5-pro",
    ]


async def test_extra_params_pass_through(recorder):
    """Operators inject vendor-specific knobs (cache, thinking, effort) here."""
    provider = LiteLLMProvider(
        extra_params={
            "cache_control": {"type": "ephemeral"},
            "thinking": {"type": "adaptive"},
            "reasoning_effort": "high",
            "thinking_config": {"thinking_budget": 1024},
        }
    )
    await provider.complete(model="m", system="s", user="u")
    [call] = recorder.calls
    assert call["cache_control"] == {"type": "ephemeral"}
    assert call["thinking"] == {"type": "adaptive"}
    assert call["reasoning_effort"] == "high"
    assert call["thinking_config"] == {"thinking_budget": 1024}


async def test_default_extra_params_empty(recorder):
    """No vendor knobs unless the operator opts in. Explicit > implicit."""
    await LiteLLMProvider().complete(model="m", system="s", user="u")
    [call] = recorder.calls
    assert "cache_control" not in call
    assert "thinking" not in call
    assert "reasoning_effort" not in call
    assert "thinking_config" not in call


# ---- error paths ---------------------------------------------------------


async def test_no_text_content_raises_with_finish_reason(recorder):
    recorder.next_response = _FakeResponse(
        choices=[
            _FakeChoice(
                message=_FakeMessage(content=None),
                finish_reason="length",
            )
        ]
    )
    with pytest.raises(ValueError, match="length"):
        await LiteLLMProvider().complete(model="m", system="s", user="u")


async def test_empty_text_raises(recorder):
    recorder.next_response = _FakeResponse(
        choices=[_FakeChoice(message=_FakeMessage(content=""))]
    )
    with pytest.raises(ValueError):
        await LiteLLMProvider().complete(model="m", system="s", user="u")


async def test_litellm_errors_propagate_unchanged(recorder):
    """Transport errors must bubble up so the orchestrator can decide
    whether to retry. The provider does NOT swallow or rewrap."""

    class FakeRateLimitError(Exception):
        pass

    recorder.raise_exc = FakeRateLimitError("simulated 429")
    with pytest.raises(FakeRateLimitError, match="429"):
        await LiteLLMProvider().complete(model="m", system="s", user="u")
