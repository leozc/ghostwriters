#!/usr/bin/env python3
"""Show score evolution across iterations for each persona and dimension."""

import json
import sys
from pathlib import Path

DATA_DIR = Path("data")


def load_iterations():
    """Load all iteration summaries in order."""
    iters = []
    for d in sorted(DATA_DIR.glob("iter_*")):
        summary = d / "summary.json"
        if summary.exists():
            with open(summary) as f:
                data = json.load(f)
                data["_dir"] = d.name
                iters.append(data)
    return iters


def print_persona_table(persona: str, iters: list):
    """Print a table showing dimension scores across iterations for one persona."""
    # Collect all dimensions for this persona
    dims = set()
    for it in iters:
        if persona in it.get("per_persona", {}):
            dims.update(it["per_persona"][persona].get("dimensions", {}).keys())

    if not dims:
        return

    dims = sorted(dims)

    # Header
    iter_labels = []
    for it in iters:
        status = it.get("status", "?")
        marker = "x" if status == "discard" else ""
        iter_labels.append(f"i{it['id']:02d}{marker}")

    col_w = 6
    dim_w = max(len(d) for d in dims) + 2
    header = f"{'dimension':<{dim_w}}" + "".join(f"{l:>{col_w}}" for l in iter_labels) + f"{'  trend':>8}"
    print(f"\n{'=' * len(header)}")
    print(f"  {persona}")
    print(f"{'=' * len(header)}")
    print(header)
    print("-" * len(header))

    for dim in dims:
        row = f"{dim:<{dim_w}}"
        scores = []
        for it in iters:
            p = it.get("per_persona", {}).get(persona, {})
            d = p.get("dimensions", {})
            score = d.get(dim)
            if score is not None:
                row += f"{score:>{col_w}}"
                scores.append(score)
            else:
                row += f"{'—':>{col_w}}"

        # Trend
        if len(scores) >= 2:
            delta = scores[-1] - scores[0]
            if delta > 0:
                trend = f"  +{delta} ↑"
            elif delta < 0:
                trend = f"  {delta} ↓"
            else:
                trend = f"   0 →"
        else:
            trend = ""
        row += f"{trend:>8}"
        print(row)

    # Min and mean rows
    print("-" * len(header))
    min_row = f"{'MIN':<{dim_w}}"
    mean_row = f"{'MEAN':<{dim_w}}"
    for it in iters:
        p = it.get("per_persona", {}).get(persona, {})
        mn = p.get("min")
        me = p.get("mean")
        min_row += f"{mn if mn is not None else '—':>{col_w}}"
        mean_row += f"{me if me is not None else '—':>{col_w}}"
    print(min_row)
    print(mean_row)


def print_overall(iters: list):
    """Print overall min/mean across iterations."""
    col_w = 6
    iter_labels = [f"i{it['id']:02d}" for it in iters]
    header = f"{'overall':<20}" + "".join(f"{l:>{col_w}}" for l in iter_labels)
    print(f"\n{'=' * len(header)}")
    print("  OVERALL")
    print(f"{'=' * len(header)}")
    print(header)
    print("-" * len(header))

    min_row = f"{'min_score':<20}"
    mean_row = f"{'mean_score':<20}"
    for it in iters:
        min_row += f"{it.get('min_score', '—'):>{col_w}}"
        mean_row += f"{it.get('mean_score', '—'):>{col_w}}"
    print(min_row)
    print(mean_row)

    # Status row
    status_row = f"{'status':<20}"
    for it in iters:
        s = it.get("status", "?")
        status_row += f"{s[:4]:>{col_w}}"
    print(status_row)


def main():
    iters = load_iterations()
    if not iters:
        print("No iterations found in data/")
        sys.exit(1)

    # Filter to kept iterations only (unless --all flag)
    show_all = "--all" in sys.argv
    if not show_all:
        kept = [it for it in iters if it.get("status") == "keep"]
    else:
        kept = iters

    # Get all personas
    personas = set()
    for it in kept:
        personas.update(it.get("per_persona", {}).keys())

    # Optional: filter to specific persona
    filter_persona = None
    for arg in sys.argv[1:]:
        if arg != "--all" and arg in personas:
            filter_persona = arg

    print_overall(kept)

    for persona in sorted(personas):
        if filter_persona and persona != filter_persona:
            continue
        print_persona_table(persona, kept)

    print(f"\n{len(kept)} iterations shown.", end="")
    if not show_all:
        discarded = len(iters) - len(kept)
        if discarded:
            print(f" ({discarded} discarded hidden, use --all to show)", end="")
    print()


if __name__ == "__main__":
    main()
