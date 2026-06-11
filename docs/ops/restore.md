# Restoring Fleet from Backup

Fleet backups are `.tar.gz` archives created by `python -m fleet.cli backup`.
Each archive contains:

- The SQLite database file (e.g. `fleet.db`)
- `fleet/manifests/` — role and policy YAML files
- `backup_meta.json` — timestamp, source DB path, format version

## Restore procedure

1. **Stop the server** — ensure no writes are in flight before replacing the DB:

   ```bash
   sudo systemctl stop fleet
   ```

2. **Extract the backup**:

   ```bash
   tar -xzf fleet-backup-<ts>.tar.gz -C /tmp/fleet-restore/
   ```

3. **Replace the DB file** — copy the extracted DB over the live one
   (use `cp` rather than `mv` to preserve permissions and ownership):

   ```bash
   cp /tmp/fleet-restore/fleet.db /var/lib/fleet/fleet.db
   ```

4. **Optionally restore manifests** (only needed if policy files changed):

   ```bash
   cp -r /tmp/fleet-restore/fleet/manifests/ /opt/fleet/fleet/manifests/
   ```

5. **Restart the server**:

   ```bash
   sudo systemctl start fleet
   ```

## What is recovered

Persisted state resumes from the point of the backup:

- All agent records, worktree records, tasks, events, and approvals are restored.
- The policy manifests (role definitions, tool allowlists) are restored if
  you replaced them in step 4.

## What is lost

- **In-flight agent sessions** — any session that was running at the time of
  the backup is not recoverable. Agents whose `status` is `running` in the
  restored DB are orphaned; run `python -m fleet.cli doctor` to identify and
  clean them up manually.
- **Events between backup and failure** — events written after the backup
  timestamp are gone.

## Verify the restore

```bash
python -m fleet.cli doctor --db /var/lib/fleet/fleet.db
```

All checks should report `[OK]` after a clean restore.
