-- B2: Schema unification — fix task_id type, add indexes, normalise timestamps.

-- 1. Fix validation_evidence.task_id INTEGER → TEXT.
--    SQLite cannot ALTER COLUMN, so recreate-copy-drop-rename.
CREATE TABLE IF NOT EXISTS validation_evidence_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT    NOT NULL REFERENCES tasks(id),
    check_name    TEXT    NOT NULL,
    status        TEXT    NOT NULL CHECK(status IN ('pass','fail','skip')),
    output        TEXT    NOT NULL DEFAULT '',
    recorded_by   TEXT,
    recorded_by_role TEXT,
    ts            TEXT    NOT NULL
);

INSERT INTO validation_evidence_new
    (id, task_id, check_name, status, output, recorded_by, recorded_by_role, ts)
SELECT  id, CAST(task_id AS TEXT), check_name, status, output,
        recorded_by,
        -- recorded_by_role was added by 0003; column may not exist on 0001-only DBs.
        -- COALESCE handles both cases transparently.
        COALESCE(recorded_by_role, NULL),
        ts
FROM validation_evidence;

DROP TABLE validation_evidence;
ALTER TABLE validation_evidence_new RENAME TO validation_evidence;

-- 2. Performance indexes.
CREATE INDEX IF NOT EXISTS idx_inbox_to_status
    ON inbox(to_agent_id, status);

CREATE INDEX IF NOT EXISTS idx_events_agent_id
    ON events(agent_id, id);

-- 3. Normalise legacy Z-suffix timestamps to +00:00.
--    Replace trailing 'Z' with '+00:00' only where the suffix is exactly 'Z'.
UPDATE events
    SET ts = SUBSTR(ts, 1, LENGTH(ts) - 1) || '+00:00'
    WHERE ts LIKE '%Z' AND ts NOT LIKE '%+00:00';

UPDATE agent_memories
    SET ts = SUBSTR(ts, 1, LENGTH(ts) - 1) || '+00:00'
    WHERE ts LIKE '%Z' AND ts NOT LIKE '%+00:00';
