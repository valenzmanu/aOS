# Frozen schema-3 migration fixture

This directory is a structurally real schema-3 root used to test the explicit schema-3-to-4
upgrade path. It is deliberately frozen: regenerating it with the current journal implementation
would only prove that current code can read its own output.

## Provenance

- Migration implementation: commit
  `1582aa1fc5d070b63ffcad0b6e6689f04f1464bb`
  (`Implement lossless legacy journal migration`).
- Legacy input: the schema-2 fixture generated from commit
  `e920ff23654c16204c7f7547d4ca7952d52c29bd`.
- Migration operation: `OP-STRUCTURE-V3`, invoked as
  `maintenance:fingerprint`.
- Fixed migration time: `2026-07-23T12:00:00.000+00:00`.
- Source/target schema: 2 to 3.
- Legacy event high-water mark: 13; migration boundary event: 14; final event: 20.

The fixture was produced in a detached worktree at the migration implementation commit:

1. Copy `tests/legacy/fixtures/schema-v2/` to a disposable state root.
2. Run that commit's `migrate_legacy_root` with the operation, invoker, and fixed time above.
3. Copy the resulting `aos.db`, `artifacts/`, and `blobs/` here.
4. Copy the protected rollback backup here as `legacy-backup.sqlite3`.

`legacy-backup.sqlite3` is fixture support data, not a file that belongs at the root of a live
state tree. Upgrade tests materialize it at the protected backup path recorded by the migration
before invoking the upgrader. Its byte digest differs from the original schema-2 database because
SQLite's backup API can produce a different page layout; its schema fingerprint and logical
contents are the schema-2 source.

`manifest.json` records all payload-file hashes and semantic anchors. Validate the fixture with:

```sh
uv run pytest tests/v2/test_schema3_fixture_provenance.py
```
