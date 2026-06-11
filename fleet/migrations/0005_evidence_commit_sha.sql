-- Migration 0005: add commit_sha to validation_evidence for staleness detection.
--
-- Existing rows get NULL (no SHA known at migration time); new evidence rows
-- should always supply commit_sha.  The merge gate rejects evidence where
-- any row's commit_sha doesn't match the current branch tip.

ALTER TABLE validation_evidence ADD COLUMN commit_sha TEXT;
