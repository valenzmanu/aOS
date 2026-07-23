# Legacy SQLite golden fixtures

These fixtures exercise the one-way migration from both released aOS 0.1 database layouts:

- `schema-v1/aos.db` comes from commit `7187645`.
- `schema-v2/aos.db` comes from commit `e920ff2`.

The generator does not reproduce either schema with copied SQL. It extracts that commit's
`aos/store.py` through `git archive`, imports the extracted module in isolation, and calls its
actual `Store.initialize()`. It then adds deterministic synthetic data. No live repository state,
credentials, bearer tokens, or user paths enter the fixtures.

The fixtures intentionally have different scopes:

- Schema v1 is the minimal first migration slice: two events with fixed IDs, timestamps, and
  deliberately noncanonical raw JSON payload text, plus one inbox cursor. It has no mission,
  process, lease, or artifact, so event preservation can be implemented before migration
  snapshots and artifact publication.
- Schema v2 is populated for later slices. It contains an active mission/process and liaison lease;
  durable contract, attempt, work-order, worker-session, and provider-observation rows; and one
  safe provider-output artifact.

## Regenerate

Regeneration requires a Git clone containing both source commits:

```text
uv run python tests/legacy/fixtures/generate.py
```

The command stages and validates both databases before replacing the two known fixture
directories. Times, identifiers, token hashes, rows, events, cursors, and artifacts are
deterministic. SQLite files are still provenance-pinned to the recorded Python and SQLite
implementation; review any binary digest change after a toolchain upgrade.

Validate the checked-in outputs without rewriting them:

```text
uv run python tests/legacy/fixtures/generate.py --check
```

`manifest.json` records the full source commit, Git blob and SHA-256 of the extracted historical
store, database and artifact digests, row counts, event high-water marks, inbox cursors, SQLite
integrity and foreign-key results, and a structural fingerprint.

The structural fingerprint is SHA-256 over canonical JSON for:

```sql
SELECT type, name, tbl_name, sql
FROM sqlite_master
WHERE name NOT LIKE 'sqlite_%'
ORDER BY type, name;
```

Table definitions retain their original SQL text in that calculation, so changes to columns,
constraints, or indexes change the fingerprint even when metadata still claims the same schema
version.
