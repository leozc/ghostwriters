"""Lineage store: SQLite for relational data + filesystem for content blobs.

See docs/v1-design.md "Data model" and "Crash recovery" sections.

Threading model: one Store instance per process. `sqlite3.Connection` is shared;
SQLite serializes writes internally (WAL mode allows concurrent readers).
The orchestrator wraps each iteration in `with store.transaction() as cur` to
get atomicity (FR12).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from ..types import (
    ClaimVerdict,
    Decision,
    FactCheckClaim,
    FactCheckReport,
    FactCheckStatus,
    IterationRecord,
    Pairwise,
    PairwisePref,
    ReviewerOutput,
    RubricScores,
    StopReason,
    TaskConfig,
    TaskStatus,
)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContentStore:
    """Filesystem-backed blob storage for article version content.

    Layout: <root>/<article_id>/<version_id>.md
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, article_id: str, content_id: str, text: str) -> str:
        article_dir = self.root / article_id
        article_dir.mkdir(parents=True, exist_ok=True)
        rel_path = f"{article_id}/{content_id}.md"
        (self.root / rel_path).write_text(text, encoding="utf-8")
        return rel_path

    def read(self, content_path: str) -> str:
        return (self.root / content_path).read_text(encoding="utf-8")


class Store:
    """SQLite-backed lineage store. All FR-required tables initialized on first start."""

    def __init__(self, db_path: Path, content_root: Optional[Path] = None):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path),
            isolation_level=None,  # autocommit off; we manage transactions manually
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

        if content_root is None:
            content_root = self._path.parent / "content"
        self.content = ContentStore(content_root)

    def _init_schema(self) -> None:
        schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        self._conn.executescript(schema_sql)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        """Yield a cursor inside an atomic transaction (FR12)."""
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield cur
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    # ---- articles ----------------------------------------------------------

    def create_article(self, *, slug: str, content: str) -> tuple[str, str]:
        """Returns (article_id, initial_version_id)."""
        article_id = _new_id("art")
        version_id = _new_id("ver")
        rel_path = self.content.write(article_id, version_id, content)
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO articles (id, slug, created_at) VALUES (?, ?, ?)",
                (article_id, slug, _now()),
            )
            cur.execute(
                "INSERT INTO article_versions "
                "(id, article_id, parent_version_id, content_path, "
                " editor_prompt_hash, editor_model, edit_summary, created_at) "
                "VALUES (?, ?, NULL, ?, NULL, NULL, NULL, ?)",
                (version_id, article_id, rel_path, _now()),
            )
        return article_id, version_id

    def get_article(self, article_id: str) -> Optional[dict]:
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_version(self, version_id: str) -> Optional[dict]:
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT * FROM article_versions WHERE id = ?", (version_id,)
        ).fetchone()
        return dict(row) if row else None

    def latest_version(self, article_id: str) -> Optional[dict]:
        """Most recent kept version. With a linear DAG this is the leaf."""
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT * FROM article_versions WHERE article_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (article_id,),
        ).fetchone()
        return dict(row) if row else None

    # ---- tasks -------------------------------------------------------------

    def create_task(self, config: TaskConfig) -> str:
        task_id = _new_id("task")
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO tasks "
                "(id, article_id, target_aggregate, reviewer_floor, loop_limit, "
                " must_do_text, must_not_do_text, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    config.article_id,
                    config.target_aggregate,
                    config.reviewer_floor,
                    config.loop_limit,
                    config.must_do_text,
                    config.must_not_do_text,
                    TaskStatus.PENDING.value,
                    _now(),
                ),
            )
            for r in config.reviewers:
                cur.execute(
                    "INSERT INTO task_reviewers (task_id, reviewer_id, weight) "
                    "VALUES (?, ?, ?)",
                    (task_id, r.reviewer_id, r.weight),
                )
        return task_id

    def get_task(self, task_id: str) -> Optional[dict]:
        cur = self._conn.cursor()
        row = cur.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        task = dict(row)
        reviewers = cur.execute(
            "SELECT reviewer_id, weight FROM task_reviewers WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        task["reviewers"] = [dict(r) for r in reviewers]
        return task

    def mark_task_running(self, task_id: str) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (TaskStatus.RUNNING.value, task_id),
            )

    def mark_task_terminal(
        self,
        *,
        task_id: str,
        status: TaskStatus,
        stop_reason: StopReason,
        final_version_id: Optional[str],
    ) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE tasks SET status = ?, stop_reason = ?, "
                " final_version_id = ?, completed_at = ? WHERE id = ?",
                (status.value, stop_reason.value, final_version_id, _now(), task_id),
            )

    def resume_state(self) -> list[tuple[str, int]]:
        """Returns (task_id, last_iter_index) for tasks left in 'running' state.

        FR12: orchestrator restart-safe. After a crash, every task in 'running'
        either resumes from MAX(iter_index)+1 or is cleanly aborted.
        """
        cur = self._conn.cursor()
        rows = cur.execute(
            "SELECT t.id, COALESCE(MAX(ir.iter_index), -1) AS last_idx "
            "FROM tasks t LEFT JOIN iteration_records ir ON ir.task_id = t.id "
            "WHERE t.status = ? GROUP BY t.id",
            (TaskStatus.RUNNING.value,),
        ).fetchall()
        return [(r["id"], r["last_idx"]) for r in rows]

    # ---- idempotency (FR9) ------------------------------------------------

    def lookup_idempotency(self, task_id: str, key: str) -> Optional[str]:
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT iteration_id FROM idempotency_keys WHERE task_id = ? AND key = ?",
            (task_id, key),
        ).fetchone()
        return row["iteration_id"] if row else None

    # ---- human notes ------------------------------------------------------

    def add_human_note(self, task_id: str, text: str) -> str:
        note_id = _new_id("note")
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO human_notes (id, task_id, note_text, consumed_at, created_at) "
                "VALUES (?, ?, ?, NULL, ?)",
                (note_id, task_id, text, _now()),
            )
        return note_id

    def pending_notes(self, task_id: str) -> list[dict]:
        cur = self._conn.cursor()
        rows = cur.execute(
            "SELECT id, note_text FROM human_notes "
            "WHERE task_id = ? AND consumed_at IS NULL "
            "ORDER BY created_at ASC",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- iteration write (the heart of FR4 + FR7 + FR12) ------------------

    def record_iteration(
        self,
        *,
        task_id: str,
        article_id: str,
        iter_index: int,
        parent_version_id: str,
        decision: Decision,
        decision_reason: str,
        candidate_text: str,
        edit_summary: str,
        editor_prompt_hash: str,
        editor_model: str,
        aggregate_score: float,
        pref_delta: float,
        pref_ci_low: float,
        pref_ci_high: float,
        reviewer_outputs: list[ReviewerOutput],
        consume_note_ids: Optional[list[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> tuple[str, str]:
        """Atomically write an iteration's full state.

        Inserts (in one transaction):
          - article_versions OR rejected_candidates (for the candidate)
          - iteration_records
          - reviewer_scores (one row per reviewer)
          - idempotency_keys (if provided)
          - UPDATE human_notes.consumed_at for any consumed notes

        Returns (iteration_id, candidate_id).
        """
        consume_note_ids = consume_note_ids or []
        iteration_id = _new_id("iter")

        # Write content blob outside the DB transaction. If the DB tx fails,
        # we leak a blob file but never have a DB row pointing at missing content.
        if decision is Decision.KEPT:
            candidate_id = _new_id("ver")
        else:
            candidate_id = _new_id("rej")
        rel_path = self.content.write(article_id, candidate_id, candidate_text)

        with self.transaction() as cur:
            if decision is Decision.KEPT:
                cur.execute(
                    "INSERT INTO article_versions "
                    "(id, article_id, parent_version_id, content_path, "
                    " editor_prompt_hash, editor_model, edit_summary, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        candidate_id,
                        article_id,
                        parent_version_id,
                        rel_path,
                        editor_prompt_hash,
                        editor_model,
                        edit_summary,
                        _now(),
                    ),
                )
            else:
                cur.execute(
                    "INSERT INTO rejected_candidates "
                    "(id, task_id, parent_version_id, content_path, rejection_reason, "
                    " edit_summary, editor_prompt_hash, editor_model, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        candidate_id,
                        task_id,
                        parent_version_id,
                        rel_path,
                        decision_reason,
                        edit_summary,
                        editor_prompt_hash,
                        editor_model,
                        _now(),
                    ),
                )

            cur.execute(
                "INSERT INTO iteration_records "
                "(id, task_id, iter_index, parent_version_id, candidate_id, decision, "
                " decision_reason, aggregate_score, pref_delta, pref_ci_low, pref_ci_high, "
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    iteration_id,
                    task_id,
                    iter_index,
                    parent_version_id,
                    candidate_id,
                    decision.value,
                    decision_reason,
                    aggregate_score,
                    pref_delta,
                    pref_ci_low,
                    pref_ci_high,
                    _now(),
                ),
            )

            for ro in reviewer_outputs:
                cur.execute(
                    "INSERT INTO reviewer_scores "
                    "(iteration_id, reviewer_id, rubric_scores_json, rubric_aggregate, "
                    " pairwise_pref, pairwise_rationale, rubric_prompt_hash, "
                    " pairwise_prompt_hash, reviewer_model) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        iteration_id,
                        ro.reviewer_id,
                        json.dumps(ro.rubric.scores),
                        ro.rubric.aggregate,
                        ro.pairwise.pref.value,
                        ro.pairwise.rationale,
                        ro.rubric.prompt_hash,
                        ro.pairwise.prompt_hash,
                        ro.rubric.model,
                    ),
                )

            if idempotency_key is not None:
                cur.execute(
                    "INSERT INTO idempotency_keys (task_id, key, iteration_id) "
                    "VALUES (?, ?, ?)",
                    (task_id, idempotency_key, iteration_id),
                )

            for note_id in consume_note_ids:
                cur.execute(
                    "UPDATE human_notes SET consumed_at = ? WHERE id = ? AND task_id = ?",
                    (_now(), note_id, task_id),
                )

        return iteration_id, candidate_id

    # ---- lineage read (FR13) ----------------------------------------------

    def get_lineage(self, task_id: str) -> list[IterationRecord]:
        """Returns all iterations for a task in iter_index order.

        Each record carries its full reviewer_outputs payload reconstructed
        from reviewer_scores rows.
        """
        cur = self._conn.cursor()
        iters = cur.execute(
            "SELECT * FROM iteration_records WHERE task_id = ? "
            "ORDER BY iter_index ASC",
            (task_id,),
        ).fetchall()
        result: list[IterationRecord] = []
        for it in iters:
            scores = cur.execute(
                "SELECT * FROM reviewer_scores WHERE iteration_id = ?",
                (it["id"],),
            ).fetchall()
            reviewer_outputs = [
                ReviewerOutput(
                    reviewer_id=s["reviewer_id"],
                    weight=0.0,  # weight not stored on iteration; lookup task_reviewers if needed
                    rubric=RubricScores(
                        scores=json.loads(s["rubric_scores_json"]),
                        aggregate=s["rubric_aggregate"],
                        prompt_hash=s["rubric_prompt_hash"],
                        model=s["reviewer_model"],
                    ),
                    pairwise=Pairwise(
                        pref=PairwisePref(s["pairwise_pref"]),
                        rationale=s["pairwise_rationale"] or "",
                        prompt_hash=s["pairwise_prompt_hash"],
                        model=s["reviewer_model"],
                    ),
                )
                for s in scores
            ]
            result.append(
                IterationRecord(
                    id=it["id"],
                    task_id=it["task_id"],
                    iter_index=it["iter_index"],
                    parent_version_id=it["parent_version_id"],
                    candidate_id=it["candidate_id"],
                    decision=Decision(it["decision"]),
                    decision_reason=it["decision_reason"],
                    aggregate_score=it["aggregate_score"],
                    pref_delta=it["pref_delta"],
                    pref_ci_low=it["pref_ci_low"],
                    pref_ci_high=it["pref_ci_high"],
                    reviewer_outputs=reviewer_outputs,
                    created_at=datetime.fromisoformat(it["created_at"]),
                )
            )
        return result

    # ---- fact-check (FR6) -------------------------------------------------

    def save_fact_check(
        self, *, task_id: str, final_version_id: str, report: FactCheckReport
    ) -> str:
        report_id = _new_id("fcr")
        payload = {
            "claims": [
                {
                    "text": c.text,
                    "verdict": c.verdict.value,
                    "sources": c.sources,
                    "rationale": c.rationale,
                }
                for c in report.claims
            ],
        }
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO fact_check_reports "
                "(id, task_id, final_version_id, status, report_json, "
                " fact_checker_model, fact_checker_prompt_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    task_id,
                    final_version_id,
                    report.status.value,
                    json.dumps(payload),
                    report.model,
                    report.prompt_hash,
                    _now(),
                ),
            )
        return report_id

    def get_fact_check(self, task_id: str) -> Optional[FactCheckReport]:
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT * FROM fact_check_reports WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            return None
        payload = json.loads(row["report_json"])
        return FactCheckReport(
            status=FactCheckStatus(row["status"]),
            claims=[
                FactCheckClaim(
                    text=c["text"],
                    verdict=ClaimVerdict(c["verdict"]),
                    sources=c.get("sources", []),
                    rationale=c.get("rationale", ""),
                )
                for c in payload["claims"]
            ],
            prompt_hash=row["fact_checker_prompt_hash"],
            model=row["fact_checker_model"],
        )

    # ---- rejected candidates lookup (for lineage view) --------------------

    def get_rejected(self, rejected_id: str) -> Optional[dict]:
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT * FROM rejected_candidates WHERE id = ?", (rejected_id,)
        ).fetchone()
        return dict(row) if row else None
