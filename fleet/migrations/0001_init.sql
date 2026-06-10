-- Fleet initial schema — idempotent (all CREATE TABLE IF NOT EXISTS)

CREATE TABLE IF NOT EXISTS repositories (
    id TEXT PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    default_branch TEXT NOT NULL,
    merge_policy_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    scope TEXT NOT NULL,
    role TEXT NOT NULL,
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('idle','running','waiting','paused_budget','failed','archived')),
    parent_id TEXT REFERENCES agents(id),
    repository_id TEXT REFERENCES repositories(id),
    session_ref TEXT,
    worktree_id TEXT,
    context_pct REAL NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    budget_soft_usd REAL,
    budget_hard_usd REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(scope, name)
);

CREATE TABLE IF NOT EXISTS worktrees (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    repository_id TEXT NOT NULL REFERENCES repositories(id),
    path TEXT NOT NULL,
    branch TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    owned_paths_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK(status IN ('active','merged','removed')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    scope TEXT NOT NULL,
    agent_id TEXT,
    type TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_scope_id ON events(scope, id);

CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    to_agent_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','delivered','failed')),
    created_at TEXT NOT NULL,
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    owner_agent_id TEXT,
    branch TEXT,
    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS validation_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    command TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    summary TEXT NOT NULL,
    skipped TEXT,
    residual_risk TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    requester_agent_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    rationale TEXT NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','approved','denied')),
    decided_by TEXT,
    comment TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS memory (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('architecture_decision','known_bug','failed_attempt','command_recipe','dependency_note','deployment_note')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    source_event_id INTEGER REFERENCES events(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    ts TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    tokens INTEGER NOT NULL
);
