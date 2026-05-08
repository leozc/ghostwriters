# ghostwriter

Single-tenant service for autonomous prose optimization. Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch): an iterative loop of *propose → review → keep-or-revert*, with a factual gate, lineage tracking, and pairwise-preference promotion.

## What v1 does

You hand ghostwriter an article and a panel of reviewer personas with weights. The orchestrator iterates:

1. **Editor** drafts a candidate based on the prior version, reviewer feedback, and any human notes injected mid-loop.
2. **Reviewers** score the candidate (rubric + pairwise preference vs. the incumbent), in parallel.
3. **Promotion gate** keeps the candidate iff the bootstrap-CI lower bound on the weighted preference is above zero (FR4) — score-gaming is harder when promotion requires near-unanimous human-preference signal, not just rubric points.
4. **Fact-checker** runs once at the terminal step (FR6), advisory.
5. **Lineage store** records every iteration, including rejected candidates, with the prompt hashes used.

You can run multiple tasks per article concurrently with different reviewer panels and goals.

## Architecture

```
+---------+     +-----------------+     +------------+
|   API   | <-> |  Orchestrator   | <-> |  Adapters  |
| (HTTP)  |     |  - per-task lock |     |  Editor    |
+---------+     |  - parallel rev  |     |  Reviewer  |
                |  - pairwise gate |     |  FactCheck |
                +-----------------+     +-----+------+
                          |                   |
                          v                   v
                   +-----------+        +-----------+
                   |  Lineage  |        | Provider  |
                   |  Store    |        | (LiteLLM) |
                   |  SQLite   |        +-----------+
                   +-----------+              |
                                              v
                              Anthropic / OpenAI / Gemini / 100+
```

- **One Provider impl** — `LiteLLMProvider` wraps [LiteLLM](https://github.com/BerriAI/litellm). Models are addressed as `<vendor>/<model>` (`anthropic/claude-opus-4-7`, `openai/gpt-5`, `gemini/gemini-2.5-pro`). Vendor-specific knobs (Anthropic prompt caching, adaptive thinking; OpenAI `reasoning_effort`; Gemini `thinking_config`) go through `extra_params`.
- **Pluggable adapters** — `EditorConfig` / `ReviewerConfig` / `FactCheckerConfig` accept any model string; mix vendors per role freely.
- **SQLite-backed lineage** — immutable iteration records, content blobs on disk, per-task asyncio lock.

See `docs/v1-design.md` for the full architecture and `docs/v1-requirements.md` for the FR/A traceability matrix.

## Quick start

```bash
git clone https://github.com/leozc/ghostwriters
cd ghostwriters
uv sync
```

Set the API keys for whatever vendors you'll route to:

```bash
export ANTHROPIC_API_KEY=...   # for anthropic/* models
export OPENAI_API_KEY=...      # for openai/* models
export GEMINI_API_KEY=...      # for gemini/* models
```

Run the HTTP API:

```bash
uv run uvicorn ghostwriter.api:app --reload
```

Or wire the orchestrator directly in Python — see `test_acceptance.py` for an end-to-end example using `FakeProvider`.

## Configuring a provider

```python
from ghostwriter.adapters.litellm_provider import LiteLLMProvider
from ghostwriter.adapters.editor import EditorConfig
from ghostwriter.adapters.reviewer import ReviewerConfig
from ghostwriter.adapters.fact_checker import FactCheckerConfig

# One provider per tier — each tier picks its own knobs.
editor_provider = LiteLLMProvider(
    extra_params={"cache_control": {"type": "ephemeral"},
                  "thinking": {"type": "adaptive"}}
)
reviewer_provider = LiteLLMProvider(
    extra_params={"cache_control": {"type": "ephemeral"}}  # Haiku: no thinking
)
factcheck_provider = LiteLLMProvider(
    extra_params={"reasoning_effort": "high"}  # OpenAI gpt-5 / o3
)

editor_cfg    = EditorConfig(model="anthropic/claude-sonnet-4-6")
reviewer_cfg  = ReviewerConfig(rubric_model="anthropic/claude-haiku-4-5",
                               pairwise_model="anthropic/claude-sonnet-4-6")
factcheck_cfg = FactCheckerConfig(model="openai/gpt-5")
```

## Tests

```bash
uv run pytest -q          # unit + integration + acceptance
uv run pytest test_acceptance.py -v   # A1–A6 user-facing scenarios
```

Real-API behavior is not exercised in the test suite. See `todo.md` for the recommended pre-deploy smoke test.

## License

MIT
