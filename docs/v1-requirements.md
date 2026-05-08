# Ghostwriter v1 Redesign — Requirements

## Context

Ghostwriter today is a single-user CLI loop. The author drops a draft, configures personas plus weights, target score, loop limit, and free-text guardrails. The loop iterates: edit → score → keep if the weakest score improved by ≥ 0.5, otherwise revert. It works, but four gaps motivate this redesign:

1. **No factual gate.** Hallucinations and bad citations can be promoted.
2. **Score-gaming risk.** Drafts can win on rubric points while degrading for humans (length inflation, hedging, keyword stuffing).
3. **No version lineage.** Once a commit lands, the rationale, alternatives, and evidence aren't recoverable.
4. **Single article, single task.** Can't run multiple optimizations concurrently or against different goals on the same article.

## Goal

A single-tenant service version of ghostwriter that addresses all four gaps while preserving the existing workflow shape.

## Users and jobs-to-be-done

Single tenant: one author or a small team sharing a service.

- *When I'm drafting several posts at once, I want to start optimization tasks and walk away.*
- *When a version is kept, I want to see why it won — scores, preferences, evidence.*
- *When the loop finishes, I want a factual report on the final draft.*
- *When I learn something mid-task, I want to inject a note that the next iteration's editor incorporates.*

## Scope

**In v1**
- Single-tenant service: HTTP API + CLI client + local persistence.
- Multiple articles, multiple concurrent tasks per tenant.
- Reviewer panel (today's personas, formalized) with weights and rubric scoring.
- Editor with free-text guardrails.
- Pairwise-preference promotion gate with bootstrap CI.
- Lineage DAG including rejected candidates.
- Advisory fact-checker run once at end of loop, regardless of outcome.

**Out of v1 (deferred)**
- Per-audience agent router with provider/model policy and fallback chains.
- Held-out judge / drift detection.
- Automated calibration loop against human labels.
- Built-in structured guardrails (length band, style linter).
- Multi-tenant, auth, quotas, billing, public API.

## Workflow

**Inputs**
- draft article
- reviewers and weights
- `target_aggregate` (e.g., 4.0)
- `reviewer_floor` (safety net; set generously low, e.g., 3.0)
- `loop_limit`
- `must_do`: free-text required behaviors (may be empty)
- `must_not_do`: free-text prohibited behaviors (may be empty)

**Loop**

```
while not (aggregate >= target_aggregate
           AND every reviewer's rubric_score >= reviewer_floor)
      and iter < loop_limit:

    candidate = editor(draft, prior_reviews, guardrails)

    for each reviewer r with weight > 0:
        rubric_call(r, candidate)         -> rubric_scores_r
        pairwise_call(r, candidate, inc)  -> preference_r

    pref_delta = sum_r [ w_r * (P(cand > inc | r) - 0.5) ]
    aggregate  = sum_r [ w_r * rubric_score_r ]

    if CI_low(pref_delta) > 0:
        keep candidate as new incumbent
        record_lineage(kept, ...)
    else:
        revert
        record_lineage(rejected, reason="no_pref_improvement")
```

**Post-loop**
- `fact_checker(final_version)` → advisory report attached, never blocks.

**Cost shape:** with N reviewers and L iterations, reviewer calls = `2 * N * L`; editor calls = `L`; fact-check calls = `1`.

## Functional requirements

- **FR1** Articles, kept versions, and rejected candidates form an immutable parent-child DAG.
- **FR2** A task identifies one article, reviewers and weights, `target_aggregate`, `reviewer_floor`, `loop_limit`, and free-text `must_do` and `must_not_do` guardrails.
- **FR3** Each iteration: editor produces a candidate; each reviewer with weight > 0 is called twice — once for rubric scores on the candidate, once for pairwise preference vs. incumbent.
- **FR4** Candidate is kept iff bootstrap CI lower bound of weighted `pref_delta` > 0; otherwise reverted with reason.
- **FR5** Loop terminates when `aggregate >= target_aggregate` AND every reviewer's `rubric_score >= reviewer_floor`, or when `iter >= loop_limit`.
- **FR6** Fact-checker runs exactly once at end of loop on the final version, regardless of termination reason (target reached, loop limit, or aborted); output is advisory and never blocks or reverts.
- **FR7** Every iteration (kept or reverted) writes a lineage record: parent_id, candidate_id, edit summary, per-reviewer rubric scores, per-reviewer preferences, decision, reason, prompt hashes, model IDs.
- **FR8** Free-text `must_do` and `must_not_do` are passed verbatim into the editor prompt as two labeled sections; not structurally enforced.
- **FR9** `iterate` is idempotent per `(task_id, idempotency_key)`.
- **FR10** Multiple tasks run concurrently across different articles without state corruption.
- **FR11** Reviewers with weight = 0 are not called and excluded from both gates.
- **FR12** Crash mid-iteration leaves no half-kept state; on restart, resume or clean rollback.
- **FR13** Author can inspect full lineage and the fact-check report for any task.

## Quality requirements

- **Auditability.** Every kept version is reconstructible from stored prompts, inputs, and model IDs.
- **Concurrency.** Target N=10 concurrent tasks across distinct articles without interference.
- **Reliability.** Orchestrator restart-safe; in-flight iterations either complete or roll back cleanly.
- **Privacy.** Drafts and reviewer/fact-check prompts stay on the tenant's host; outbound traffic only to configured providers.

## Acceptance criteria

- **A1** Author creates a task, runs the loop, receives a final draft + lineage + fact-check report.
- **A2** Lineage view exposes kept versions and rejected candidates with reasons.
- **A3** Limit test: `loop_limit = N` ⇒ at most N iterations.
- **A4** Concurrency test: two tasks on different articles complete without interference.
- **A5** Crash test: kill mid-iteration; on restart no half-kept version exists.
- **A6** Stop test: a task that reaches `target_aggregate` and `reviewer_floor` halts before `loop_limit`.

## Key design decisions and tradeoffs

- **Pairwise preference (with bootstrap CI) replaces "weakest score improved by 0.5" as the keep/revert rule.** More robust than a noisy absolute-score delta. Costs one extra reviewer call per iteration.
- **Aggregate target + per-reviewer floor.** Aggregate-only lets a high-weight reviewer get drowned out. Per-reviewer-only stalls on one stubborn reviewer. The floor is a generous safety net.
- **Fact-checker is always advisory, runs once at end of loop.** Author keeps decision authority on factual issues. Tradeoff: factually wrong drafts can still be marked complete if the human ignores the report.
- **Free-text guardrails only (`must_do` + `must_not_do`).** No built-in length or style enforcement in v1. Pairwise preference partially protects against length/style drift but not fully. Acceptable for v1; revisit if drift observed.
- **No held-out judge in v1.** Anti-gaming relies on pairwise being more robust than absolute scores. Add later if needed.

## Out of scope (deferred to later versions)

- Per-audience agent routing with provider/model policy and fallback chains.
- Automated calibration / drift detection against human labels.
- Built-in structured guardrails (length band, style linter, forbidden phrases).
- Multi-tenant, auth, quotas, billing.
- Public API / SaaS.
