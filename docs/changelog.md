# Changelog

## Unreleased

### Changed

- **SQLAlchemy 2.x is now required** (`sqlalchemy>=2.0,<3`). The internal models moved from the legacy `registry()`/`DeclarativeMeta`/`Column` pattern to `DeclarativeBase`/`Mapped`/`mapped_column`. The public API (`AlchemicalQueues`, `AlchemicalQueue`, `AlchemicalEntry`, `AlchemicalResponse`) did not change.
- `AlchemicalQueue.get()` no longer needs a `BEGIN EXCLUSIVE` transaction on SQLite. Picking and removing the next entry now happens as a single atomic `DELETE ... RETURNING` statement, which needs no extra isolation and no longer locks the whole engine for unrelated code sharing it. On PostgreSQL, MySQL and Oracle this also uses `FOR UPDATE SKIP LOCKED` so concurrent workers don't contend on the same rows. See [Database support](usage/databases.md).
- The project's environment and dependencies are now managed with [uv](https://docs.astral.sh/uv/) instead of Poetry.

### Added

- `AlchemicalQueues` accepts an optional `base` argument: an existing `DeclarativeBase` to attach its tables to, so they can share a registry and metadata with the rest of your application (e.g. Flask-SQLAlchemy's `db.Model`) instead of always getting an isolated one.

### Fixed

- On SQLite, `entry_id` and `response_id` could be silently reused once a queue emptied out and refilled, since SQLite recycles a deleted row's rowid by default. Entry and response tables now set `sqlite_autoincrement`, so ids stay unique for the lifetime of the database, matching the documented guarantee.
- `AlchemicalQueue.responses()` deleted expired responses (past their `cleanup_at`) but never committed that delete, so it was silently rolled back when the session closed and expired responses were never actually purged.

## Version 0.1.1

Previous stable release.

## Version 0.1.0

Initial release.
