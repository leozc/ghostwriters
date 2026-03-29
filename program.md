# ghostwriter

This is an experiment to have an LLM autonomously improve a blog post, inspired by [autoresearch](https://github.com/karpathy/autoresearch).

## Preflight checks

Before anything else, verify dependencies are available:

```bash
# Required tools
uv --version          # uv package manager (for running Python scripts)
git --version         # git (for branch management and ratchet)
codex --version       # OpenAI Codex CLI (for dual-model reader comments)

# If any are missing, tell the user:
#   uv:    curl -LsSf https://astral.sh/uv/install.sh | sh
#   codex: npm install -g @openai/codex
#   git:   should already be installed

# Install Python dependencies
uv sync
```

If `codex` is not installed, the loop can still run with Claude-only readers (skip the Codex CLI calls in step 6). Tell the user and proceed.

## Setup

To set up a new optimization run, work with the user to:

1. **Agree on a run tag**: propose a tag based on today's date and the post topic (e.g. `bitter-lesson-mar29`). The branch `ghostwriter/<tag>` must not already exist.
2. **Create the branch**: `git checkout -b ghostwriter/<tag>` from current state.
3. **Initialize data/**: `uv run data.py init <path_to_source_draft>`. This creates `data/draft.md` as the working copy. All autoresearch artifacts will live in `data/`.
4. **Read the in-scope files**:
   - `goal.md` -- what the post should accomplish, tone, constraints, stopping criteria.
   - `config.toml` -- thresholds and focus point allocation.
   - `personas/*.md` -- the evaluation panel and their rubrics (one file per persona).
   - `writer.md` + `writer_config.md` -- writer agent instructions and voice config.
   - `data/draft.md` -- the blog post you will be improving. **This is the working draft. All edits go here.**
5. **Allocate focus points**: Present the loaded personas to the user and ask them to distribute 100 focus points. Show the current allocation from `config.toml` as a default. Update `config.toml` with the user's allocation.
6. **Confirm and go**: Confirm setup looks good with the user.

Once you get confirmation, kick off the experimentation.

## Experimentation

Each iteration evaluates the draft against multiple personas using **subagents**.

**What you CAN do:**
- Modify `data/draft.md` — this is the only file you edit. Everything is fair game: structure, tone, arguments, evidence, examples, section ordering, additions, deletions.
- Create files in `research/` — save web research findings here for reference across iterations.
- Use `WebSearch` to find data, statistics, market information, regulatory developments.
- Use `/browse` (gstack skill) to read specific web pages in depth.

**What you CANNOT do:**
- Modify `evaluate.md` or `score.py`. They are read-only. They define the fixed evaluation harness.
- Modify `personas/*.md` or `goal.md`. These are set by the human.
- Install new packages or add dependencies beyond what's in `pyproject.toml`.

**The goal is simple: get the highest min_score.** The min_score is the lowest score across ALL persona-rubric dimensions. The post is only as strong as its weakest impression. Everything is fair game: restructure, rewrite sections, add evidence, remove fluff, sharpen arguments, add examples.

**Simplicity criterion**: All else being equal, cleaner prose is better. A small score improvement that adds bloated paragraphs is not worth it. A score improvement from deleting weak content? Definitely keep. An equal score but tighter, sharper writing? Keep.

**The first run**: Your very first run should always be to establish the baseline — evaluate the draft as-is.

## Evaluation via subagents

**DO NOT call the Anthropic API directly. Use subagents.**

For each persona in `personas/` (excluding `_template.md`), spawn a subagent **in parallel** (all personas at once in a single message with multiple Agent tool calls). Each subagent:

1. Reads the persona file (`personas/<name>.md`)
2. Reads `draft.md`
3. Evaluates the draft 3 times from that persona's perspective
4. Returns structured JSON scores

### Subagent prompt

For each persona, spawn an Agent with `subagent_type: "general-purpose"` and this prompt:

```
You are an evaluation agent for ghostwriter. Your job is to evaluate a blog post draft
from the perspective of a specific persona, 3 separate times.

Read these two files:
- personas/<PERSONA_NAME>.md — your persona identity and rubric
- draft.md — the blog post to evaluate

For EACH of your 3 evaluation runs:
1. For each rubric dimension, reference a specific passage from the draft
2. Explain your reasoning in 1-2 sentences
3. Assign a score from 0 to 10
4. State whether the dealbreaker was triggered

After all 3 evaluations, output ONLY this JSON block at the very end of your response:

SCORES_JSON:
{
  "persona": "<PERSONA_NAME>",
  "runs": [
    {"<dim1>": <score>, "<dim2>": <score>, ...},
    {"<dim1>": <score>, "<dim2>": <score>, ...},
    {"<dim1>": <score>, "<dim2>": <score>, ...}
  ],
  "dealbreaker_triggered": false
}

Use the exact dimension names from the rubric. Scores must be integers 0-10.
If the dealbreaker triggers in ANY run, set dealbreaker_triggered: true and all scores to 0.
```

### Collecting results

After all subagents return:

1. Parse the `SCORES_JSON` from each subagent's response
2. Combine into a single JSON object:
   ```json
   {
     "investor": {"runs": [...], "dealbreaker": false},
     "vp_engineering": {"runs": [...], "dealbreaker": false},
     "senior_developer": {"runs": [...], "dealbreaker": false},
     "engineer": {"runs": [...], "dealbreaker": false}
   }
   ```
3. Write to `scores.json`
4. Run: `uv run score.py scores.json`
5. Read the output — this is your score summary

## Output format

`score.py` prints a summary like this:

```
---
min_score:        5.0
mean_score:       6.2
investor_min:     5.5
vp_engineering_min:       5.0
senior_developer_min:     6.0
engineer_min:     6.0
weakest:          vp_engineering/cost_reality
```

## Logging results

When an iteration is done, log it to `results.tsv` (tab-separated).

The TSV has a header row and 8 columns:

```
commit	min_score	mean_score	investor_min	vp_eng_min	sr_dev_min	engineer_min	status	description
```

1. git commit hash (short, 7 chars)
2. min_score
3. mean_score
4. investor_min (per-persona minimum)
5. vp_eng_min
6. sr_dev_min
7. engineer_min
8. status: `keep`, `discard`, or `crash`
9. short text description of what this iteration tried

Example:

```
commit	min_score	mean_score	investor_min	vp_eng_min	sr_dev_min	engineer_min	status	description
a1b2c3d	5.0	6.2	5.5	5.0	6.0	6.0	keep	baseline
b2c3d4e	5.5	6.5	6.0	5.5	6.5	6.0	keep	added industry benchmarks and cost data
c3d4e5f	5.5	6.3	6.5	5.5	6.0	5.5	discard	tried adding API code examples (felt forced)
```

## The experiment loop

The loop runs on a dedicated branch (e.g. `ghostwriter/bitter-lesson-mar29`).

## User input handling

The user may interrupt the loop at any time with additional context, facts, or direction. Examples:
- "compliance/Cost reality -> usually cost is $20-$40 per L1 investigation..."
- "add a section about cross-chain tracing"
- "tone it down in the opening"

When the user provides input:

1. **Always spawn the Writer agent first.** The writer incorporates the user's context into draft.md using the configured voice/tone. The orchestrator NEVER edits draft.md directly.
2. **Then commit and evaluate.** Same flow as a normal iteration — commit → evaluate + readers → keep/discard.
3. **Pass the user's context verbatim to the writer.** Include it in the writer prompt as:
   ```
   USER CONTEXT:
   [exact user input]

   YOUR TASK:
   Incorporate this context into draft.md. Use the author's voice from writer_config.md.
   Place it where it flows naturally. Don't just paste it in — write it as the author would.
   ```

This ensures the user's domain knowledge gets filtered through the writer's voice before evaluation.

---

LOOP FOREVER:

1. **Start iteration**: `uv run data.py new "<description>"`
   This creates the iteration folder, snapshots draft.md, and generates the diff.

2. **Check for user input**: if the user has provided new context, go to step 5 with the user's input as the diagnosis. Skip steps 3-4.

3. **Diagnose**: read the latest `data/iter_NN/summary.json` for scores and weakest dimension. Weight by focus points from config.toml: a low score on a high-focus persona is more urgent.

4. **Research** (if the weakness is data/evidence-related and no user input):
   - Use WebSearch to find relevant stats, examples, evidence
   - Use /browse to read specific pages for depth
   - Save findings to `research/<topic>.md`
   - If the weakness is structural/tonal, skip research and just edit

5. **Spawn the Writer agent** to make the edit. The writer agent is defined in `writer.md`. Spawn it with this prompt:

   ```
   You are the writer agent for ghostwriter.

   Read these files first:
   - writer.md -- your instructions, focus-point awareness, and anti-AI-slop rules
   - writer_config.md -- the author's voice, tone, and style configuration
   - config.toml -- focus points (persona weights)
   - goal.md -- strategic direction and constraints
   - data/draft.md -- the current blog post (working copy in data/)
   - comments.md -- latest HN and X reader reactions (if it exists)

   EVALUATION FEEDBACK:
   [paste the score summary + the detailed evaluator reasoning for the weakest dimensions]

   READER REACTIONS:
   [paste the HN and X comments from the previous iteration's reader agents]

   USER CONTEXT (if provided):
   [exact user input, or "None -- autonomous iteration"]

   DIAGNOSIS:
   The weakest persona-dimension is [X/Y] at score [Z] (focus weight: [W]).
   The evaluators said: [key quotes from their reasoning].
   The HN/X readers reacted: [key quotes from comments.md that are relevant].

   YOUR TASK:
   If USER CONTEXT is provided: incorporate that context into draft.md using the author's voice.
   If no user context: make ONE focused edit targeting the diagnosed weakness.
   You may restructure, merge, split, or reorder paragraphs if that's the best editorial move.
   Apply the edit directly to draft.md using the Edit tool.
   Follow writer_config.md for voice and tone. Follow the anti-AI-slop rules in writer.md strictly.
   Explain what you changed and why.
   ```

   The writer agent owns the voice. The orchestrator (you) does NOT write.

6. **git commit**: `git -c commit.gpgsign=false commit -m "ghostwriter: <description>"`

7. **Evaluate + Read**: spawn all agents in parallel:

   **4 Evaluator agents** (one per persona in `personas/`, excluding `_template.md` and `hn_reader.md` and `x_reader.md`). Each evaluates 3 times and returns SCORES_JSON. After each returns, save scores:
   ```
   uv run data.py save-scores <persona_name> '<SCORES_JSON>'
   ```

   **2 Claude Reader agents** (HN + X):
   ```
   # HN Reader
   Read personas/hn_reader.md and draft.md. Write 3 HN comments. Return prefixed with HN_COMMENTS:

   # X Reader
   Read personas/x_reader.md and draft.md. Write 3 X reactions. Return prefixed with X_COMMENTS:
   ```
   After each returns, save:
   ```
   uv run data.py save-comment claude hn '<comments>'
   uv run data.py save-comment claude x '<comments>'
   ```

   **2 Codex Reader agents** via CLI (run in parallel with the above):
   ```bash
   codex exec "Read personas/hn_reader.md and draft.md. Write 3 HN-style comments as that persona (2-5 sentences, terse). Do NOT edit files." 2>&1
   codex exec "Read personas/x_reader.md and draft.md. Write 3 X reactions as that persona (1-3 sentences, punchy). Do NOT edit files." 2>&1
   ```
   After each returns, save:
   ```
   uv run data.py save-comment codex hn '<comments>'
   uv run data.py save-comment codex x '<comments>'
   ```

8. **Finalize scores**: `uv run data.py finalize <keep|discard>`
   This computes medians from per-persona score files, writes summary.json, updates manifest.json.

9. **Keep or discard**:
   - Compare against previous iteration's summary.json
   - If min_score improved by >= min_improvement (from config.toml): **keep**
   - If min_score same AND mean_score improved by >= mean_improvement: **keep**
   - Otherwise: **discard** and `git reset --hard HEAD~1`

10. **Aggregate comments**: combine all 4 reader outputs (claude_hn, claude_x, codex_hn, codex_x) into `comments.md` for the writer to read next iteration.

11. **Check stopping criteria**: if min_score >= threshold from config.toml, announce completion.

12. **Continue** -- do not stop, do not ask the human.

**Stuck detection**: If you get 3 consecutive discards targeting the same weakness:
- Try a completely different approach to that dimension
- Try a structural change (reorder sections, split a section, add a new one)
- Do fresh web research on the topic
- Try targeting a different weakness instead

**Crashes**: If a subagent fails to return parseable scores, retry that persona only. If it fails 3 times, skip that persona for this iteration and note it in the log.

**NEVER STOP**: Once the loop has begun, do NOT pause to ask the human. The human may be away. You are autonomous. If you run out of ideas, re-read goal.md, re-read the personas, do more research, try radical structural changes. The loop runs until the human interrupts or the stopping criteria is met.

## Filesystem expectations

Before the loop starts:
```
ghostwriter/
├── program.md, writer.md, writer_config.md       (read-only system files)
├── goal.md, config.toml                           (user config)
├── personas/*.md                                  (persona definitions)
├── data.py, score.py                              (CLI tools)
├── research/                                      (web research findings, persisted across iterations)
└── data/                                          (created by init)
    ├── draft.md                                   (working draft)
    └── manifest.json                              (empty)
```

After N iterations:
```
data/
├── draft.md              <- current best version of the draft
├── manifest.json         <- index: [{id, status, min_score, mean_score, description}, ...]
├── iter_00/
│   ├── draft.md          <- snapshot at iteration 0
│   ├── diff.patch        <- empty for baseline
│   ├── summary.json      <- {id, status, min_score, mean_score, per_persona, weakest}
│   ├── scores/           <- one file per evaluator persona (parallel safe)
│   │   ├── investor.json
│   │   ├── vp_engineering.json
│   │   ├── senior_developer.json
│   │   └── engineer.json
│   └── comments/         <- one file per reader source (parallel safe)
│       ├── claude_hn.md
│       ├── claude_x.md
│       ├── codex_hn.md
│       └── codex_x.md
└── iter_NN/
    └── ...
```

## Hard rules

- The orchestrator (you) NEVER edits data/draft.md. Only the writer agent does.
- Use `git -c commit.gpgsign=false` for all commits.
- Run Codex via CLI (`codex exec "..." 2>&1`), not via Claude subagents.
- No em dashes in any writing. Banned as AI signature.
- Each persona's scores go in a separate file (parallel safe).
- Each reader's comments go in a separate file (parallel safe).
- Discarded iterations keep their data folder (useful for learning what didn't work).
