# Ghostwriter v1 Redesign — Design

Companion to `docs/v1-requirements.md`. Maps each functional requirement to a concrete design element. Scoped strictly to FR1–FR13 — anything not listed there is deferred.

## High-level architecture

```
+-------+     +---------+     +-----------------+
|  CLI  | --> |  HTTP   | --> |  Orchestrator   |
+-------+     |  API    |     +--------+--------+
              +---------+              |
                                       v
              +----------+    +--------+--------+    +-----------+
              | Lineage  | <- |    Adapters     | -> | Providers |
              |  Store   |    |  - Editor       |    | (LLM API) |
              +----------+    |  - Reviewer     |    +-----------+
                              |  - FactChecker  |
                              +-----------------+
```

Single Python process per tenant. SQLite + filesystem for persistence. Adapters wrap LLM providers behind a uniform interface so editor / reviewer / fact-checker can be configured independently.

## Components

### Orchestrator
Owns the loop. One asyncio task per active task. Per-task lock prevents concurrent iterations on the same task (FR9, FR10). Maintains in-memory state for in-flight iterations; writes durable state to the lineage store at iteration boundaries (FR12).

Responsibilities:
- Schedule iterations (`POST /tasks/{id}/iterate`)
- Compose editor + reviewer + fact-checker calls
- Compute weighted aggregate, pairwise pref delta, bootstrap CI
- Decide keep/revert (FR4) and stop (FR5)
- Write iteration record + reviewer scores transactionally
- On task completion, run fact-checker once (FR6)

### Lineage store
SQLite for relational data. Filesystem for content blobs at `data/articles/<article_id>/<version_id>.md`. WAL mode for concurrent readers.

### Editor adapter
```python
def propose(
    incumbent_text: str,
    prior_reviews: list[ReviewerOutput],   # last iteration only (see Context management)
    must_do_text: str,
    must_not_do_text: str,
    human_notes: list[str],                # consumed-after-use
) -> Candidate
```
Returns `Candidate(text, edit_summary, prompt_hash, model)`.

### Reviewer adapter
Two calls per iteration per reviewer (FR3):
```python
def score_rubric(candidate_text: str, persona: Persona) -> RubricScores
def compare_pairwise(candidate_text: str, incumbent_text: str, persona: Persona) -> Pairwise
```
- `RubricScores`: `{dim: float}` plus `prompt_hash`, `model`
- `Pairwise`: `pref ∈ {candidate, incumbent, tie}`, `rationale`, `prompt_hash`, `model`

### Fact-checker adapter
```python
def fact_check(final_text: str) -> FactCheckReport
```
Single call at end of loop (FR6). Configured to use a different provider/model than the editor.

### HTTP API
Thin async layer (FastAPI). Endpoints below.

### CLI
Reference client over HTTP. Wraps `make`-style commands from today's repo.

## Data model

```sql
CREATE TABLE articles (
    id              TEXT PRIMARY KEY,
    slug            TEXT NOT NULL,
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE article_versions (
    id                 TEXT PRIMARY KEY,
    article_id         TEXT NOT NULL REFERENCES articles(id),
    parent_version_id  TEXT REFERENCES article_versions(id),
    content_path       TEXT NOT NULL,
    editor_prompt_hash TEXT,
    editor_model       TEXT,
    edit_summary       TEXT,
    created_at         TIMESTAMP NOT NULL
);

CREATE TABLE rejected_candidates (
    id                 TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL REFERENCES tasks(id),
    parent_version_id  TEXT NOT NULL REFERENCES article_versions(id),
    content_path       TEXT NOT NULL,
    rejection_reason   TEXT NOT NULL,
    edit_summary       TEXT,
    editor_prompt_hash TEXT,
    editor_model       TEXT,
    created_at         TIMESTAMP NOT NULL
);

CREATE TABLE tasks (
    id                  TEXT PRIMARY KEY,
    article_id          TEXT NOT NULL REFERENCES articles(id),
    target_aggregate    REAL NOT NULL,
    reviewer_floor      REAL NOT NULL,
    loop_limit          INTEGER NOT NULL,
    must_do_text        TEXT NOT NULL DEFAULT '',
    must_not_do_text    TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL,        -- 'pending' | 'running' | 'done' | 'aborted'
    final_version_id    TEXT REFERENCES article_versions(id),
    stop_reason         TEXT,                 -- 'target_reached' | 'loop_limit' | 'aborted'
    created_at          TIMESTAMP NOT NULL,
    completed_at        TIMESTAMP
);

CREATE TABLE task_reviewers (
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    reviewer_id TEXT NOT NULL,
    weight      REAL NOT NULL,
    PRIMARY KEY (task_id, reviewer_id)
);

CREATE TABLE iteration_records (
    id                TEXT PRIMARY KEY,
    task_id           TEXT NOT NULL REFERENCES tasks(id),
    iter_index        INTEGER NOT NULL,
    parent_version_id TEXT NOT NULL,
    candidate_id      TEXT NOT NULL,         -- versions.id if kept, rejected.id otherwise
    decision          TEXT NOT NULL,         -- 'kept' | 'rejected'
    decision_reason   TEXT NOT NULL,
    aggregate_score   REAL NOT NULL,
    pref_delta        REAL NOT NULL,
    pref_ci_low       REAL NOT NULL,
    pref_ci_high      REAL NOT NULL,
    created_at        TIMESTAMP NOT NULL,
    UNIQUE (task_id, iter_index)
);

CREATE TABLE reviewer_scores (
    iteration_id          TEXT NOT NULL REFERENCES iteration_records(id),
    reviewer_id           TEXT NOT NULL,
    rubric_scores_json    TEXT NOT NULL,
    rubric_aggregate      REAL NOT NULL,
    pairwise_pref         TEXT NOT NULL,     -- 'candidate' | 'incumbent' | 'tie'
    pairwise_rationale    TEXT,
    rubric_prompt_hash    TEXT NOT NULL,
    pairwise_prompt_hash  TEXT NOT NULL,
    reviewer_model        TEXT NOT NULL,
    PRIMARY KEY (iteration_id, reviewer_id)
);

CREATE TABLE fact_check_reports (
    id                       TEXT PRIMARY KEY,
    task_id                  TEXT NOT NULL UNIQUE REFERENCES tasks(id),
    final_version_id         TEXT NOT NULL REFERENCES article_versions(id),
    status                   TEXT NOT NULL, -- 'clean' | 'has_findings'
    report_json              TEXT NOT NULL,
    fact_checker_model       TEXT NOT NULL,
    fact_checker_prompt_hash TEXT NOT NULL,
    created_at               TIMESTAMP NOT NULL
);

CREATE TABLE human_notes (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks(id),
    note_text    TEXT NOT NULL,
    consumed_at  TIMESTAMP,                  -- NULL until next iteration picks it up
    created_at   TIMESTAMP NOT NULL
);

CREATE TABLE idempotency_keys (
    task_id      TEXT NOT NULL REFERENCES tasks(id),
    key          TEXT NOT NULL,
    iteration_id TEXT REFERENCES iteration_records(id),
    PRIMARY KEY (task_id, key)
);
```

### FR-to-table mapping

| FR    | Tables / behavior                                                        |
|-------|--------------------------------------------------------------------------|
| FR1   | `articles`, `article_versions`, `rejected_candidates`                    |
| FR2   | `tasks` + `task_reviewers`                                               |
| FR3   | `reviewer_scores` (one row per reviewer per iteration; both calls)        |
| FR4   | `iteration_records.pref_ci_low > 0` ⇒ `decision = 'kept'`                |
| FR5   | Orchestrator stop check on `aggregate_score` and per-reviewer scores      |
| FR6   | `fact_check_reports`; written once per task at end of loop                |
| FR7   | `iteration_records` + `reviewer_scores` capture full per-iter context     |
| FR8   | `tasks.must_do_text` and `tasks.must_not_do_text` passed verbatim to editor as labeled sections |
| FR9   | `idempotency_keys` table; orchestrator checks before scheduling iter     |
| FR10  | Per-task asyncio lock; SQLite WAL; no shared mutable state across tasks  |
| FR11  | Orchestrator filters `task_reviewers.weight > 0` before adapter calls    |
| FR12  | Single SQLite transaction per iteration; resume on startup               |
| FR13  | Read endpoints join across tables                                        |

## API surface

```
POST   /articles                          create article from initial draft
                                          body: {slug, content}

GET    /articles/{id}                     article + current incumbent

POST   /tasks                             create task
                                          body: TaskConfig (FR2)

GET    /tasks/{id}                        status, current incumbent, stop_reason

GET    /tasks/{id}/lineage                full DAG: kept versions + rejected
                                          (FR13)

POST   /tasks/{id}/iterate                run iterations until stop or N reached
                                          headers: Idempotency-Key (FR9)
                                          body: {max_iterations?: int}

POST   /tasks/{id}/note                   inject human note for next iteration
                                          body: {text}; orchestrator consumes one-shot

POST   /tasks/{id}/abort                  stop the task; sets status='aborted'

GET    /tasks/{id}/fact-check-report      end-of-loop report (FR6)
```

All POST endpoints accept `Idempotency-Key`; only `iterate` semantically requires it but providing it everywhere costs nothing.

### TaskConfig

```json
{
  "article_id": "art_xyz",
  "reviewers": [
    {"reviewer_id": "investor", "weight": 0.4},
    {"reviewer_id": "engineer", "weight": 0.3},
    {"reviewer_id": "vp",       "weight": 0.3}
  ],
  "target_aggregate": 4.0,
  "reviewer_floor":   3.0,
  "loop_limit":       12,
  "must_do_text":     "include a concrete launch date; cite the source for every metric",
  "must_not_do_text": "no hype; no analogies to cars; no headers longer than 8 words"
}
```

## Concurrency model

- **Per-task lock**: asyncio lock keyed by `task_id`. Held for the duration of a single iteration. Concurrent `iterate` calls on the same task with different idempotency keys queue; with the same key, the second call returns the result of the first.
- **Cross-task**: independent. SQLite WAL allows concurrent readers; writes serialize automatically. Target N=10 concurrent tasks on distinct articles.
- **No shared mutable state** between tasks beyond the SQLite handle and provider clients.

### Sync Store + asyncio orchestrator (v1 decision)

`Store` is synchronous (`sqlite3` stdlib). The orchestrator is asyncio. For v1, we accept that DB calls block the event loop briefly: iteration writes are milliseconds while LLM calls dominate per-iteration cost (seconds). At the FR10 target of N=10 concurrent tasks, the threading lock around `Store.transaction()` is not a meaningful bottleneck.

If usage moves past ~10 concurrent tasks, two options:
1. Migrate to `aiosqlite` (async driver, methods become `async def`).
2. Wrap Store calls in `loop.run_in_executor(None, ...)` from the orchestrator without changing the Store API.

Either is a bounded refactor. We don't pre-pay for it in v1.

## Crash recovery (FR12)

Each iteration is a single transaction:

```
BEGIN
  INSERT article_versions OR rejected_candidates
  INSERT iteration_records
  INSERT reviewer_scores rows (one per reviewer)
  UPDATE tasks SET status, final_version_id, stop_reason  (only on terminal iter)
COMMIT
```

If the process dies mid-iteration, the transaction rolls back. On startup the orchestrator scans `tasks WHERE status = 'running'` and resumes from `MAX(iter_index) + 1` for each. No half-kept state is observable.

## Pairwise CI computation (FR4)

Per reviewer `r` with weight `w_r`, map pairwise pref to a signed score:
- `candidate` → `+1`
- `tie`       → `0`
- `incumbent` → `-1`

Reviewer signal: `s_r = w_r * pref_r`. Total: `S = Σ s_r`.

Bootstrap CI: resample reviewers with replacement N=1000 times, recompute weighted mean, take the 5th and 95th percentiles for a 90% CI. Keep iff `pref_ci_low > 0`.

**Caveat:** with only 4–8 reviewers, bootstrap CIs are wide and the lower bound is conservative. In practice `pref_ci_low > 0` requires near-unanimous preference. v1 ships with this rule; if it proves too strict in real use, swap for a fixed weighted-sum threshold (e.g. `S > 0.25`) — both options are recorded per iteration so we can compare retroactively.

## Provider abstraction

```python
class LLMProvider(Protocol):
    async def call(self, prompt: str, model: str, **kwargs) -> ProviderResponse: ...
```

Concrete implementations: `AnthropicProvider`, `OpenAIProvider`. Configured per adapter:

```toml
[editor]
provider = "anthropic"
model = "claude-opus-4-7"

[reviewer]
provider = "anthropic"
model = "claude-sonnet-4-6"

[fact_checker]
provider = "openai"
model = "gpt-5"
```

Editor and fact-checker SHOULD use different providers/models (best practice for v1; not enforced in code).

## Project layout

```
ghostwriter/
  api/                  FastAPI app
    __init__.py
    routes.py
    schemas.py          pydantic request/response models
  core/
    orchestrator.py     loop, scoring, gating
    bootstrap_ci.py     pairwise CI math
  adapters/
    editor.py
    reviewer.py
    fact_checker.py
    provider.py         LLMProvider protocol + concrete impls
  store/
    sqlite.py           connection, migrations
    queries.py          typed accessors per table
    schema.sql          DDL
  cli/
    __init__.py
    main.py
  tests/
    test_lineage.py
    test_orchestrator.py
    test_acceptance.py
```

Existing top-level files (`personas/`, `goal.md`, `writer.md`, etc.) stay where they are; the new service consumes them.

## Mapping milestones to design

1. **Skeleton** — directory layout above + `pyproject.toml` updates + empty modules with type stubs.
2. **Lineage store** — `store/sqlite.py`, `store/queries.py`, `store/schema.sql` + tests.
3. **Adapters** — editor, reviewer, fact-checker with provider abstraction.
4. **Orchestrator** — loop, CI, gating; integrates adapters and store.
5. **Fact-checker integration** — wire end-of-loop call.
6. **API + CLI** — FastAPI routes + CLI client.
7. **Acceptance tests** — A1–A6 from the requirements doc.

## Context management

Each adapter call has a strict context budget to prevent prompt bloat as iterations accumulate.

**Editor prompt** sees, per iteration:

| Section                              | Size            | Bounded?                          |
|--------------------------------------|-----------------|-----------------------------------|
| Goal                                 | hundreds tok.   | static                            |
| `must_do_text`                       | small           | static                            |
| `must_not_do_text`                   | small           | static                            |
| Current incumbent text               | thousands tok.  | bounded by article size           |
| **Last iteration's reviewer feedback** | varies        | bounded — only most recent iter   |
| Pending human notes                  | small           | consumed-after-use                |

**Key rule:** the editor only receives the *most recent* iteration's reviewer feedback. Older iterations are queryable in the lineage store for human inspection but never re-injected. This caps editor prompt size at `O(article + reviewers)` regardless of `loop_limit`.

**Pending human notes** are consumed atomically: when an iteration includes a note, the orchestrator marks `human_notes.consumed_at = NOW()` in the same transaction that writes the iteration record. They never re-appear in subsequent prompts.

**Reviewer prompts** see only the candidate text, the reviewer's persona, the rubric definition, and (for the pairwise call) the incumbent. No iteration history, no other reviewers' personas or scores, no cross-reviewer contamination.

**Fact-checker prompt** sees only the final version text and (optionally) a curated source list. No iteration history.

This isolation has a cost: the editor doesn't see *why* prior candidates were rejected beyond the most recent reviewer feedback. If that turns out to matter, v2 can add a compressed history channel — but we ship v1 without it and observe whether convergence suffers.

## Open questions for the next milestone

1. **Reviewer rubric dimensions** — keep today's per-persona rubric (4–6 dims defined in `personas/*.md`) or normalize to a fixed set across reviewers? Affects `RubricScores` schema.
2. **Fact-checker retrieval substrate** — web search via what provider? Or curated source list only? v1 default proposed: curated sources via a `sources.toml`, web search behind a flag.
3. **Human note injection** — append verbatim to editor prompt under a "Human notes" section, or expose as a structured field that the editor system prompt explicitly references? Recommended: append verbatim under a labeled section; structured later if needed.
