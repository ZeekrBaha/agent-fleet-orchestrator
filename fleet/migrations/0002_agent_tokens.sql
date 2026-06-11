-- Fleet migration 0002: per-agent identity tokens
-- Adds token_hash to agents for per-agent authentication.
-- SHA-256 hashes only; plaintext tokens are never stored.
ALTER TABLE agents ADD COLUMN token_hash TEXT;
