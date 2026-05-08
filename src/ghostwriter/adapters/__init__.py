"""LLM-facing adapters: editor (propose) and reviewers (rubric + pairwise).

Each adapter is a plain async function that takes a typed config plus a
Provider and returns a frozen dataclass from ghostwriter.types. The
Provider abstraction lets us swap a deterministic FakeProvider into
tests/orchestrator-fixtures while real Anthropic/OpenAI clients land
later (M5.5).
"""
