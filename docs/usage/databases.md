# Database support

*Alchemical Queues* works with any database backend supported by SQLAlchemy, but different databases have different performance characteristics for concurrent queue operations, and not every driver supports the same SQL features. CI runs the full test suite against every backend and driver listed below (see [Testing against different databases](#testing-against-different-databases)).

## PostgreSQL (recommended for high concurrency)

PostgreSQL supports `SKIP LOCKED`, which lets several workers call `get()` at the same time without waiting on each other's rows. Any driver works, sync or async:

```python
from sqlalchemy import create_engine

engine = create_engine("postgresql+psycopg2://user:password@localhost/dbname")  # psycopg2
engine = create_engine("postgresql+psycopg://user:password@localhost/dbname")  # psycopg 3
engine = create_engine("postgresql+pg8000://user:password@localhost/dbname")  # pg8000
```

```python
from sqlalchemy.ext.asyncio import create_async_engine

engine = create_async_engine("postgresql+asyncpg://user:password@localhost/dbname")
engine = create_async_engine("postgresql+psycopg://user:password@localhost/dbname")
```

## MySQL / MariaDB

MySQL 8.0+ and MariaDB 10.6+ also support `SKIP LOCKED`. Neither supports `UPDATE ... RETURNING`, and MySQL supports no `RETURNING` at all (MariaDB supports it for `DELETE`); *Alchemical Queues* detects this from the engine's dialect and falls back to locking the candidate row first instead, so both are fully supported transparently -- there's nothing to configure.

```python
engine = create_engine("mysql+pymysql://user:password@localhost/dbname")  # pymysql
engine = create_engine("mysql+mysqldb://user:password@localhost/dbname")  # mysqlclient

# MariaDB: use the "mariadb" URL prefix, or let it autodetect under "mysql+..."
engine = create_engine("mariadb+pymysql://user:password@localhost/dbname")
```

```python
engine = create_async_engine("mysql+asyncmy://user:password@localhost/dbname")
engine = create_async_engine("mysql+aiomysql://user:password@localhost/dbname")
```

## Oracle

Oracle is fully supported, including the `SKIP LOCKED` optimization, via `python-oracledb` (thin mode -- no Oracle Instant Client needed), sync and async:

```python
engine = create_engine("oracle+oracledb://user:password@localhost/?service_name=FREEPDB1")
engine = create_async_engine("oracle+oracledb_async://user:password@localhost/?service_name=FREEPDB1")
```

## SQL Server (MSSQL)

Supported via `pymssql` (sync only -- see [Driver limitations](#driver-limitations)):

```python
engine = create_engine("mssql+pymssql://user:password@localhost/dbname")
```

SQL Server has no `SKIP LOCKED` support in SQLAlchemy, so concurrent `get()` calls fall back to plain locking, the same as SQLite -- this is still completely safe, just a bit less parallel under heavy concurrent load.

## SQLite

SQLite works great for development, testing, and low-concurrency deployments, and is what *Alchemical Queues* is tested against by default.

```python
engine = create_engine("sqlite:///path/to/database.db")
```

For in-memory testing:

```python
engine = create_engine("sqlite:///:memory:")
```

SQLite has no `SKIP LOCKED` support, so concurrent `get()` calls fall back to plain locking (see below); this is still completely safe, just a bit less parallel under heavy concurrent load.

## Driver limitations

- **MSSQL has no supported async driver.** The only async DBAPI SQLAlchemy offers for SQL Server (`aioodbc`) needs Microsoft's proprietary ODBC driver installed on the host, which isn't available as a plain pip install; `pymssql`, the only dependency-free driver, is sync only. Use the sync `AlchemicalQueues`/`AlchemicalTaskQueue` API against SQL Server, or an `async_task`-free `Worker` if you need to run tasks.

## How `get()` stays safe under concurrency

Every queue operation goes through the database, so *Alchemical Queues* relies on it, not application-level locks, to keep concurrent workers from stepping on each other. Picking the next entry and claiming it happen as a single atomic `UPDATE ... RETURNING` statement (or, on a dialect without `RETURNING` support, as a `SELECT ... FOR UPDATE` that locks the row followed by an `UPDATE`/`DELETE` by id within the same transaction), so two workers calling `get()` at the same time can never both receive the same entry, and no extra transaction isolation level is needed to make that guarantee hold.

On PostgreSQL, MySQL, MariaDB and Oracle, the statement also picks its candidate row with `FOR UPDATE SKIP LOCKED`, so workers racing for entries skip past rows another worker already has locked instead of waiting on them — this is what lets many workers `get()` in parallel with minimal contention. *Alchemical Queues* detects this automatically from the SQLAlchemy engine's dialect; there is nothing to configure.

## Claims and redelivery

This section applies to [`AlchemicalTaskQueue`][alchemical_queues.AlchemicalTaskQueue], obtained via [`queues.get_task_queue(...)`][alchemical_queues.AlchemicalQueues.get_task_queue] — the plain [`AlchemicalQueue`][alchemical_queues.AlchemicalQueue] you get from `queues.get(...)` has none of this: its `get()` removes an entry outright, with no claims, redelivery, or `visibility_timeout` to think about. Reach for `get_task_queue()` when you need at-least-once delivery with redelivery on failure/crash — which is exactly what `alchemical_queues.tasks` builds on.

`AlchemicalTaskQueue.get()` doesn't remove an entry from the queue, it *claims* it: the entry stays in the table, marked unavailable to other `get()` calls, until you call [`discard()`][alchemical_queues.AlchemicalTaskQueue.discard] (remove it for good) or [`release()`][alchemical_queues.AlchemicalTaskQueue.release] (put it back, claimable again immediately). If neither ever happens — your process crashes, gets OOM-killed, or is forcibly stopped mid-task — the claim expires after its `visibility_timeout` and the entry becomes claimable again, so a dead worker loses at most the time left on the timeout, not the task itself.

Every claim carries a `claim_token` (on `entry.claim_token`), a random value fresh for that specific claim. `release()`, `discard()`, and `extend()` (below) all require it alongside the `entry_id`, and raise `ClaimExpired` if it doesn't match the entry's *current* claim. This is what keeps a worker that's running late from corrupting a claim that's since moved on to someone else: even though the entry_id is identical, a stale claim_token means "that's not your claim anymore."

```python
from alchemical_queues import ClaimExpired

queue = queues.get_task_queue("pdf-generation", visibility_timeout=timedelta(minutes=10))

entry = queue.get()
if entry is not None:
    try:
        result = do_the_work(entry.data)
        # Prove we still hold the claim *before* recording a response --
        # if someone else redelivered and already finished this, discard()
        # raises here and we never file a (now-redundant) response.
        queue.discard(entry.entry_id, entry.claim_token)
        queue.respond(entry.entry_id, result)
    except ClaimExpired:
        pass  # someone else already handled this; nothing more to do
    except Exception:
        queue.release(entry.entry_id, entry.claim_token)  # retry sooner than the timeout
        raise
```

Pick a `visibility_timeout` comfortably longer than your task normally takes. `qsize()`/`empty()` count claimed-but-unreleased entries as outstanding, since the work isn't actually done yet.

### Long-running work: `extend()` and `keepalive_every`

If a task can run longer than `visibility_timeout`, call [`extend()`][alchemical_queues.AlchemicalTaskQueue.extend] periodically while you're still on it, to push the claim's expiry out:

```python
entry = queue.get()
...
queue.extend(entry.entry_id, entry.claim_token)  # pushes claimed_until further out
```

`extend()` also requires the matching `claim_token` and raises `ClaimExpired` the same way, so a worker that calls it too late — after someone else has already reclaimed the entry — finds out immediately rather than carrying on under a false assumption.

`alchemical_queues.tasks.Worker` can do this automatically: pass `keepalive_every` and it runs a background thread that calls `extend()` at that interval for as long as your task handler is running, stopping as soon as it returns.

```python
from alchemical_queues.tasks import Worker

Worker(queue, keepalive_every=timedelta(minutes=3)).work()
```

Without a keepalive (manual or via `Worker`), a task that outruns `visibility_timeout` risks being redelivered to, and *run* by, a second worker while the first is still on it — fencing guarantees at most one of them ever gets to record a response (the other logs a warning and discards its own result instead of risking a duplicate), but it can't stop the task's *handler* from genuinely running twice, so idempotent task bodies are worth aiming for regardless of whether you use a keepalive.

`alchemical_queues.tasks.Worker` handles the claim lifecycle for you already (discard-then-respond, or discard-then-retry, in every case including crashes and keyboard interrupts) — you only need to think about `visibility_timeout` and, for long tasks, `keepalive_every` directly when using `AlchemicalTaskQueue` on its own.

!!! note "entry_id / response_id uniqueness on SQLite"

    On SQLite, `entry_id` and `response_id` are created with `sqlite_autoincrement`, so a deleted row's id is never reused even after the queue empties out and refills. Other backends don't need this: their native auto-incrementing primary keys never reuse ids either.

## Testing against different databases

You can run the test suite against a database of your choice with the `--engine` flag (sync) or `--async-engine` flag (the async tests, `tests/test_async_main.py`/`tests/test_async_task.py`):

```bash
# SQLite (the default)
uv run pytest

# PostgreSQL
uv run pytest --engine "postgresql+psycopg2://user:pass@localhost/testdb"

# MySQL / MariaDB
uv run pytest --engine "mysql+pymysql://user:pass@localhost/testdb"
uv run pytest --engine "mariadb+pymysql://user:pass@localhost/testdb"

# Oracle
uv run pytest --engine "oracle+oracledb://user:pass@localhost/?service_name=FREEPDB1"

# SQL Server
uv run pytest --engine "mssql+pymssql://user:pass@localhost/testdb"
```

See [Testing against other databases](../development/other-databases.md) for a quick way to spin up each of these locally with Docker; CI (`.github/workflows/testsuite.yml`) runs every driver listed on this page against a real service container of its database on every push.
