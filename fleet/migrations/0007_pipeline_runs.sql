-- Migration 0007: pipeline_run and pipeline_stage tables.
--
-- Persists PipelineRun and PipelineStage (fleet.pipeline.models, Task T1) —
-- one row per workflow execution and one row per stage within that run.
-- The UNIQUE (run_id, step_key) constraint on pipeline_stage backs the
-- idempotency check that prevents a step from being spawned twice within
-- the same run (enforced at the SQLite layer so it holds even under races).

CREATE TABLE pipeline_run (
    id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    idea TEXT NOT NULL,
    scope TEXT NOT NULL,
    root_agent_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','blocked','done')),
    created_at TEXT NOT NULL
);

CREATE TABLE pipeline_stage (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_run(id),
    step_key TEXT NOT NULL,
    role TEXT NOT NULL,
    agent_id TEXT,
    task_id TEXT,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','running','passed','failed')),
    UNIQUE (run_id, step_key)
);
