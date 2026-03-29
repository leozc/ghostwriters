"""
ghostwriter data management. All autoresearch artifacts live in data/.
The repo ships clean without data/ (it's gitignored). Run init to bootstrap.

Usage:
    # Initialize data/ with a source draft (run once per ghostwriter session)
    uv run data.py init path/to/source_draft.md

    # Start a new iteration (creates folder, snapshots data/draft.md, generates diff)
    uv run data.py new "description of what this iteration tries"

    # Save a persona's scores (call once per persona, parallel safe)
    uv run data.py save-scores <persona_name> '<json_scores>'

    # Save reader comments (call once per source+persona, parallel safe)
    uv run data.py save-comment <source> <persona> '<comment_text>'
    # source: claude | codex     persona: hn | x

    # Finalize iteration (compute medians, write summary, update manifest)
    uv run data.py finalize <keep|discard>

    # Show iteration history
    uv run data.py status

File layout:
    data/
    ├── draft.md              <- working draft (writer edits this)
    ├── manifest.json         <- index of all iterations
    ├── iter_00/
    │   ├── draft.md          <- snapshot at this iteration
    │   ├── diff.patch        <- what changed from previous
    │   ├── summary.json      <- scores + status
    │   ├── scores/
    │   │   ├── investor.json
    │   │   ├── compliance_officer.json
    │   │   └── ...
    │   └── comments/
    │       ├── claude_hn.md
    │       ├── claude_x.md
    │       ├── codex_hn.md
    │       └── codex_x.md
    └── iter_01/
        └── ...
"""

import json
import os
import shutil
import subprocess
import sys
import statistics
import tomllib
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
MANIFEST_PATH = DATA_DIR / "manifest.json"
DRAFT_PATH = DATA_DIR / "draft.md"  # working draft lives in data/


def load_config():
    with open(BASE_DIR / "config.toml", "rb") as f:
        return tomllib.load(f)


def load_manifest():
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {"iterations": []}


def save_manifest(manifest):
    DATA_DIR.mkdir(exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")


def current_iter_id(manifest):
    if not manifest["iterations"]:
        return 0
    return manifest["iterations"][-1]["id"]


def current_iter_dir(manifest):
    iter_id = current_iter_id(manifest)
    return DATA_DIR / f"iter_{iter_id:02d}"


def git_short_hash():
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, cwd=BASE_DIR
    )
    return result.stdout.strip()


def git_diff_from_parent():
    result = subprocess.run(
        ["git", "diff", "HEAD~1", "--", "data/draft.md"],
        capture_output=True, text=True, cwd=BASE_DIR
    )
    return result.stdout


# -------------------------------------------------------------------------
# Commands
# -------------------------------------------------------------------------

def cmd_init(source_draft):
    """Initialize data/ with a source draft. Run once at the start of a new ghostwriter run."""
    DATA_DIR.mkdir(exist_ok=True)
    source = Path(source_draft)
    if not source.exists():
        print(f"ERROR: source draft not found: {source}")
        sys.exit(1)
    shutil.copy2(source, DRAFT_PATH)
    save_manifest({"iterations": []})
    print(f"Initialized data/ with draft from {source}")
    print(f"  Working draft: {DRAFT_PATH}")
    print(f"  Manifest: {MANIFEST_PATH}")
    print(f"  Writer agent should edit: data/draft.md")


def cmd_new(description):
    """Create a new iteration folder, snapshot draft, generate diff."""
    manifest = load_manifest()
    iterations = manifest["iterations"]

    if iterations:
        next_id = iterations[-1]["id"] + 1
    else:
        next_id = 0

    iter_dir = DATA_DIR / f"iter_{next_id:02d}"
    iter_dir.mkdir(parents=True, exist_ok=True)
    (iter_dir / "scores").mkdir(exist_ok=True)
    (iter_dir / "comments").mkdir(exist_ok=True)

    # Snapshot draft from working copy in data/
    if DRAFT_PATH.exists():
        shutil.copy2(DRAFT_PATH, iter_dir / "draft.md")

    # Generate diff (skip for baseline)
    if next_id > 0:
        diff = git_diff_from_parent()
        (iter_dir / "diff.patch").write_text(diff)
    else:
        (iter_dir / "diff.patch").write_text("")

    # Write partial summary (finalized later)
    summary = {
        "id": next_id,
        "description": description,
        "commit": git_short_hash(),
        "status": "pending",
        "min_score": None,
        "mean_score": None,
        "per_persona": {},
    }
    (iter_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    # Append to manifest as pending
    iterations.append({
        "id": next_id,
        "status": "pending",
        "description": description,
        "commit": summary["commit"],
    })
    save_manifest(manifest)

    print(f"iter {next_id:02d} created: {iter_dir}")


def cmd_save_scores(persona_name, scores_json):
    """Save one persona's scores to its own file. No concurrency issues."""
    manifest = load_manifest()
    iter_dir = current_iter_dir(manifest)
    scores_dir = iter_dir / "scores"
    scores_dir.mkdir(exist_ok=True)

    scores = json.loads(scores_json)
    out_path = scores_dir / f"{persona_name}.json"
    out_path.write_text(json.dumps(scores, indent=2) + "\n")
    print(f"saved: {out_path.relative_to(BASE_DIR)}")


def cmd_save_comment(source, persona, comment_text):
    """Save one reader comment to its own file. No concurrency issues.
    source: claude | codex
    persona: hn | x
    """
    manifest = load_manifest()
    iter_dir = current_iter_dir(manifest)
    comments_dir = iter_dir / "comments"
    comments_dir.mkdir(exist_ok=True)

    out_path = comments_dir / f"{source}_{persona}.md"
    out_path.write_text(comment_text + "\n")
    print(f"saved: {out_path.relative_to(BASE_DIR)}")


def cmd_finalize(status):
    """Compute scores from per-persona files, write summary, update manifest."""
    manifest = load_manifest()
    iter_dir = current_iter_dir(manifest)
    scores_dir = iter_dir / "scores"
    config = load_config()
    focus_points = {k: int(v) for k, v in config.get("focus", {}).items()}

    # Read all persona score files
    persona_medians = {}
    for score_file in sorted(scores_dir.glob("*.json")):
        persona_name = score_file.stem
        data = json.loads(score_file.read_text())
        runs = data.get("runs", [])
        dealbreaker = data.get("dealbreaker", False)

        if dealbreaker or not runs:
            dims = list(runs[0].keys()) if runs else []
            medians = {d: 0.0 for d in dims}
        else:
            dimensions = list(runs[0].keys())
            medians = {
                dim: statistics.median([r[dim] for r in runs])
                for dim in dimensions
            }
        persona_medians[persona_name] = medians

    # Compute aggregate
    all_scores = []
    per_persona = {}
    total_focus = sum(focus_points.values()) or 1
    weakest_dim = None
    weakest_score = float("inf")

    for pname, dim_scores in persona_medians.items():
        weight = focus_points.get(pname, 0) / total_focus
        pmin = min(dim_scores.values()) if dim_scores else 0
        pmean = round(statistics.mean(dim_scores.values()), 1) if dim_scores else 0
        per_persona[pname] = {
            "min": pmin,
            "mean": pmean,
            "dimensions": dim_scores,
        }
        for dname, score in dim_scores.items():
            all_scores.append(score)
            weighted = score * (weight * len(persona_medians))
            if weighted < weakest_score:
                weakest_score = weighted
                weakest_dim = f"{pname}/{dname}"

    global_min = min(all_scores) if all_scores else 0
    global_mean = round(statistics.mean(all_scores), 1) if all_scores else 0

    # Write summary
    summary = json.loads((iter_dir / "summary.json").read_text())
    summary["status"] = status
    summary["min_score"] = global_min
    summary["mean_score"] = global_mean
    summary["per_persona"] = per_persona
    summary["weakest"] = weakest_dim
    (iter_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    # Update manifest
    iter_id = summary["id"]
    for entry in manifest["iterations"]:
        if entry["id"] == iter_id:
            entry["status"] = status
            entry["min_score"] = global_min
            entry["mean_score"] = global_mean
            break
    save_manifest(manifest)

    # Print autoresearch-style output
    print("---")
    print(f"min_score:        {global_min:.1f}")
    print(f"mean_score:       {global_mean:.1f}")
    for pname, pdata in per_persona.items():
        print(f"{pname}_min:  {pdata['min']:.1f}")
    print(f"weakest:          {weakest_dim}")
    print(f"status:           {status}")


def cmd_status():
    """Show iteration history."""
    manifest = load_manifest()
    iterations = manifest["iterations"]
    if not iterations:
        print("No iterations yet. Run: uv run data.py new 'baseline'")
        return

    print(f"{'ID':>3}  {'Status':<8}  {'Min':>5}  {'Mean':>5}  Description")
    print("-" * 72)
    for it in iterations:
        min_s = f"{it['min_score']:>5.1f}" if it.get("min_score") is not None else "    ?"
        mean_s = f"{it['mean_score']:>5.1f}" if it.get("mean_score") is not None else "    ?"
        print(f"{it['id']:>3}  {it['status']:<8}  {min_s}  {mean_s}  {it['description']}")


# -------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "init":
        if len(sys.argv) < 3:
            print("Usage: uv run data.py init <path_to_source_draft>")
            sys.exit(1)
        cmd_init(sys.argv[2])

    elif cmd == "new":
        if len(sys.argv) < 3:
            print("Usage: uv run data.py new 'description'")
            sys.exit(1)
        cmd_new(sys.argv[2])

    elif cmd == "save-scores":
        if len(sys.argv) < 4:
            print("Usage: uv run data.py save-scores <persona> '<json>'")
            sys.exit(1)
        cmd_save_scores(sys.argv[2], sys.argv[3])

    elif cmd == "save-comment":
        if len(sys.argv) < 5:
            print("Usage: uv run data.py save-comment <source> <persona> '<text>'")
            sys.exit(1)
        cmd_save_comment(sys.argv[2], sys.argv[3], sys.argv[4])

    elif cmd == "finalize":
        if len(sys.argv) < 3:
            print("Usage: uv run data.py finalize <keep|discard>")
            sys.exit(1)
        cmd_finalize(sys.argv[2])

    elif cmd == "status":
        cmd_status()

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
