# ghostwriter

Autonomous blog post optimization inspired by [autoresearch](https://github.com/karpathy/autoresearch). An AI agent loop that iteratively improves a blog post using multi-persona evaluation, dual-model reader critics, and a ratchet that only keeps improvements.

## How it works

```
                    +---------------+
                    |  goal.md      |  Human sets direction, tone, constraints
                    |  config.toml  |  Human sets persona weights (focus points)
                    +-------+-------+
                            |
                    +-------v-------+
                    | Writer Agent  |  Edits data/draft.md (one edit per iteration)
                    | writer.md     |  Reads: voice config, eval feedback, reader comments
                    +-------+-------+
                            |
                       git commit
                            |
               +------------+------------+
               |            |            |
      +--------v---+ +-----v-----+ +---v--------+
      | Evaluator  | | 2 Claude  | | 2 Codex    |
      | Personas   | | Readers   | | Readers    |
      | (parallel) | | (HN + X)  | | (HN + X)  |
      +--------+---+ +-----+-----+ +---+--------+
               |            |            |
               |    data.py save-scores  |
               |    data.py save-comment |
               +-----------+-------------+
                            |
                     data.py finalize
                            |
                    +-------v-------+
                    | Keep/Discard  |  min_score improved by >=0.5? Keep.
                    |  (ratchet)    |  Otherwise revert.
                    +-------+-------+
                            |
                       Loop forever
```

## Quick start

### Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [git](https://git-scm.com/)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (the agent loop runs inside Claude Code)
- [OpenAI Codex CLI](https://github.com/openai/codex) (optional, for dual-model reader comments)
- `ANTHROPIC_API_KEY` set in your environment

### Setup

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/ghostwriter.git
cd ghostwriter

# 2. Install dependencies
uv sync

# 3. Configure for your blog post
#    Edit these files for your domain:
#    - goal.md           -- the article goal: what the post should accomplish
#    - writer_config.md  -- author voice and tone
#    - config.toml       -- persona weights and eval thresholds
#    - personas/*.md     -- your evaluation panel (see "Creating personas" below)

# 4. Initialize with your draft
make init DRAFT=path/to/your_draft.md

# 5. Run tests (optional, verifies setup)
make test

# 6. Start the loop (in Claude Code)
#    Prompt: "Read program.md and let's kick off a new experiment."
```

### What happens next

Claude Code reads `program.md` and runs the optimization loop autonomously:

1. Evaluates the draft against your persona panel (parallel subagents)
2. Collects reader reactions from Claude and Codex (HN + X personas)
3. Spawns the writer agent to make one focused edit targeting the weakest dimension
4. Commits, re-evaluates, keeps or discards based on the ratchet
5. Loops until the stopping criteria is met or you interrupt

You can interrupt at any time to provide additional context, facts, or direction. The writer agent incorporates your input using the configured voice.

## File structure

```
ghostwriter/                  The system (ships clean, no runtime data)
|-- program.md                Orchestrator instructions (the "skill")
|-- writer.md                 Writer agent: writing style rules, anti-AI-slop, professional editor
|-- writer_config.md          Configurable voice/tone (CEO vs CTO, visionary vs practical)
|-- goal.md                   Article goal: what the post should accomplish, tone, constraints
|-- config.toml               Focus points, eval thresholds, stopping criteria
|-- evaluate.md               Evaluation protocol (fixed, do not modify)
|-- personas/                 One file per persona
|   |-- _template.md          Template for creating new personas
|   |-- investor.md           Example: Series A developer tools investor
|   |-- vp_engineering.md     Example: VP Engineering with budget authority
|   |-- senior_developer.md   Example: Senior developer (daily user)
|   |-- engineer.md           Example: Platform engineer (builder)
|   |-- hn_reader.md          Hacker News reader (critic, not scorer)
|   +-- x_reader.md           X / Twitter reader (critic, not scorer)
|-- data.py                   Iteration data management (CLI)
|-- evaluate.py               Standalone evaluation harness (API-based)
|-- score.py                  Scoring math (medians, aggregates)
|-- test_data.py              Unit tests
|-- Makefile                  Convenience commands
|-- pyproject.toml            Dependencies
+-- .gitignore                Excludes data/ (runtime artifacts)

data/                         Runtime artifacts (created by `make init`, gitignored)
|-- draft.md                  Working draft (writer edits this)
|-- manifest.json             Index of all iterations
+-- iter_NN/                  One folder per iteration
    |-- draft.md              Snapshot at this point
    |-- diff.patch            What changed from previous iteration
    |-- summary.json          Scores, status, weakest dimension
    |-- scores/               One file per evaluator persona (parallel safe)
    +-- comments/             One file per reader source (parallel safe)
```

## Key concepts

### The ratchet

Like autoresearch's val_bpb, the loop only advances on improvement. An edit that doesn't raise min_score by >= 0.5 (or mean_score by >= 0.3 as a tiebreaker) gets reverted. The draft never gets worse.

### Focus points

Distribute 100 points across personas in `config.toml`. Higher points = the writer prioritizes that persona's weak dimensions first. A persona at 40 points matters 4x more than one at 10.

### Dual-model readers

After each iteration, the draft gets reviewed by 4 reader agents: Claude as HN reader, Claude as X reader, Codex (GPT) as HN reader, Codex as X reader. Different models catch different things. Both models agreeing on a critique means it's real.

### Writer agent

The writer is a professional editor, not a text inserter. It can restructure, merge, split, reorder, cut, or rewrite. It reads focus points, evaluation scores, and reader comments. Writing style rules live in `writer.md` (anti-AI-slop patterns, editing approach, what good writing sounds like). Voice and tone are configurable via `writer_config.md` (author role, tone spectrum, vocabulary).

## Customizing for your domain

The repo ships with example personas for a developer tools domain. To use ghostwriter for your own content:

1. **Define your audience** in `personas/`. Copy `_template.md` and fill in:
   - **Identity**: who they are, what pressures they face (second person)
   - **What they care about**: what makes them pay attention
   - **Value proposition lens**: what would make them act (share, buy, take a meeting)
   - **Rubric**: 4-6 scored dimensions (0-10), each with a "what does 10 look like" description
   - **Dealbreaker**: what causes an instant zero

2. **Set your article goal** in `goal.md`: what the post should accomplish, the tone you want, what must be included or avoided. This is the north star the writer agent and evaluators use.

3. **Configure the voice** in `writer_config.md`: author role, tone spectrum, sentence style, vocabulary. The companion `writer.md` defines the writing style rules (anti-AI-slop patterns, editing approach, structural moves) and can also be customized.

4. **Allocate focus points** in `config.toml`: distribute 100 points across your personas.

## Makefile commands

```bash
make init DRAFT=my_post.md        # Bootstrap data/ from a source draft
make new DESC="added intro stats"  # Start a new iteration
make status                        # Show iteration history
make finalize-keep                 # Mark current iteration as kept
make finalize-discard              # Revert and mark as discarded
make test                          # Run unit tests
make clean                         # Wipe data/ for a fresh run
```

## data.py CLI

```bash
uv run data.py init source.md                              # Initialize
uv run data.py new "description"                           # New iteration
uv run data.py save-scores investor '{"runs":[...]}'       # Save persona scores
uv run data.py save-comment claude hn "1. Good. 2. Fix."   # Save reader comment
uv run data.py finalize keep                               # Compute scores, update manifest
uv run data.py status                                      # Show history
```

## How the evaluation works

Each persona evaluates the draft 3 times (for noise reduction via median). Scores are per-dimension (0-10). The aggregate metrics:

- **min_score**: lowest score across all persona-dimensions. The post is only as strong as its weakest impression.
- **mean_score**: average across all dimensions. Used as tiebreaker.
- **weakest**: the specific persona/dimension dragging down the score, weighted by focus points.

The ratchet keeps an edit only if min_score improves by >= 0.5, or (same min_score AND mean_score improves by >= 0.3).

## License

MIT
