"""Unit tests for data.py."""

import json
import os
import shutil
import tempfile
import subprocess
import sys
from pathlib import Path
from unittest import TestCase, main

# Import from data.py
sys.path.insert(0, str(Path(__file__).parent))
import data


class TestDataPy(TestCase):

    def setUp(self):
        """Create a temp directory and override data.py paths."""
        self.tmpdir = tempfile.mkdtemp()
        self.orig_base = data.BASE_DIR
        self.orig_data = data.DATA_DIR
        self.orig_manifest = data.MANIFEST_PATH
        self.orig_draft = data.DRAFT_PATH

        data.BASE_DIR = Path(self.tmpdir)
        data.DATA_DIR = Path(self.tmpdir) / "data"
        data.MANIFEST_PATH = data.DATA_DIR / "manifest.json"
        data.DRAFT_PATH = data.DATA_DIR / "draft.md"

        # Create a minimal config.toml
        config_path = Path(self.tmpdir) / "config.toml"
        config_path.write_text(
            '[eval]\nruns = 3\nmin_improvement = 0.5\nmean_improvement = 0.3\n\n'
            '[stopping]\nmin_score_target = 7\n\n'
            '[focus]\ninvestor = 40\ncompliance_officer = 10\nengineer = 20\n'
        )

        # Init git so git commands don't fail
        subprocess.run(["git", "init"], cwd=self.tmpdir, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=self.tmpdir, capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=self.tmpdir, capture_output=True)

    def tearDown(self):
        """Restore original paths and clean up."""
        data.BASE_DIR = self.orig_base
        data.DATA_DIR = self.orig_data
        data.MANIFEST_PATH = self.orig_manifest
        data.DRAFT_PATH = self.orig_draft
        shutil.rmtree(self.tmpdir)

    def _create_draft(self, content="# Test Draft\n\nHello world."):
        """Helper: create a source draft file."""
        draft_path = Path(self.tmpdir) / "source.md"
        draft_path.write_text(content)
        return draft_path

    def _git_commit(self, msg="test"):
        """Helper: stage and commit all files."""
        subprocess.run(["git", "add", "-A"], cwd=self.tmpdir, capture_output=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", msg, "--allow-empty"],
            cwd=self.tmpdir, capture_output=True
        )

    # ----- init -----

    def test_init_creates_data_dir(self):
        draft = self._create_draft()
        data.cmd_init(str(draft))
        self.assertTrue(data.DATA_DIR.exists())
        self.assertTrue(data.DRAFT_PATH.exists())
        self.assertTrue(data.MANIFEST_PATH.exists())

    def test_init_copies_draft_content(self):
        content = "# My Blog Post\n\nSpecific content here."
        draft = self._create_draft(content)
        data.cmd_init(str(draft))
        self.assertEqual(data.DRAFT_PATH.read_text(), content)

    def test_init_creates_empty_manifest(self):
        draft = self._create_draft()
        data.cmd_init(str(draft))
        manifest = json.loads(data.MANIFEST_PATH.read_text())
        self.assertEqual(manifest, {"iterations": []})

    def test_init_fails_on_missing_source(self):
        with self.assertRaises(SystemExit):
            data.cmd_init("/nonexistent/draft.md")

    # ----- new -----

    def test_new_creates_iteration_folder(self):
        draft = self._create_draft()
        data.cmd_init(str(draft))
        self._git_commit()
        data.cmd_new("baseline")
        iter_dir = data.DATA_DIR / "iter_00"
        self.assertTrue(iter_dir.exists())
        self.assertTrue((iter_dir / "scores").exists())
        self.assertTrue((iter_dir / "comments").exists())

    def test_new_snapshots_draft(self):
        content = "# Snapshot Test"
        draft = self._create_draft(content)
        data.cmd_init(str(draft))
        self._git_commit()
        data.cmd_new("baseline")
        snapshot = (data.DATA_DIR / "iter_00" / "draft.md").read_text()
        self.assertEqual(snapshot, content)

    def test_new_increments_id(self):
        draft = self._create_draft()
        data.cmd_init(str(draft))
        self._git_commit()
        data.cmd_new("first")
        data.cmd_new("second")
        data.cmd_new("third")
        manifest = data.load_manifest()
        ids = [it["id"] for it in manifest["iterations"]]
        self.assertEqual(ids, [0, 1, 2])

    def test_new_writes_pending_summary(self):
        draft = self._create_draft()
        data.cmd_init(str(draft))
        self._git_commit()
        data.cmd_new("test iteration")
        summary = json.loads((data.DATA_DIR / "iter_00" / "summary.json").read_text())
        self.assertEqual(summary["status"], "pending")
        self.assertEqual(summary["description"], "test iteration")
        self.assertIsNone(summary["min_score"])

    # ----- save-scores -----

    def test_save_scores_creates_persona_file(self):
        draft = self._create_draft()
        data.cmd_init(str(draft))
        self._git_commit()
        data.cmd_new("baseline")

        scores = json.dumps({
            "runs": [
                {"Novel insight": 7, "Thesis clarity": 8},
                {"Novel insight": 6, "Thesis clarity": 7},
                {"Novel insight": 7, "Thesis clarity": 8}
            ],
            "dealbreaker": False
        })
        data.cmd_save_scores("investor", scores)

        out_path = data.DATA_DIR / "iter_00" / "scores" / "investor.json"
        self.assertTrue(out_path.exists())
        loaded = json.loads(out_path.read_text())
        self.assertEqual(len(loaded["runs"]), 3)

    def test_save_scores_multiple_personas_no_conflict(self):
        draft = self._create_draft()
        data.cmd_init(str(draft))
        self._git_commit()
        data.cmd_new("baseline")

        for persona in ["investor", "compliance_officer", "engineer"]:
            scores = json.dumps({
                "runs": [{"dim1": 7}, {"dim1": 8}, {"dim1": 7}],
                "dealbreaker": False
            })
            data.cmd_save_scores(persona, scores)

        scores_dir = data.DATA_DIR / "iter_00" / "scores"
        files = sorted(f.name for f in scores_dir.glob("*.json"))
        self.assertEqual(files, ["compliance_officer.json", "engineer.json", "investor.json"])

    # ----- save-comment -----

    def test_save_comment_creates_file(self):
        draft = self._create_draft()
        data.cmd_init(str(draft))
        self._git_commit()
        data.cmd_new("baseline")

        data.cmd_save_comment("claude", "hn", "1. Good post.\n2. Needs work.\n3. Ship it.")
        out_path = data.DATA_DIR / "iter_00" / "comments" / "claude_hn.md"
        self.assertTrue(out_path.exists())
        self.assertIn("Good post", out_path.read_text())

    def test_save_comment_four_sources_no_conflict(self):
        draft = self._create_draft()
        data.cmd_init(str(draft))
        self._git_commit()
        data.cmd_new("baseline")

        for source, persona in [("claude", "hn"), ("claude", "x"), ("codex", "hn"), ("codex", "x")]:
            data.cmd_save_comment(source, persona, f"Comment from {source}_{persona}")

        comments_dir = data.DATA_DIR / "iter_00" / "comments"
        files = sorted(f.name for f in comments_dir.glob("*.md"))
        self.assertEqual(files, ["claude_hn.md", "claude_x.md", "codex_hn.md", "codex_x.md"])

    # ----- finalize -----

    def test_finalize_computes_medians(self):
        draft = self._create_draft()
        data.cmd_init(str(draft))
        self._git_commit()
        data.cmd_new("baseline")

        # Save scores for two personas
        data.cmd_save_scores("investor", json.dumps({
            "runs": [{"A": 6, "B": 8}, {"A": 7, "B": 9}, {"A": 6, "B": 8}],
            "dealbreaker": False
        }))
        data.cmd_save_scores("engineer", json.dumps({
            "runs": [{"C": 5, "D": 7}, {"C": 4, "D": 8}, {"C": 5, "D": 7}],
            "dealbreaker": False
        }))

        data.cmd_finalize("keep")

        summary = json.loads((data.DATA_DIR / "iter_00" / "summary.json").read_text())
        self.assertEqual(summary["status"], "keep")
        self.assertEqual(summary["min_score"], 5)  # min of medians: investor A=6,B=8, engineer C=5,D=7
        self.assertEqual(summary["per_persona"]["investor"]["dimensions"]["A"], 6)
        self.assertEqual(summary["per_persona"]["investor"]["dimensions"]["B"], 8)
        self.assertEqual(summary["per_persona"]["engineer"]["dimensions"]["C"], 5)
        self.assertEqual(summary["per_persona"]["engineer"]["dimensions"]["D"], 7)

    def test_finalize_updates_manifest(self):
        draft = self._create_draft()
        data.cmd_init(str(draft))
        self._git_commit()
        data.cmd_new("baseline")

        data.cmd_save_scores("investor", json.dumps({
            "runs": [{"A": 7}, {"A": 8}, {"A": 7}],
            "dealbreaker": False
        }))
        data.cmd_finalize("keep")

        manifest = data.load_manifest()
        self.assertEqual(manifest["iterations"][0]["status"], "keep")
        self.assertEqual(manifest["iterations"][0]["min_score"], 7)

    def test_finalize_discard_status(self):
        draft = self._create_draft()
        data.cmd_init(str(draft))
        self._git_commit()
        data.cmd_new("bad edit")

        data.cmd_save_scores("investor", json.dumps({
            "runs": [{"A": 3}, {"A": 4}, {"A": 3}],
            "dealbreaker": False
        }))
        data.cmd_finalize("discard")

        summary = json.loads((data.DATA_DIR / "iter_00" / "summary.json").read_text())
        self.assertEqual(summary["status"], "discard")

    def test_finalize_dealbreaker_zeros_scores(self):
        draft = self._create_draft()
        data.cmd_init(str(draft))
        self._git_commit()
        data.cmd_new("dealbreaker test")

        data.cmd_save_scores("investor", json.dumps({
            "runs": [{"A": 8, "B": 9}, {"A": 7, "B": 8}, {"A": 8, "B": 9}],
            "dealbreaker": True
        }))
        data.cmd_finalize("discard")

        summary = json.loads((data.DATA_DIR / "iter_00" / "summary.json").read_text())
        self.assertEqual(summary["per_persona"]["investor"]["dimensions"]["A"], 0.0)
        self.assertEqual(summary["per_persona"]["investor"]["dimensions"]["B"], 0.0)

    # ----- status -----

    def test_status_empty(self):
        # Should not crash with no data dir
        data.cmd_status()

    def test_status_with_iterations(self):
        draft = self._create_draft()
        data.cmd_init(str(draft))
        self._git_commit()
        data.cmd_new("first")
        data.cmd_save_scores("investor", json.dumps({
            "runs": [{"A": 7}, {"A": 7}, {"A": 7}], "dealbreaker": False
        }))
        data.cmd_finalize("keep")
        data.cmd_new("second")
        data.cmd_save_scores("investor", json.dumps({
            "runs": [{"A": 5}, {"A": 5}, {"A": 5}], "dealbreaker": False
        }))
        data.cmd_finalize("discard")

        # Just verify it doesn't crash
        data.cmd_status()
        manifest = data.load_manifest()
        self.assertEqual(len(manifest["iterations"]), 2)
        self.assertEqual(manifest["iterations"][0]["status"], "keep")
        self.assertEqual(manifest["iterations"][1]["status"], "discard")

    # ----- end-to-end -----

    def test_full_iteration_lifecycle(self):
        """Simulate a complete iteration: init -> new -> save scores -> save comments -> finalize."""
        draft = self._create_draft("# Test Post\n\nOriginal content.")
        data.cmd_init(str(draft))
        self._git_commit("init")

        # Iteration 0: baseline
        data.cmd_new("baseline")
        data.cmd_save_scores("investor", json.dumps({
            "runs": [{"A": 5, "B": 6}, {"A": 6, "B": 6}, {"A": 5, "B": 7}],
            "dealbreaker": False
        }))
        data.cmd_save_scores("engineer", json.dumps({
            "runs": [{"C": 4, "D": 7}, {"C": 5, "D": 6}, {"C": 4, "D": 7}],
            "dealbreaker": False
        }))
        data.cmd_save_comment("claude", "hn", "1. Good opening.\n2. Needs data.\n3. Ship it.")
        data.cmd_save_comment("codex", "x", "1. Nice frame.\n2. Where's the demo?\n3. Based.")
        data.cmd_finalize("keep")

        # Verify structure
        iter_dir = data.DATA_DIR / "iter_00"
        self.assertTrue((iter_dir / "draft.md").exists())
        self.assertTrue((iter_dir / "summary.json").exists())
        self.assertTrue((iter_dir / "scores" / "investor.json").exists())
        self.assertTrue((iter_dir / "scores" / "engineer.json").exists())
        self.assertTrue((iter_dir / "comments" / "claude_hn.md").exists())
        self.assertTrue((iter_dir / "comments" / "codex_x.md").exists())

        summary = json.loads((iter_dir / "summary.json").read_text())
        self.assertEqual(summary["min_score"], 4)  # engineer C median = 4
        self.assertEqual(summary["status"], "keep")

        # Iteration 1: edit
        data.DRAFT_PATH.write_text("# Test Post\n\nImproved content with data.")
        self._git_commit("edit")
        data.cmd_new("added data to section 1")
        data.cmd_save_scores("investor", json.dumps({
            "runs": [{"A": 7, "B": 8}, {"A": 7, "B": 7}, {"A": 7, "B": 8}],
            "dealbreaker": False
        }))
        data.cmd_save_scores("engineer", json.dumps({
            "runs": [{"C": 6, "D": 8}, {"C": 7, "D": 7}, {"C": 6, "D": 8}],
            "dealbreaker": False
        }))
        data.cmd_finalize("keep")

        # Verify iteration 1
        iter1_summary = json.loads((data.DATA_DIR / "iter_01" / "summary.json").read_text())
        self.assertEqual(iter1_summary["min_score"], 6)  # engineer C median = 6
        self.assertGreater(iter1_summary["min_score"], summary["min_score"])

        # Verify manifest has both
        manifest = data.load_manifest()
        self.assertEqual(len(manifest["iterations"]), 2)


if __name__ == "__main__":
    main()
