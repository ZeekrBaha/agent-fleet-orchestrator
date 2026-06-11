-- Migration 0006: append-only triggers on the events audit log.
--
-- Prevents UPDATE and DELETE on the events table at the SQLite layer,
-- making the forensic record tamper-resistant even if application logic
-- is compromised or buggy.  INSERTs are unaffected.

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only: UPDATE not permitted');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only: DELETE not permitted');
END;
