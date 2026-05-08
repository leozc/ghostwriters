# todo

Tracking what's left after the v1 milestones (M1–M9, M5.5) land. All v1
functional requirements (FR1–FR13) and acceptance criteria (A1–A6) are
covered by the 156 tests on this branch; the items below are either
production-readiness or future-version scope.

---

## Before first production run

These need to happen once before the system handles a real article. None
are code; they're operational.

- [ ] **End-to-end smoke test against a real LLM API.** Requires an API
      key for at least one vendor, a sample article, and a real reviewer
      panel. Not automatable in CI. Construct a `LiteLLMProvider`, run
      `Orchestrator.iterate(task_id)` against a 1-iteration task, verify
      lineage + fact-check report look sane. Watch token usage.
      Estimated: 30 min.

- [ ] **Pick concrete model strings per adapter.** Models are routed via
      LiteLLM's `<vendor>/<model>` convention. Recommended starting
      points (any vendor, mixable):
      - Editor: `anthropic/claude-sonnet-4-6` or `openai/gpt-5`
      - Reviewer rubric: `anthropic/claude-haiku-4-5` (fast, cheap)
      - Reviewer pairwise: `anthropic/claude-sonnet-4-6` (nuanced judgment)
      - Fact-checker: `openai/gpt-5` w/ `reasoning_effort=high`, or
        `anthropic/claude-sonnet-4-6` (precision matters)
      Validate with the smoke test; adjust based on quality vs cost.

- [ ] **Decide on `extra_params` per tier.** Each `LiteLLMProvider` takes
      vendor-specific knobs:
      - Anthropic Opus 4.7: `{"thinking": {"type": "adaptive"},
        "cache_control": {"type": "ephemeral"}}`
      - Anthropic Haiku 4.5: `{"cache_control": {"type": "ephemeral"}}`
        (Haiku doesn't support adaptive thinking)
      - OpenAI gpt-5 / o3: `{"reasoning_effort": "high"}`
      - Gemini 2.5 Pro: `{"thinking_config": {"thinking_budget": 1024}}`
      One provider per tier is fine — they're cheap to construct.

---

## Real but not blocking

- [ ] **CLI wrapper.** A thin `ghostwriter <subcommand>` that talks to the
      Store + Orchestrator directly (no HTTP). Subcommands: `article create`,
      `task create`, `iterate`, `note`, `abort`, `lineage`, `status`,
      `fact-check`. Mostly argparse + a thin shim. ~1 hour.

- [ ] **Orphan blob garbage collection.** When `record_iteration` rolls back
      (e.g. a concurrent caller raced past the idempotency lookup, or a
      `consume_note_ids` mismatch), the content blob written before the tx
      is left on disk with no DB row pointing at it. Sweep on startup or
      via cron: list `content/<article_id>/*.md`, diff against
      `article_versions.content_path` ∪ `rejected_candidates.content_path`,
      delete the leftovers. Bounded blast radius (orphans don't break
      anything, just take disk). ~2 hours.

- [ ] **Real-provider integration test as a manual job.** Once the smoke
      test path is wired, freeze it as `tests/integration/test_real_api.py`
      gated behind `pytest -m integration` so CI skips it by default.
      Operators run before each production deploy.

- [ ] **Prompt caching tuning.** Anthropic auto-caches when
      `extra_params={"cache_control": {"type": "ephemeral"}}` is set, but
      our system prompts are below the 1K-4K minimum on most models so
      they may not cross the cache threshold as-is. The win would come
      from restructuring the user prompt so the stable prefix
      (article + guardrails) precedes the volatile suffix (reviewer id).
      Measure first via LiteLLM's `usage.cache_read_input_tokens` after
      the smoke test; tune only if cache hit rate is low and cost matters.
      OpenAI/Gemini cache automatically server-side — no opt-in needed.

---

## Deferred to v2 (intentional)

These are out of v1 scope per `docs/v1-requirements.md` §Deferred. Listed
here so we don't lose them.

- **Per-audience agent router with provider/model fallback.** v1 binds one
  provider + one model per adapter at config time.
- **Held-out judge / drift detection.** v1's bootstrap CI is the only
  quality signal.
- **Automated reviewer calibration loop.** Reviewer weights are operator-set
  in v1.
- **Built-in structured guardrails (length, style, tone).** v1 uses
  free-text `must_do_text` / `must_not_do_text` passed verbatim (FR8).
- **Multi-tenant, auth, billing.** v1 is single-tenant.
- **Multi-process orchestration.** SQLite + idempotency keys keep it
  correct across processes; performance isn't tuned. The
  per-task asyncio lock is process-local. Triggered if FR10's N=10
  ceiling moves.

---

## Future analytics (DuckDB sidecar pattern)

When operators want to ask questions about the lineage — "median iterations
to target across all tasks", "fact-check verdict frequency over time",
"pref_delta distributions by reviewer panel" — the natural answer is a
read-only DuckDB sidecar that `ATTACH`es the SQLite file:

```sql
ATTACH 'ghostwriter.db' AS lineage (TYPE SQLITE);
SELECT MEDIAN(iter_count) FROM (
  SELECT task_id, COUNT(*) AS iter_count
  FROM lineage.iteration_records
  WHERE decision = 'kept'
  GROUP BY task_id
);
```

The operational store stays SQLite. ~4 hours when someone needs it.
