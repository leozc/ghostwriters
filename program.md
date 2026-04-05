# ghostwriter

This is an experiment to have an LLM autonomously improve a post, inspired by [autoresearch](https://github.com/karpathy/autoresearch).

## Preflight checks

Before anything else, verify dependencies are available:

```bash
# Required tools
uv --version          # uv package manager (for running Python scripts)
git --version         # git (for branch management and ratchet)
codex --version       # OpenAI Codex CLI (reader path + evaluator fallback)
codex login status    # should report a working Codex login

# If any are missing, tell the user:
#   uv:    curl -LsSf https://astral.sh/uv/install.sh | sh
#   codex: npm install -g @openai/codex
#   git:   should already be installed

# Install Python dependencies
uv sync
```

Read `config.toml` before starting:
- If `[eval].provider = "anthropic"` and `ANTHROPIC_API_KEY` is present, use Anthropic for evaluator runs.
- If `[eval].provider = "anthropic"` but `ANTHROPIC_API_KEY` is missing, fall back to Codex/OpenAI for evaluator runs.
- If `[eval].provider = "openai"` or `"codex"`, Codex CLI is required and must be logged in.

## Setup

To set up a new optimization run, work with the user to:

1. **Agree on a run tag**: propose a tag based on today's date and the post topic (e.g. `bitter-lesson-mar29`). The branch `ghostwriter/<tag>` must not already exist.
2. **Create the branch**: `git checkout -b ghostwriter/<tag>` from current state.
3. **Select platform**: Read `config.toml [platform].target` to see which platform is active (`xhs`, `x`, or `substack`). Confirm with the user. Change if needed.
4. **Initialize data/**: `uv run data.py init <path_to_source_draft>`. This creates `data/draft.md` as the working copy.
5. **Read the in-scope files**:
   - `goal.md` -- what the post should accomplish, tone, constraints, stopping criteria.
   - `config.toml` -- platform config, thresholds, and focus point allocation.
   - `personas/*.md` -- the evaluation panel and their rubrics. Only personas listed in `[platform.<target>]` and `[platform.universal]` are active.
   - `writer.md` + `writer_config.md` -- writer agent instructions and voice config.
   - `data/draft.md` -- the post you will be improving. **This is the working draft. All edits go here.**
6. **Review active evaluators and readers**: Based on the platform config, list which evaluators and readers will run. Show the focus points from `[focus.<platform>]`.
7. **Confirm and go**: Confirm setup looks good with the user.

Once you get confirmation, kick off the experimentation.

## Experimentation

Each iteration evaluates the draft against platform-specific personas using **subagents**.

**What you CAN do:**
- Modify `data/draft.md` -- this is the only file you edit. Everything is fair game: structure, tone, arguments, evidence, examples, section ordering, additions, deletions.
- Create files in `research/` -- save web research findings here for reference across iterations.
- Use `WebSearch` to find data, statistics, market information, regulatory developments.
- Use `/browse` (gstack skill) to read specific web pages in depth.

**What you CANNOT do:**
- Modify `evaluate.md` or `score.py`. They are read-only. They define the fixed evaluation harness.
- Modify `personas/*.md` or `goal.md`. These are set by the human.
- Install new packages or add dependencies beyond what's in `pyproject.toml`.

**The goal is simple: get the highest min_score.** The min_score is the lowest score across ALL active evaluator dimensions. The post is only as strong as its weakest impression. Everything is fair game: restructure, rewrite sections, add evidence, remove fluff, sharpen arguments, add examples.

**Simplicity criterion**: All else being equal, cleaner prose is better. A small score improvement that adds bloated paragraphs is not worth it. A score improvement from deleting weak content? Definitely keep. An equal score but tighter, sharper writing? Keep.

## Discovery phase (iteration 0)

**The first run establishes a baseline and optionally discovers which evaluators to keep.**

1. Evaluate the draft as-is against ALL evaluators for the target platform.
2. After scoring, run: `uv run data.py discover`
3. This analyzes scores and recommends which evaluators to keep:
   - **Keep**: evaluators with 2+ point spread across dimensions (discriminating) or any dimension below 6 (has weaknesses to improve)
   - **Consider dropping**: evaluators scoring uniformly high (not adding signal)
4. Review the recommendation with the user. If adjustments are needed, update `config.toml` focus points.
5. From iteration 1 onward, the evaluator set is locked for the optimization loop.

## Evaluation via subagents

**DO NOT call the Anthropic API directly. Use subagents.**

**CRITICAL: Each persona MUST be evaluated by a SEPARATE subagent.** Never combine multiple personas into a single agent call. Combining personas in one agent creates cross-contamination: the agent anchors on one persona's scores when evaluating the next. Independent subagents produce more honest, varied scores.

### Which personas to evaluate

Read `config.toml` to determine active personas:
- Evaluators (scoring): `[platform.universal].evaluators` + `[platform.<target>].evaluators`
- Readers (qualitative): `[platform.<target>].readers`

For each **evaluator** persona, spawn a subagent in parallel. For each **reader** persona, spawn a subagent (or use Codex CLI) in parallel. All agents run simultaneously in a single message with multiple Agent tool calls.

### Evaluator subagent prompt

For each evaluator persona, spawn an Agent with `subagent_type: "general-purpose"` and this prompt:

```
You are an evaluation agent for ghostwriter. Your job is to evaluate a post draft
from the perspective of a specific persona, 3 separate times.

Read these two files:
- personas/<PERSONA_NAME>.md -- your persona identity and rubric
- data/draft.md -- the post to evaluate

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

### Reader subagent prompt

For each reader persona, spawn an Agent with `subagent_type: "general-purpose"` and this prompt:

```
You are a reader agent for ghostwriter. Your job is to react to a post draft
from the perspective of a specific reader persona.

Read these two files:
- personas/<READER_NAME>.md -- your persona identity and comment instructions
- data/draft.md -- the post to react to

Follow the "How you comment" / "How you reply" instructions in the persona file exactly.
Write your comments/reactions. Return them prefixed with:

READER_COMMENTS:
<your comments here>
```

### Collecting results

After all subagents return:

1. Parse the `SCORES_JSON` from each evaluator subagent's response
2. Save each persona's scores: `uv run data.py save-scores <persona_name> '<SCORES_JSON>'`
3. Parse the `READER_COMMENTS` from each reader subagent's response
4. Save each reader's comments: `uv run data.py save-comment <reader_name> '<comments>'`

## The experiment loop

The loop runs on a dedicated branch (e.g. `ghostwriter/bitter-lesson-mar29`).

## User input handling

The user may interrupt the loop at any time with additional context, facts, or direction. When the user provides input:

1. **Always spawn the Writer agent first.** The writer incorporates the user's context into draft.md using the configured voice/tone. The orchestrator NEVER edits draft.md directly.
2. **Then commit and evaluate.** Same flow as a normal iteration.
3. **Pass the user's context verbatim to the writer.**

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
   - data/draft.md -- the current post (working copy in data/)
   - comments.md -- latest reader reactions (if it exists)

   EVALUATION FEEDBACK:
   [paste the score summary + the detailed evaluator reasoning for the weakest dimensions]

   READER REACTIONS:
   [paste the reader comments from the previous iteration]

   USER CONTEXT (if provided):
   [exact user input, or "None -- autonomous iteration"]

   DIAGNOSIS:
   The weakest persona-dimension is [X/Y] at score [Z] (focus weight: [W]).
   The evaluators said: [key quotes from their reasoning].
   The readers reacted: [key quotes from comments that are relevant].

   YOUR TASK:
   If USER CONTEXT is provided: incorporate that context into draft.md using the author's voice.
   If no user context: make ONE focused edit targeting the diagnosed weakness.
   You may restructure, merge, split, or reorder paragraphs if that's the best editorial move.
   Apply the edit directly to data/draft.md using the Edit tool.
   Follow writer_config.md for voice and tone. Follow the anti-AI-slop rules in writer.md strictly.
   Explain what you changed and why.
   ```

   The writer agent owns the voice. The orchestrator (you) does NOT write.

6. **git commit**: `git -c commit.gpgsign=false commit -m "ghostwriter: <description>"`

7. **Evaluate + Read**: spawn all agents in parallel. ALL evaluator agents + ALL reader agents MUST run every iteration. No exceptions. Reader comments are critical input for the writer agent. Skipping readers means the writer operates blind to how the draft reads to real audiences.

   **Evaluator agents** (one per active evaluator persona). Each evaluates 3 times and returns SCORES_JSON. After each returns, save scores:
   ```
   uv run data.py save-scores <persona_name> '<SCORES_JSON>'
   ```

   **Reader agents** (one per active reader persona). Each writes comments per their persona instructions. After each returns, save:
   ```
   uv run data.py save-comment <reader_name> '<comments>'
   ```

   Optionally, run Codex CLI readers in parallel for model diversity:
   ```bash
   codex exec "Read personas/<reader_name>.md and data/draft.md. Follow the comment instructions exactly. Do NOT edit files." 2>&1
   ```
   Save with a prefixed name: `uv run data.py save-comment codex_<reader_name> '<comments>'`

8. **Finalize scores**: `uv run data.py finalize <keep|discard>`
   This computes medians from per-persona score files, writes summary.json, updates manifest.json.

9. **Keep or discard**:
   - Compare against previous iteration's summary.json
   - If min_score improved by >= min_improvement (from config.toml): **keep**
   - If min_score same AND mean_score improved by >= mean_improvement: **keep**
   - Otherwise: **discard** and `git reset --hard HEAD~1`

10. **Aggregate comments**: combine reader outputs into `comments.md` for the writer to read next iteration.

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
├── personas/                                      (persona definitions)
│   ├── <evaluator>.md                             (scoring personas with rubrics)
│   ├── <reader>.md                                (comment-only personas)
│   └── disabled/                                  (inactive personas)
├── data.py, evaluate.py, score.py                 (CLI tools)
├── research/                                      (web research findings)
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
│   ├── discovery.json    <- evaluator analysis (baseline only)
│   ├── scores/           <- one file per evaluator persona (parallel safe)
│   │   ├── sharp_peer.json
│   │   ├── xhs_dev_diaspora.json
│   │   └── ...
│   └── comments/         <- one file per reader (parallel safe)
│       ├── xhs_commenter.md
│       ├── x_replier.md
│       └── ...
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
