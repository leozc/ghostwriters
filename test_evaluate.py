"""Unit tests for evaluate.py."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import evaluate


class TestEvaluatePy(TestCase):

    def test_normalize_provider_supports_codex_alias(self):
        self.assertEqual(evaluate.normalize_provider("openai"), "openai")
        self.assertEqual(evaluate.normalize_provider("codex"), "openai")
        self.assertEqual(evaluate.normalize_provider("anthropic"), "anthropic")

    def test_normalize_provider_rejects_unknown_provider(self):
        with self.assertRaises(ValueError):
            evaluate.normalize_provider("unknown")

    def test_load_personas_skips_reader_files_without_rubrics(self):
        personas = evaluate.load_personas(Path(__file__).parent / "personas")
        names = [persona["name"] for persona in personas]
        # Should include scoring personas (have rubric dimensions)
        self.assertIn("sharp_peer", names)
        self.assertIn("xhs_dev_diaspora", names)
        # Should not include reader personas (no rubric dimensions)
        self.assertNotIn("xhs_commenter", names)
        self.assertNotIn("x_replier", names)

    def test_load_personas_with_evaluator_filter(self):
        personas = evaluate.load_personas(
            Path(__file__).parent / "personas",
            evaluator_filter=["sharp_peer", "xhs_dev_diaspora"],
        )
        names = [p["name"] for p in personas]
        self.assertEqual(sorted(names), ["sharp_peer", "xhs_dev_diaspora"])

    def test_load_personas_errors_on_non_reader_without_rubric(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            personas_dir = Path(tmpdir)
            # A file explicitly listed as evaluator but with no rubric dimensions
            (personas_dir / "broken_eval.md").write_text(
                "# Persona: Broken Eval\n\n## Identity\nYou are...\n\n## Rubric\n"
            )

            with self.assertRaises(ValueError):
                evaluate.load_personas(personas_dir, evaluator_filter=["broken_eval"])

    @patch("evaluate.subprocess.run")
    def test_run_codex_prompt_uses_output_file(self, mock_run):
        def fake_run(cmd, input, text, capture_output, cwd):
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.write_text("hello from codex")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        mock_run.side_effect = fake_run

        text = evaluate.run_codex_prompt("prompt body", "gpt-5.4")

        self.assertEqual(text, "hello from codex")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("--ephemeral", cmd)
        self.assertIn("-m", cmd)
        self.assertIn("gpt-5.4", cmd)

    @patch.dict(os.environ, {}, clear=True)
    @patch("evaluate.check_codex_login_status")
    def test_build_prompt_runner_falls_back_to_openai_when_no_anthropic_key(self, mock_status):
        prompt_runner, provider, model = evaluate.build_prompt_runner("anthropic")

        self.assertEqual(provider, "openai")
        self.assertEqual(model, evaluate.OPENAI_MODEL)
        self.assertTrue(callable(prompt_runner))
        mock_status.assert_called_once()

    def test_evaluate_persona_retries_failed_parse_once(self):
        persona = {
            "name": "investor",
            "dimensions": [{"name": "Novel insight", "description": "test"}],
            "identity": "You are an investor.",
            "care": "- insight",
            "value": "Would I invest?",
            "dealbreaker": "",
        }

        responses = iter([
            "not parseable",
            "Novel insight: cites a line -> 7\nOVERALL: good\nDEALBREAKER_TRIGGERED: no",
            "Novel insight: cites a line -> 6\nOVERALL: good\nDEALBREAKER_TRIGGERED: no",
            "Novel insight: cites a line -> 8\nOVERALL: good\nDEALBREAKER_TRIGGERED: no",
        ])

        def prompt_runner(prompt, temperature):
            return next(responses)

        runs = evaluate.evaluate_persona(prompt_runner, persona, "draft text", "openai")

        self.assertEqual(runs, [
            {"Novel insight": 7},
            {"Novel insight": 6},
            {"Novel insight": 8},
        ])


if __name__ == "__main__":
    main()
