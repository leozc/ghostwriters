"""Shared pytest fixtures.

Layout:
- tests/unit/        — pure logic, mocked I/O
- tests/integration/ — multiple components, real SQLite under tmp_path
- tests/acceptance/  — end-to-end user scenarios via FastAPI TestClient

The pytest-asyncio mode is configured in pyproject.toml; async tests
work without per-test decorators.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ghostwriter.store.sqlite import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Fresh on-disk Store under tmp_path. Per-test isolation is automatic
    via tmp_path's per-test scope."""
    return Store(db_path=tmp_path / "g.db", content_root=tmp_path / "content")
