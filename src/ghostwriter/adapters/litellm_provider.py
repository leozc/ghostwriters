"""LiteLLM-backed Provider — unified binding for Anthropic, OpenAI, Gemini, and 100+ others.

Implements the same async `complete()` surface as FakeProvider so the
orchestrator can swap providers without code changes.

Model strings use LiteLLM's `<vendor>/<model>` convention:
- "anthropic/claude-opus-4-7"
- "openai/gpt-5"
- "gemini/gemini-2.5-pro"

LiteLLM normalizes the request format across vendors — adapters pass
(system, user) and LiteLLM constructs vendor-appropriate payloads.

Vendor-specific knobs go through `extra_params`. Examples:

- Anthropic prompt caching:
    extra_params={"cache_control": {"type": "ephemeral"}}
- Anthropic adaptive thinking (Opus 4.7):
    extra_params={"thinking": {"type": "adaptive"}}
- OpenAI reasoning effort (gpt-5, o3):
    extra_params={"reasoning_effort": "high"}
- Gemini thinking budget (2.5 Flash, 2.5 Pro):
    extra_params={"thinking_config": {"thinking_budget": 1024}}

Auth is environment-based: set ANTHROPIC_API_KEY / OPENAI_API_KEY /
GEMINI_API_KEY (or GOOGLE_API_KEY) per the vendors you route to.

Errors propagate as LiteLLM's typed exceptions (litellm.RateLimitError,
litellm.APIError, etc.). The adapter layer above catches ValueError on
JSON-parse failures; transport errors bubble to the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import litellm


@dataclass
class LiteLLMProvider:
    """Production Provider binding around `litellm.acompletion`.

    No client construction needed — LiteLLM reads vendor API keys from
    environment variables on each call.

    Per-iteration model selection lives in EditorConfig / ReviewerConfig
    / FactCheckerConfig, not here. This provider just routes the
    `<vendor>/<model>` string to LiteLLM.
    """

    extra_params: dict[str, Any] = field(default_factory=dict)

    async def complete(
        self, *, model: str, system: str, user: str, max_tokens: int = 2048
    ) -> str:
        response = await litellm.acompletion(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            **self.extra_params,
        )

        # LiteLLM normalizes responses to OpenAI format regardless of vendor.
        choice = response.choices[0]
        text = getattr(choice.message, "content", None)
        if not text:
            finish_reason = getattr(choice, "finish_reason", "?")
            raise ValueError(
                f"LiteLLM response had no text content "
                f"(finish_reason={finish_reason!r}); "
                "this typically means a refusal or max_tokens hit before "
                "any text was emitted"
            )
        return str(text)
