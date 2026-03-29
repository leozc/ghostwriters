"""
ghostwriter scoring utility. Computes medians and summary from raw evaluation scores.
Usage: uv run score.py scores.json
Input: JSON file with persona evaluation runs.
Output: autoresearch-style summary to stdout.
"""

import json
import statistics
import sys
import tomllib
from pathlib import Path

BASE_DIR = Path(__file__).parent


def load_config() -> dict:
    with open(BASE_DIR / "config.toml", "rb") as f:
        return tomllib.load(f)


def compute_median_scores(runs: list[dict[str, int]]) -> dict[str, float]:
    if not runs:
        return {}
    dimensions = runs[0].keys()
    return {dim: statistics.median([r[dim] for r in runs]) for dim in dimensions}


def main():
    if len(sys.argv) < 2:
        print("Usage: uv run score.py scores.json", file=sys.stderr)
        sys.exit(1)

    scores_path = Path(sys.argv[1])
    raw = json.loads(scores_path.read_text())

    config = load_config()
    focus_points = {k: int(v) for k, v in config.get("focus", {}).items()}
    total_focus = sum(focus_points.values()) or 1

    all_scores = []
    per_persona = {}
    weakest_dim = None
    weakest_score = float("inf")

    for persona_name, data in raw.items():
        runs = data["runs"]
        dealbreaker = data.get("dealbreaker", False)

        if dealbreaker:
            # Zero all scores
            dims = runs[0].keys() if runs else []
            medians = {d: 0.0 for d in dims}
        else:
            medians = compute_median_scores(runs)

        weight = focus_points.get(persona_name, 0) / total_focus
        persona_min = min(medians.values()) if medians else 0
        persona_mean = statistics.mean(medians.values()) if medians else 0

        per_persona[persona_name] = {
            "min": persona_min,
            "mean": round(persona_mean, 1),
            "weight": weight,
            "dimensions": medians,
        }

        for dim_name, score in medians.items():
            weighted = score * (weight * len(raw))
            all_scores.append(score)
            if weighted < weakest_score:
                weakest_score = weighted
                weakest_dim = f"{persona_name}/{dim_name}"

    global_min = min(all_scores) if all_scores else 0
    global_mean = round(statistics.mean(all_scores), 1) if all_scores else 0

    # Print autoresearch-style summary (stdout, parseable)
    print("---")
    print(f"min_score:        {global_min:.1f}")
    print(f"mean_score:       {global_mean:.1f}")
    for pname, pdata in per_persona.items():
        print(f"{pname}_min:  {pdata['min']:.1f}")
    print(f"weakest:          {weakest_dim}")

    # Detailed breakdown to stderr
    print(f"\nDetailed scores:", file=sys.stderr)
    for pname, pdata in per_persona.items():
        fp = focus_points.get(pname, 0)
        print(f"  {pname} (focus={fp}):", file=sys.stderr)
        for dim, score in pdata["dimensions"].items():
            print(f"    {dim}: {score}", file=sys.stderr)
        print(f"    -> min={pdata['min']:.1f}, mean={pdata['mean']:.1f}", file=sys.stderr)


if __name__ == "__main__":
    main()
