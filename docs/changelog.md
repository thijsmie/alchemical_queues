# Changelog

## Unreleased

### Changed

- **SQLAlchemy 2.x is now required** (`sqlalchemy>=2.0,<3`). The internal models moved from the legacy `registry()`/`DeclarativeMeta`/`Column` pattern to `DeclarativeBase`/`Mapped`/`mapped_column`.
- **`AlchemicalQueue` and `AlchemicalTaskQueue` are now separate classes.** `AlchemicalQueue` (from `queues.get()`/`get_typed()`) stays a minimal FIFO/priority queue: `put()`, `get()` (deletes and returns the next entry outright), `qsize()`, `empty()`, `clear()`. Everything related to reliable task delivery -- claims, `visibility_timeout`, `release()`/`discard()`/`extend()`, `respond()`/`responses()` -- moved to the new `AlchemicalTaskQueue`, obtained via `queues.get_task_queue()`/`get_task_queue_typed()`. `alchemical_queues.tasks.Worker` now requires an `AlchemicalTaskQueue`. **Why**: most users putting plain data through a queue never touch claims or responses at all, and the claim-based `get()` added below bulked up every `AlchemicalQueue` with machinery that's only relevant to the task-queue use case. See [Database support](usage/databases.md#claims-and-redelivery).
- `AlchemicalTaskQueue.get()` no longer needs a `BEGIN EXCLUSIVE` transaction on SQLite, and **no longer deletes the entry it returns** — it *claims* it instead (an atomic `UPDATE ... RETURNING` on SQLite, with `FOR UPDATE SKIP LOCKED` added on PostgreSQL/MySQL/Oracle so concurrent workers don't contend on the same rows). A claimed entry stays visible to `qsize()`/`empty()` as outstanding work, and other `get()` calls can't claim it, until you call `discard()` or `release()` (both new, see below). **This is why**: previously, a worker that died between `get()` and `respond()` lost the entry silently and permanently, since the row was already gone. Now an unreleased claim simply expires after its `visibility_timeout` and becomes claimable again, so a crashed worker costs you a delay, not the task. `alchemical_queues.tasks.Worker` already does the right thing here (claim handling, retries, and keyboard interrupts all go through the same discard/release logic) — this mainly matters if you use `AlchemicalTaskQueue` directly.
- `AlchemicalTaskQueue.respond()` no longer touches the claimed entry at all -- it only ever recorded a response, and conflating "record an outcome" with "manage the claim" turned out to hide a real bug (see Fixed, below). Call `discard()` once you've responded, same as `tasks.Worker` does.
- The project's environment and dependencies are now managed with [uv](https://docs.astral.sh/uv/) instead of Poetry.

### Added

- `AlchemicalQueues` accepts an optional `base` argument: an existing `DeclarativeBase` to attach its tables to, so they can share a registry and metadata with the rest of your application (e.g. Flask-SQLAlchemy's `db.Model`) instead of always getting an isolated one.
- `AlchemicalQueues.get_task_queue()`/`get_task_queue_typed()`: obtain an `AlchemicalTaskQueue` by name, accepting a `visibility_timeout` to control how long a claimed entry stays claimed before becoming available again (see above).
- `AlchemicalTaskQueue.release(entry_id, claim_token)`: put a claimed entry back in the queue, claimable again immediately, without recording a response.
- `AlchemicalTaskQueue.discard(entry_id, claim_token)`: remove a claimed entry entirely without recording a response.
- `AlchemicalTaskQueue.extend(entry_id, claim_token, by=None)`: push a claim's expiry further out, for work that runs longer than `visibility_timeout`. `tasks.Worker(queue, keepalive_every=...)` does this automatically in a background thread for as long as a task handler is running.
- `AlchemicalEntry.claim_token`: a random value fresh for each claim, required by `release()`/`discard()`/`extend()` alongside the entry_id. This is a deliberate fencing token: if a claim has since expired and been reclaimed by someone else (same entry_id, new claim_token), a worker that's still running late can't mistake its own stale claim for the current one -- those calls raise the new `ClaimExpired` instead of silently succeeding against a claim that's moved on.
- `QueuedTask.done`: `True` once a task has a recorded outcome (success or failure). Added because `task.result` is `None` both while a task hasn't finished yet *and* after it finishes successfully with no return value — there was no way to tell those apart for a task whose success value is legitimately `None`.
- `TaskException.exception_type`: the failed task's original exception class name (e.g. `"ValueError"`), alongside the existing stringified `msg`. `TaskException` also now has a readable `__repr__`/`__str__`.
- `alchemical_worker` accepts `--import module:attribute` as an alternative to the positional engine URL, to run against an app-provided `AlchemicalQueues` instance (one using `base=`, custom table names, or a non-default `visibility_timeout`) instead of always constructing a default one — e.g. `alchemical_worker --import myapp.queues:queues task-queue`.

### Fixed

- On SQLite, `entry_id` and `response_id` could be silently reused once a queue emptied out and refilled, since SQLite recycles a deleted row's rowid by default. Entry and response tables now set `sqlite_autoincrement`, so ids stay unique for the lifetime of the database, matching the documented guarantee.
- `responses()` deleted expired responses (past their `cleanup_at`) but never committed that delete, so it was silently rolled back when the session closed and expired responses were never actually purged.
- A claim whose `visibility_timeout` lapsed while a worker was still (genuinely) working on it could be redelivered, finished by a second worker, *and then* have the first worker's now-stale `respond()` call also succeed -- recording two responses for one entry. Found with a multi-threaded chaos test against SQLite mixing successful, released, discarded, and abandoned ("crashed") claims across many workers; fixed by the `claim_token` fencing described above, which makes a stale worker's `discard()`/`release()`/`extend()` call raise `ClaimExpired` instead of silently succeeding. `tasks.Worker` now proves it still holds a task's claim (via `discard()`) *before* responding or retrying, not after, so a duplicate response can no longer happen as long as responses go through `discard()`-then-`respond()`, the same order `tasks.Worker` itself uses.

## Version 0.1.1

Previous stable release.

## Version 0.1.0

Initial release.
