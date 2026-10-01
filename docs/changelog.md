# Changelog

## Unreleased

### Changed

- **SQLAlchemy 2.x is now required** (`sqlalchemy>=2.0,<3`). The internal models moved from the legacy `registry()`/`DeclarativeMeta`/`Column` pattern to `DeclarativeBase`/`Mapped`/`mapped_column`. The public API (`AlchemicalQueues`, `AlchemicalQueue`, `AlchemicalEntry`, `AlchemicalResponse`) did not change.
- `AlchemicalQueue.get()` no longer needs a `BEGIN EXCLUSIVE` transaction on SQLite, and **no longer deletes the entry it returns** — it *claims* it instead (an atomic `UPDATE ... RETURNING` on SQLite, with `FOR UPDATE SKIP LOCKED` added on PostgreSQL/MySQL/Oracle so concurrent workers don't contend on the same rows). A claimed entry stays visible to `qsize()`/`empty()` as outstanding work, and other `get()` calls can't claim it, until you call `respond()` or the new `release()`. **This is why**: previously, a worker that died between `get()` and `respond()` lost the entry silently and permanently, since the row was already gone. Now an unreleased claim simply expires after its `visibility_timeout` and becomes claimable again, so a crashed worker costs you a delay, not the task. `alchemical_queues.tasks.Worker` already does the right thing here (it releases a task's claim as soon as it's handled, success, failure, or retry) — this mainly matters if you use `AlchemicalQueue` directly. See [Database support](usage/databases.md#claims-and-redelivery).
- The project's environment and dependencies are now managed with [uv](https://docs.astral.sh/uv/) instead of Poetry.

### Added

- `AlchemicalQueues` accepts an optional `base` argument: an existing `DeclarativeBase` to attach its tables to, so they can share a registry and metadata with the rest of your application (e.g. Flask-SQLAlchemy's `db.Model`) instead of always getting an isolated one.
- `AlchemicalQueue.get()` and `AlchemicalQueues.get()`/`get_typed()` accept a `visibility_timeout` to control how long a claimed entry stays claimed before becoming available again (see above).
- `AlchemicalQueue.release(entry_id)`: release a claimed entry without recording a response, for when you have nothing to respond with but want the claim freed immediately rather than waiting out its timeout.
- `QueuedTask.done`: `True` once a task has a recorded outcome (success or failure). Added because `task.result` is `None` both while a task hasn't finished yet *and* after it finishes successfully with no return value — there was no way to tell those apart for a task whose success value is legitimately `None`.
- `TaskException.exception_type`: the failed task's original exception class name (e.g. `"ValueError"`), alongside the existing stringified `msg`. `TaskException` also now has a readable `__repr__`/`__str__`.
- `alchemical_worker` accepts `--import module:attribute` as an alternative to the positional engine URL, to run against an app-provided `AlchemicalQueues` instance (one using `base=`, custom table names, or a non-default `visibility_timeout`) instead of always constructing a default one — e.g. `alchemical_worker --import myapp.queues:queues task-queue`.

### Fixed

- On SQLite, `entry_id` and `response_id` could be silently reused once a queue emptied out and refilled, since SQLite recycles a deleted row's rowid by default. Entry and response tables now set `sqlite_autoincrement`, so ids stay unique for the lifetime of the database, matching the documented guarantee.
- `AlchemicalQueue.responses()` deleted expired responses (past their `cleanup_at`) but never committed that delete, so it was silently rolled back when the session closed and expired responses were never actually purged.

## Version 0.1.1

Previous stable release.

## Version 0.1.0

Initial release.
