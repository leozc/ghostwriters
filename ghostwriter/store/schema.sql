-- Ghostwriter v1 redesign — lineage store schema.
-- See docs/v1-design.md "Data model" section.
--
-- Conventions:
--   - All TEXT primary keys are ULIDs / UUIDs.
--   - All TIMESTAMP columns are ISO-8601 UTC strings.
--   - Schema is created once on first start; no migrations in v1.
--
-- FR mapping:
--   FR1  : articles, article_versions, rejected_candidates
--   FR2  : tasks, task_reviewers
--   FR3  : reviewer_scores
--   FR4  : iteration_records.pref_ci_low > 0 -> decision = 'kept'
--   FR5  : orchestrator stop check on aggregate_score and per-reviewer scores
--   FR6  : fact_check_reports (one per task, written at end of loop)
--   FR7  : iteration_records + reviewer_scores
--   FR8  : tasks.must_do_text + tasks.must_not_do_text
--   FR9  : idempotency_keys
--   FR12 : single transaction per iteration

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS articles (
    id          TEXT PRIMARY KEY,
    slug        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS article_versions (
    id                  TEXT PRIMARY KEY,
    article_id          TEXT NOT NULL REFERENCES articles(id),
    parent_version_id   TEXT REFERENCES article_versions(id),
    content_path        TEXT NOT NULL,
    editor_prompt_hash  TEXT,
    editor_model        TEXT,
    edit_summary        TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_versions_article ON article_versions(article_id);
CREATE INDEX IF NOT EXISTS idx_versions_parent  ON article_versions(parent_version_id);

CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    article_id        TEXT NOT NULL REFERENCES articles(id),
    target_aggregate  REAL NOT NULL,
    reviewer_floor    REAL NOT NULL,
    loop_limit        INTEGER NOT NULL,
    must_do_text      TEXT NOT NULL DEFAULT '',
    must_not_do_text  TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL,           -- 'pending' | 'running' | 'done' | 'aborted'
    final_version_id  TEXT REFERENCES article_versions(id),
    stop_reason       TEXT,                    -- 'target_reached' | 'loop_limit' | 'aborted'
    created_at        TEXT NOT NULL,
    completed_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_article ON tasks(article_id);

CREATE TABLE IF NOT EXISTS task_reviewers (
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    reviewer_id TEXT NOT NULL,
    weight      REAL NOT NULL,
    PRIMARY KEY (task_id, reviewer_id)
);

CREATE TABLE IF NOT EXISTS rejected_candidates (
    id                  TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL REFERENCES tasks(id),
    parent_version_id   TEXT NOT NULL REFERENCES article_versions(id),
    content_path        TEXT NOT NULL,
    rejection_reason    TEXT NOT NULL,
    edit_summary        TEXT,
    editor_prompt_hash  TEXT,
    editor_model        TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rejected_task ON rejected_candidates(task_id);

CREATE TABLE IF NOT EXISTS iteration_records (
    id                TEXT PRIMARY KEY,
    task_id           TEXT NOT NULL REFERENCES tasks(id),
    iter_index        INTEGER NOT NULL,
    parent_version_id TEXT NOT NULL,
    candidate_id      TEXT NOT NULL,           -- versions.id if kept, rejected.id otherwise
    decision          TEXT NOT NULL,           -- 'kept' | 'rejected'
    decision_reason   TEXT NOT NULL,
    aggregate_score   REAL NOT NULL,
    pref_delta        REAL NOT NULL,
    pref_ci_low       REAL NOT NULL,
    pref_ci_high      REAL NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (task_id, iter_index)
);

CREATE INDEX IF NOT EXISTS idx_iter_task ON iteration_records(task_id);

CREATE TABLE IF NOT EXISTS reviewer_scores (
    iteration_id          TEXT NOT NULL REFERENCES iteration_records(id),
    reviewer_id           TEXT NOT NULL,
    weight                REAL NOT NULL,       -- snapshot of task_reviewers.weight at iter time
    rubric_scores_json    TEXT NOT NULL,
    rubric_aggregate      REAL NOT NULL,
    pairwise_pref         TEXT NOT NULL,       -- 'candidate' | 'incumbent' | 'tie'
    pairwise_rationale    TEXT,
    rubric_prompt_hash    TEXT NOT NULL,
    pairwise_prompt_hash  TEXT NOT NULL,
    rubric_model          TEXT NOT NULL,       -- model used for the rubric call
    pairwise_model        TEXT NOT NULL,       -- model used for the pairwise call (may differ)
    PRIMARY KEY (iteration_id, reviewer_id)
);

CREATE TABLE IF NOT EXISTS fact_check_reports (
    id                        TEXT PRIMARY KEY,
    task_id                   TEXT NOT NULL UNIQUE REFERENCES tasks(id),
    final_version_id          TEXT NOT NULL REFERENCES article_versions(id),
    status                    TEXT NOT NULL,   -- 'clean' | 'has_findings'
    report_json               TEXT NOT NULL,
    fact_checker_model        TEXT NOT NULL,
    fact_checker_prompt_hash  TEXT NOT NULL,
    created_at                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_notes (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks(id),
    note_text    TEXT NOT NULL,
    consumed_at  TEXT,                          -- NULL until consumed by an iteration
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_task_pending
    ON human_notes(task_id) WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS idempotency_keys (
    task_id      TEXT NOT NULL REFERENCES tasks(id),
    key          TEXT NOT NULL,
    iteration_id TEXT REFERENCES iteration_records(id),
    PRIMARY KEY (task_id, key)
);
