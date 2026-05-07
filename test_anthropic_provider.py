"""Tests for AnthropicProvider — wiring only, no real API calls.

We mock the AsyncAnthropic client to validate:
- complete() routes (model, system, user, max_tokens) through to the SDK
- the response text is extracted from the first text block
- cache_control is included by default and skippable
- extra_params merge into the SDK call (thinking, effort, etc.)
- SDK exceptions propagate unchanged

Real-API behavior (rate limits, prompt caching, model behavior) is the
SDK's job to test and the operator's to verify in staging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anthropic
import pytest

from ghostwriter.adapters.anthropic_provider import AnthropicProvider


# ---- Fake SDK client (mirrors anthropic.AsyncAnthropic shape) ------------


@dataclass
class _FakeBlock:
    type: str
    text: str = ""


@dataclass
class _FakeMessage:
    content: list[_FakeBlock]
    stop_reason: str = "end_turn"


class _FakeStreamCM:
    def __init__(self, message: _FakeMessage, raise_inside: Exception | None = None):
        self._message = message
        self._raise = raise_inside

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get_final_message(self):
        if self._raise is not None:
            raise self._raise
        return self._message


@dataclass
class _FakeMessages:
    """Records every stream() invocation so tests can assert on what the
    provider sent. Also configurable to raise on stream entry."""

    next_message: _FakeMessage = field(
        default_factory=lambda: _FakeMessage(content=[_FakeBlock(type="text", text="ok")])
    )
    raise_on_stream: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeStreamCM(self.next_message, raise_inside=self.raise_on_stream)


@dataclass
class _FakeClient:
    messages: _FakeMessages = field(default_factory=_FakeMessages)


def _provider(**kwargs) -> tuple[AnthropicProvider, _FakeClient]:
    client = _FakeClient()
    return AnthropicProvider(client=client, **kwargs), client  # type: ignore[arg-type]


# ---- happy paths ---------------------------------------------------------


async def test_complete_returns_first_text_block():
    provider, client = _provider()
    client.messages.next_message = _FakeMessage(
        content=[_FakeBlock(type="text", text="hello world")]
    )
    out = await provider.complete(
        model="claude-opus-4-7", system="sys", user="usr", max_tokens=512
    )
    assert out == "hello world"


async def test_complete_skips_thinking_blocks():
    """Opus 4.7 returns thinking blocks alongside text. The provider
    must skip them and return the text block."""
    provider, client = _provider()
    client.messages.next_message = _FakeMessage(
        content=[
            _FakeBlock(type="thinking", text=""),
            _FakeBlock(type="text", text="actual answer"),
        ]
    )
    out = await provider.complete(model="m", system="s", user="u")
    assert out == "actual answer"


async def test_complete_returns_first_text_when_multiple():
    """Tool-use plus text — take the first text block (rare for our
    adapters, but the contract should be deterministic)."""
    provider, client = _provider()
    client.messages.next_message = _FakeMessage(
        content=[
            _FakeBlock(type="text", text="first"),
            _FakeBlock(type="text", text="second"),
        ]
    )
    out = await provider.complete(model="m", system="s", user="u")
    assert out == "first"


# ---- request shape -------------------------------------------------------


async def test_complete_passes_model_system_user_max_tokens():
    provider, client = _provider()
    await provider.complete(
        model="claude-haiku-4-5", system="sys-prompt", user="user-text", max_tokens=999
    )
    [call] = client.messages.calls
    assert call["model"] == "claude-haiku-4-5"
    assert call["system"] == "sys-prompt"
    assert call["max_tokens"] == 999
    assert call["messages"] == [{"role": "user", "content": "user-text"}]


async def test_caching_enabled_by_default():
    provider, client = _provider()
    await provider.complete(model="m", system="s", user="u")
    [call] = client.messages.calls
    assert call.get("cache_control") == {"type": "ephemeral"}


async def test_caching_can_be_disabled():
    provider, client = _provider(enable_caching=False)
    await provider.complete(model="m", system="s", user="u")
    [call] = client.messages.calls
    assert "cache_control" not in call


async def test_extra_params_pass_through():
    """Operators inject thinking / effort / etc. via extra_params."""
    provider, client = _provider(
        extra_params={
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
        }
    )
    await provider.complete(model="m", system="s", user="u")
    [call] = client.messages.calls
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "high"}


async def test_extra_params_can_override_cache_control():
    """If the operator wants a different TTL, their extra_params should win."""
    provider, client = _provider(
        extra_params={"cache_control": {"type": "ephemeral", "ttl": "1h"}}
    )
    await provider.complete(model="m", system="s", user="u")
    [call] = client.messages.calls
    assert call["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


# ---- error paths ---------------------------------------------------------


async def test_no_text_block_raises_with_stop_reason():
    provider, client = _provider()
    client.messages.next_message = _FakeMessage(
        content=[_FakeBlock(type="thinking", text="")],
        stop_reason="refusal",
    )
    with pytest.raises(ValueError, match="no text block"):
        await provider.complete(model="m", system="s", user="u")


async def test_empty_content_raises():
    provider, client = _provider()
    client.messages.next_message = _FakeMessage(content=[], stop_reason="max_tokens")
    with pytest.raises(ValueError, match="max_tokens"):
        await provider.complete(model="m", system="s", user="u")


async def test_sdk_errors_propagate_unchanged():
    """RateLimitError and friends must bubble up so the orchestrator can
    decide whether to retry. The provider does NOT swallow or rewrap."""
    provider, client = _provider()
    rate_limit = anthropic.RateLimitError(
        message="429",
        response=_FakeResponse(429),  # type: ignore[arg-type]
        body=None,
    )
    client.messages.raise_on_stream = rate_limit
    with pytest.raises(anthropic.RateLimitError):
        await provider.complete(model="m", system="s", user="u")


# ---- helpers -------------------------------------------------------------


@dataclass
class _FakeResponse:
    """Minimal shape required by anthropic.RateLimitError constructor."""

    status_code: int

    @property
    def headers(self) -> dict[str, str]:
        return {}

    @property
    def request(self):
        return None
