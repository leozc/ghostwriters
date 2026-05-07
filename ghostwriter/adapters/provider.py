"""LLM provider interface + deterministic fake for tests.

The Provider protocol is the only seam between adapters and a real LLM
SDK. Adapters depend on this protocol, never on Anthropic/OpenAI types.
This keeps adapters trivially testable — pass a FakeProvider with a
scripted handler and assert on the recorded calls.

prompt_hash convention (FR11): sha256 of f"{model}\n{system}\n{user}",
truncated to 16 hex chars. Stored alongside each call so audit replays
can detect prompt drift.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol


def prompt_hash(*, model: str, system: str, user: str) -> str:
    raw = f"{model}\n{system}\n{user}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


class Provider(Protocol):
    """Minimal LLM call surface. Adapters may add JSON-parsing on top."""

    async def complete(
        self, *, model: str, system: str, user: str, max_tokens: int = 2048
    ) -> str: ...


@dataclass
class FakeCall:
    model: str
    system: str
    user: str


# A handler maps (model, system, user) -> response text. Sync or async.
Handler = Callable[[str, str, str], "str | Awaitable[str]"]


@dataclass
class FakeProvider:
    """Deterministic in-memory provider for tests and the dev loop.

    Pass a handler that returns the response text for any (model, system, user)
    triple. Each call is appended to `calls` so tests can assert against
    prompt content without intercepting at the SDK level.
    """

    handler: Handler
    calls: list[FakeCall] = field(default_factory=list)

    async def complete(
        self, *, model: str, system: str, user: str, max_tokens: int = 2048
    ) -> str:
        self.calls.append(FakeCall(model=model, system=system, user=user))
        result = self.handler(model, system, user)
        if hasattr(result, "__await__"):
            return await result  # type: ignore[no-any-return]
        return result  # type: ignore[return-value]
