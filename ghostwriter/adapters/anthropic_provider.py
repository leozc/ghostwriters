"""Anthropic SDK provider — production binding for the Provider protocol.

Implements the same async `complete()` surface as FakeProvider so the
orchestrator can swap providers without code changes.

Design choices:
- Async only. The orchestrator is asyncio; sync clients would require
  run_in_executor wrappers we don't need.
- Streaming with .get_final_message() always. The SDK refuses
  non-streaming requests it estimates may exceed ~10 minutes; using
  stream-then-finalize sidesteps the cliff and behaves identically for
  short responses.
- Top-level cache_control on by default. Anthropic auto-caches the
  longest cacheable prefix; if our prompts fall below the minimum
  (1K-4K tokens depending on model) the cache silently no-ops with no
  cost penalty.
- Errors propagate as the SDK's typed exceptions
  (anthropic.RateLimitError, anthropic.APIStatusError, etc.). The
  adapter layer above catches ValueError on JSON parse failures; SDK
  errors bubble to the orchestrator.

Operator notes:
- For Claude 4.x (Opus 4.7 / Opus 4.6 / Sonnet 4.6), pass
  extra_params={"thinking": {"type": "adaptive"}}. Adaptive thinking
  is opt-in on Opus 4.7 (default off).
- For Haiku 4.5, leave extra_params empty — Haiku doesn't support
  adaptive thinking.
- Per-iteration model selection lives in EditorConfig / ReviewerConfig
  / FactCheckerConfig, not here. This provider just routes the model
  string to the SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anthropic


@dataclass
class AnthropicProvider:
    """Production Provider binding around `anthropic.AsyncAnthropic`.

    Default constructor reads ANTHROPIC_API_KEY from the environment.
    Pass an explicit client (or a mock) to override.
    """

    client: anthropic.AsyncAnthropic = field(
        default_factory=lambda: anthropic.AsyncAnthropic()
    )
    enable_caching: bool = True
    extra_params: dict[str, Any] = field(default_factory=dict)

    async def complete(
        self, *, model: str, system: str, user: str, max_tokens: int = 2048
    ) -> str:
        kwargs: dict[str, Any] = dict(self.extra_params)
        if self.enable_caching:
            kwargs.setdefault("cache_control", {"type": "ephemeral"})

        async with self.client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            **kwargs,
        ) as stream:
            message = await stream.get_final_message()

        # Extract the first text block. On Opus 4.7 thinking blocks are
        # present but carry empty text by default (display="omitted"),
        # so the type-filter is sufficient — we don't need to also check
        # for non-empty content.
        for block in message.content:
            if getattr(block, "type", None) == "text":
                return block.text
        raise ValueError(
            "Anthropic response had no text block "
            f"(stop_reason={getattr(message, 'stop_reason', '?')!r}); "
            "this typically means a refusal or max_tokens hit before any "
            "text was emitted"
        )
