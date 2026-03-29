# Evaluation Protocol

This file defines how blog post drafts are evaluated. **DO NOT MODIFY.**

## Process

For each persona file in `personas/` (excluding `_template.md`):

1. **Spawn a subagent** for that persona. Each subagent runs independently (all personas evaluate in parallel).
2. Each subagent evaluates the draft **3 times** (for noise reduction via median).
3. Each subagent returns structured JSON scores.
4. After all subagents complete, run `uv run score.py <scores.json>` to compute the summary.

## Subagent prompt template

For each persona, spawn an Agent with this prompt (filling in the persona fields):

```
You are evaluating a blog post draft from the perspective of a specific persona.
You will evaluate it 3 separate times and return all 3 sets of scores.

PERSONA IDENTITY:
{identity}

WHAT YOU CARE ABOUT:
{care}

HOW YOU EVALUATE VALUE:
{value}

RUBRIC (score each dimension 0-10):
{rubric_lines}

DEALBREAKER: If the following is true, score ALL dimensions as 0:
{dealbreaker}

---

Read the file: draft.md

---

INSTRUCTIONS:
You must evaluate this draft 3 SEPARATE TIMES. For each evaluation:
1. For each rubric dimension, quote or reference a specific passage, explain in 1-2 sentences, and assign a score 0-10.
2. Provide your overall reaction as this persona in 2-3 sentences.
3. State whether the dealbreaker was triggered.

After all 3 evaluations, output a JSON summary in this EXACT format (and nothing else after the JSON):

SCORES_JSON:
{
  "persona": "<persona_name>",
  "runs": [
    {"dim1": score, "dim2": score, ...},
    {"dim1": score, "dim2": score, ...},
    {"dim1": score, "dim2": score, ...}
  ],
  "dealbreaker_triggered": false
}

Replace dim1, dim2, etc. with the actual rubric dimension names.
Scores must be integers 0-10.
If the dealbreaker is triggered in ANY run, set dealbreaker_triggered to true and all scores to 0.
```

## Collecting results

After all subagents return:
1. Parse the SCORES_JSON from each subagent's response
2. Write all scores to a temporary `scores.json` file:
   ```json
   {
     "persona_name": {"runs": [{"dim": score, ...}, ...], "dealbreaker": false},
     ...
   }
   ```
3. Run: `uv run score.py scores.json`
4. The script prints the summary in autoresearch-style format and the weakest dimension.

## Keep/discard rules (from config.toml)

- **KEEP** if: new `min_score` > current `min_score` by >= `min_improvement` (default 0.5)
- **KEEP** if: same `min_score` AND new `mean_score` > current `mean_score` by >= `mean_improvement` (default 0.3)
- **DISCARD** otherwise

## Noise mitigation

- Triple evaluation with median per dimension
- Improvement thresholds prevent noise-driven phantom improvements
- Reasoning-first scoring: evaluator must justify before scoring, grounding in specific text
