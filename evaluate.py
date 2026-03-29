"""
ghostwriter evaluation harness. Fixed — do not modify.
Evaluates draft.md against persona rubrics using Claude API.
Usage: uv run evaluate.py
"""

import os
import re
import sys
import json
import statistics
import tomllib
from pathlib import Path

import anthropic

# ---------------------------------------------------------------------------
# Configuration (loaded from config.toml)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent

def load_config() -> dict:
    config_path = BASE_DIR / "config.toml"
    if not config_path.exists():
        print("ERROR: config.toml not found", file=sys.stderr)
        sys.exit(1)
    with open(config_path, "rb") as f:
        return tomllib.load(f)

CONFIG = load_config()
MODEL = CONFIG["eval"]["model"]
EVAL_RUNS = CONFIG["eval"]["runs"]
EVAL_TEMPERATURE = CONFIG["eval"]["temperature"]
MIN_SCORE_THRESHOLD = CONFIG["eval"]["min_improvement"]
MEAN_SCORE_THRESHOLD = CONFIG["eval"]["mean_improvement"]
FOCUS_POINTS = CONFIG.get("focus", {})

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_persona_file(path: Path) -> dict:
    """Parse a persona markdown file into structured data."""
    text = path.read_text()
    name = path.stem  # filename without extension

    # Extract identity (## Identity section)
    identity_match = re.search(
        r"## Identity\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL
    )
    identity = identity_match.group(1).strip() if identity_match else ""
    # Strip HTML comments
    identity = re.sub(r"<!--.*?-->", "", identity, flags=re.DOTALL).strip()

    # Extract what they care about
    care_match = re.search(
        r"## What they care about\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL
    )
    care = care_match.group(1).strip() if care_match else ""
    care = re.sub(r"<!--.*?-->", "", care, flags=re.DOTALL).strip()

    # Extract value proposition lens
    value_match = re.search(
        r"## Value proposition lens\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL
    )
    value = value_match.group(1).strip() if value_match else ""
    value = re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL).strip()

    # Extract rubric dimensions
    rubric_match = re.search(
        r"## Rubric\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL
    )
    rubric_text = rubric_match.group(1).strip() if rubric_match else ""
    rubric_text = re.sub(r"<!--.*?-->", "", rubric_text, flags=re.DOTALL).strip()

    dimensions = []
    for line in rubric_text.split("\n"):
        dim_match = re.match(
            r"- \*\*(.+?)\*\*\s*\(0-10\):\s*(.+)", line.strip()
        )
        if dim_match:
            dimensions.append({
                "name": dim_match.group(1).strip(),
                "description": dim_match.group(2).strip(),
            })

    # Extract dealbreaker
    deal_match = re.search(
        r"## Dealbreaker\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL
    )
    dealbreaker = deal_match.group(1).strip() if deal_match else ""
    dealbreaker = re.sub(r"<!--.*?-->", "", dealbreaker, flags=re.DOTALL).strip()

    return {
        "name": name,
        "identity": identity,
        "care": care,
        "value": value,
        "dimensions": dimensions,
        "dealbreaker": dealbreaker,
    }


def load_focus_points() -> dict[str, int]:
    """Load focus point allocation from config.toml [focus] section."""
    return {k: int(v) for k, v in FOCUS_POINTS.items()}


# ---------------------------------------------------------------------------
# Evaluation prompt
# ---------------------------------------------------------------------------

def build_eval_prompt(persona: dict, draft: str) -> str:
    """Build the evaluation prompt for a persona."""
    rubric_lines = "\n".join(
        f"- {d['name']} (0-10): {d['description']}"
        for d in persona["dimensions"]
    )

    dealbreaker_section = ""
    if persona["dealbreaker"]:
        dealbreaker_section = f"""
DEALBREAKER: If the following is true, score ALL dimensions as 0:
{persona['dealbreaker']}
"""

    return f"""You are evaluating a blog post draft from the perspective of a specific persona.

PERSONA IDENTITY:
{persona['identity']}

WHAT YOU CARE ABOUT:
{persona['care']}

HOW YOU EVALUATE VALUE:
{persona['value']}

RUBRIC (score each dimension 0-10):
{rubric_lines}
{dealbreaker_section}
---

DRAFT TO EVALUATE:

{draft}

---

INSTRUCTIONS:
For each rubric dimension, you MUST:
1. Quote or reference a specific passage from the draft that informs your judgment
2. Explain your reasoning in 1-2 sentences
3. Assign a score from 0 to 10

Then provide your overall reaction as this persona in 2-3 sentences.

RESPOND IN THIS EXACT FORMAT (one line per dimension, then overall):

{chr(10).join(f'{d["name"]}: [your assessment referencing specific text] -> [score as integer]' for d in persona["dimensions"])}
OVERALL: [your 2-3 sentence reaction as this persona]
DEALBREAKER_TRIGGERED: [yes/no]"""


def parse_eval_response(response: str, persona: dict) -> dict[str, int] | None:
    """Parse scores from an evaluation response. Returns dict of dimension->score."""
    scores = {}
    for dim in persona["dimensions"]:
        pattern = re.compile(
            rf"{re.escape(dim['name'])}:\s*.*?->\s*(\d+)", re.IGNORECASE
        )
        match = pattern.search(response)
        if match:
            score = int(match.group(1))
            scores[dim["name"]] = min(max(score, 0), 10)  # clamp 0-10

    # Check dealbreaker
    if re.search(r"DEALBREAKER_TRIGGERED:\s*yes", response, re.IGNORECASE):
        for dim in persona["dimensions"]:
            scores[dim["name"]] = 0

    if len(scores) != len(persona["dimensions"]):
        return None  # incomplete parse

    return scores


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def evaluate_persona(client: anthropic.Anthropic, persona: dict, draft: str) -> list[dict[str, int]]:
    """Run EVAL_RUNS evaluations for a single persona. Returns list of score dicts."""
    prompt = build_eval_prompt(persona, draft)
    results = []

    for run in range(EVAL_RUNS):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=2000,
                temperature=EVAL_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text
            scores = parse_eval_response(text, persona)

            if scores is None:
                print(f"  WARNING: Failed to parse run {run+1} for {persona['name']}, retrying...", file=sys.stderr)
                # Retry once
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=2000,
                    temperature=0.5,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = response.content[0].text
                scores = parse_eval_response(text, persona)

            if scores is not None:
                results.append(scores)
                dim_strs = [f"{k}={v}" for k, v in scores.items()]
                print(f"  Run {run+1}: {', '.join(dim_strs)}", file=sys.stderr)
            else:
                print(f"  WARNING: Could not parse run {run+1} for {persona['name']}, skipping", file=sys.stderr)

        except Exception as e:
            print(f"  ERROR in run {run+1} for {persona['name']}: {e}", file=sys.stderr)

    return results


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def compute_median_scores(all_runs: list[dict[str, int]]) -> dict[str, float]:
    """Compute median score for each dimension across runs."""
    if not all_runs:
        return {}
    dimensions = all_runs[0].keys()
    return {
        dim: statistics.median([run[dim] for run in all_runs])
        for dim in dimensions
    }


def compute_summary(persona_medians: dict[str, dict[str, float]], focus_points: dict[str, int]) -> dict:
    """Compute aggregate metrics."""
    total_focus = sum(focus_points.values()) or 1

    all_scores = []
    per_persona = {}
    weakest_dim = None
    weakest_score = float("inf")

    for persona_name, dim_scores in persona_medians.items():
        weight = focus_points.get(persona_name, 0) / total_focus
        persona_min = min(dim_scores.values()) if dim_scores else 0
        persona_mean = statistics.mean(dim_scores.values()) if dim_scores else 0
        per_persona[persona_name] = {
            "min": persona_min,
            "mean": persona_mean,
            "weight": weight,
            "weighted_min": persona_min * weight * (total_focus / max(focus_points.values(), default=1)),
            "dimensions": dim_scores,
        }

        for dim_name, score in dim_scores.items():
            # Weight-adjusted score for finding the weakest dimension
            weighted = score * (weight * len(focus_points))
            all_scores.append(score)
            if weighted < weakest_score:
                weakest_score = weighted
                weakest_dim = f"{persona_name}/{dim_name}"

    global_min = min(all_scores) if all_scores else 0
    global_mean = statistics.mean(all_scores) if all_scores else 0

    return {
        "min_score": global_min,
        "mean_score": round(global_mean, 2),
        "per_persona": per_persona,
        "weakest": weakest_dim,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    base_dir = BASE_DIR

    # Load draft (prefer data/draft.md from the iteration loop, fall back to draft.md)
    draft_path = base_dir / "data" / "draft.md"
    if not draft_path.exists():
        draft_path = base_dir / "draft.md"
    if not draft_path.exists():
        print("ERROR: draft.md not found (checked data/draft.md and draft.md)", file=sys.stderr)
        sys.exit(1)
    draft = draft_path.read_text()

    # Load personas (all .md files in personas/ except _template.md)
    personas_dir = base_dir / "personas"
    if not personas_dir.exists():
        print("ERROR: personas/ directory not found", file=sys.stderr)
        sys.exit(1)

    persona_files = sorted(
        p for p in personas_dir.glob("*.md")
        if not p.name.startswith("_")
    )
    if not persona_files:
        print("ERROR: No persona files found in personas/", file=sys.stderr)
        sys.exit(1)

    personas = [parse_persona_file(p) for p in persona_files]
    print(f"Loaded {len(personas)} personas: {', '.join(p['name'] for p in personas)}", file=sys.stderr)

    # Load focus points from config.toml
    focus_points = load_focus_points()

    # Default: equal weight if not specified
    for p in personas:
        if p["name"] not in focus_points:
            focus_points[p["name"]] = 100 // len(personas)

    total = sum(focus_points.values())
    print(f"Focus points: {focus_points} (total: {total})", file=sys.stderr)

    # Initialize API client
    client = anthropic.Anthropic()

    # Evaluate each persona
    persona_medians = {}
    for persona in personas:
        print(f"\nEvaluating: {persona['name']} ({EVAL_RUNS} runs)...", file=sys.stderr)
        runs = evaluate_persona(client, persona, draft)

        if len(runs) < 2:
            print(f"WARNING: Only {len(runs)} valid runs for {persona['name']}", file=sys.stderr)

        if runs:
            medians = compute_median_scores(runs)
            persona_medians[persona["name"]] = medians
            print(f"  Medians: {', '.join(f'{k}={v}' for k, v in medians.items())}", file=sys.stderr)
        else:
            print(f"ERROR: No valid runs for {persona['name']}", file=sys.stderr)

    if not persona_medians:
        print("ERROR: No valid evaluations", file=sys.stderr)
        sys.exit(1)

    # Compute summary
    summary = compute_summary(persona_medians, focus_points)

    # Print results in autoresearch-style format (to stdout, parseable)
    print("---")
    print(f"min_score:        {summary['min_score']:.1f}")
    print(f"mean_score:       {summary['mean_score']:.1f}")
    for persona_name, data in summary["per_persona"].items():
        print(f"{persona_name}_min:  {data['min']:.1f}")
    print(f"weakest:          {summary['weakest']}")

    # Print detailed scores as JSON to stderr for debugging
    print(f"\nDetailed scores:", file=sys.stderr)
    for persona_name, data in summary["per_persona"].items():
        print(f"  {persona_name} (focus={focus_points.get(persona_name, 0)}):", file=sys.stderr)
        for dim, score in data["dimensions"].items():
            print(f"    {dim}: {score}", file=sys.stderr)
        print(f"    -> min={data['min']:.1f}, mean={data['mean']:.1f}", file=sys.stderr)


if __name__ == "__main__":
    main()
