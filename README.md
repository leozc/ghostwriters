# ghostwriter

**[autoresearch](https://github.com/karpathy/autoresearch), but for writing.**

Karpathy's autoresearch showed that an autonomous loop of "try, evaluate, keep or discard" can optimize research code without human intervention. Ghostwriter applies the same idea to prose.

You write the first draft. Ghostwriter assembles a **panel of AI agents** -- each one a different expert persona (investor, engineer, VP, end user) -- and has them score your draft independently, in parallel. A separate set of **reader agents** (simulating Hacker News commenters and X/Twitter reactions) poke holes from the outside. Two different foundation models (Claude and GPT via Codex) serve as readers so you get genuine disagreement, not echo-chamber feedback.

Then a **writer agent** reads all the scores and comments, diagnoses the weakest point, and makes one surgical edit. The loop commits, re-evaluates, and keeps the edit only if the weakest score improved. If not, it reverts. **Your draft never gets worse. It only gets sharper.**

The result: a blog post that has been stress-tested against multiple expert perspectives, tightened by a style-aware editor, and validated by readers from two competing AI models -- all while you were getting coffee.

---

## Get started in 3 steps

### 1. Bring your draft and set the article goal

```bash
git clone https://github.com/leozc/ghostwriters
cd ghostwriters && uv sync
```

Drop your blog post draft into the repo (e.g. `myblog.md`), then edit **`goal.md`** to tell ghostwriter what the article should accomplish: the thesis, the tone, what must be included, what to avoid.

`goal.md` is the north star. The writer agent and every evaluator reads it.

### 2. (Optional) Tune the personas and voice

The repo ships with ready-to-use example personas. Customize if you want:

| File | What it controls |
|---|---|
| `personas/*.md` | Your evaluation panel. Who reads and scores the draft. Copy `_template.md` to create new ones. |
| `config.toml` | Focus points: distribute 100 points across personas. Higher = writer prioritizes that reader. |
| `writer_config.md` | Author voice and tone (CEO vs CTO, visionary vs practical, formal vs conversational). |
| `writer.md` | Writing style rules: anti-AI-slop patterns, editing approach, what good prose sounds like. |

### 3. Run the loop in Claude Code

```bash
make init DRAFT=myblog.md
```

Then open [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and prompt:

```
Read program.md and start the autoresearch loop from @myblog.md
```

That's it. Walk away. Come back to a better draft.

---

## How the multi-agent loop works

Each iteration spawns **8+ agents in parallel**:

```
  You write the draft + goal.md
          |
    Writer Agent              1 agent: makes one surgical edit
          |
     git commit
          |
    +-----+-----+-----+-----+-----+-----+-----+-----+
    |     |     |     |     |     |     |     |     |
   Inv  VP Eng Sr Dev Engr  Claude Claude Codex Codex    8 agents, parallel
   eval  eval  eval  eval   (HN)   (X)   (HN)   (X)
    |     |     |     |     |     |     |     |     |
    +-----+-----+-----+-----+-----+-----+-----+-----+
          |
    Keep or Discard       Weakest score improved? Keep. Otherwise git revert.
          |
      Loop forever
```

**4 expert evaluators** -- each persona reads the draft independently 3 times (for noise reduction via median) and scores it on 4-6 rubric dimensions. An investor looks for thesis clarity and founder signal. An engineer looks for technical substance and builder energy. A VP looks for cost reality and strategic credibility. They don't agree with each other, and that's the point.

**4 reader critics** -- Claude and Codex (GPT) each simulate a Hacker News commenter and an X/Twitter reactor. Two models, two platforms, four distinct voices. Codex tends to be harsher. When both models flag the same weakness, it's real. When only one does, the writer weighs it but doesn't over-rotate.

**1 writer agent** -- reads all scores, all comments, the focus point weights, and the voice config. Diagnoses the single highest-impact weakness and makes one focused edit. Not a text inserter: a professional editor that can restructure, merge, split, cut, or rewrite.

**The ratchet** -- every edit is evaluated. If the weakest score doesn't improve by >= 0.5, the commit is reverted. The draft monotonically improves, like autoresearch's val_bpb. Bad edits are never kept.

**Focus points** -- you distribute 100 points across personas in `config.toml`. A persona at 40 points matters 4x more than one at 10. The writer fixes the highest-weighted weakness first.

**Human-in-the-loop** -- you can interrupt anytime to inject domain knowledge, facts, or direction. The writer agent incorporates your input in the configured voice and the loop continues. The biggest wins in practice come from these human interrupts, not autonomous iterations.

---

## Prerequisites

- [uv](https://docs.astral.sh/uv/) -- Python package manager
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) -- the agent loop runs here
- [OpenAI Codex CLI](https://github.com/openai/codex) -- optional, for dual-model reader comments
- `ANTHROPIC_API_KEY` in your environment

---

## Customizing for your domain

The example personas target a developer tools audience. To adapt ghostwriter for any domain:

1. **Define your readers** -- copy `personas/_template.md` and fill in identity, rubric (4-6 scored dimensions), and dealbreaker.
2. **Set the article goal** -- edit `goal.md` with your thesis, tone, must-include/must-avoid lists.
3. **Configure the voice** -- edit `writer_config.md` for author role, tone spectrum, sentence style.
4. **Allocate focus** -- distribute 100 points in `config.toml` across your personas.

---

## Project structure

```
ghostwriter/
|-- program.md            Orchestrator instructions (the "brain")
|-- writer.md             Writing style: anti-AI-slop rules, editing approach
|-- writer_config.md      Author voice: tone spectrum, vocabulary, sentence style
|-- goal.md               Article goal: what the post should accomplish
|-- config.toml           Focus points, eval thresholds, stopping criteria
|-- evaluate.md           Evaluation protocol (fixed)
|-- personas/             One file per reader persona
|-- data.py               Iteration data management CLI
|-- evaluate.py           Standalone evaluation harness
|-- score.py              Scoring math (medians, aggregates)
|-- test_data.py          Unit tests (19 tests)
+-- Makefile              Convenience commands
```

**Runtime artifacts** (gitignored, created by `make init`):

```
data/
|-- draft.md              Working draft (writer edits this)
|-- manifest.json         Iteration index
+-- iter_NN/              Per-iteration snapshots, scores, comments
```

---

## Makefile commands

```bash
make init DRAFT=my_post.md        # Bootstrap data/ from your draft
make test                          # Run unit tests
make status                        # Show iteration history
make clean                         # Wipe data/ for a fresh run
```

---

## License

MIT
