# Database support

*Alchemical Queues* works with any database backend supported by SQLAlchemy, but different databases have different performance characteristics for concurrent queue operations.

## PostgreSQL (recommended for high concurrency)

PostgreSQL supports `SKIP LOCKED`, which lets several workers call `get()` at the same time without waiting on each other's rows.

```python
from sqlalchemy import create_engine

engine = create_engine("postgresql+psycopg2://user:password@localhost/dbname")
```

## MySQL / MariaDB

MySQL 8.0+ and MariaDB 10.6+ also support `SKIP LOCKED`.

```python
engine = create_engine("mysql+pymysql://user:password@localhost/dbname")
```

## Oracle

Oracle is fully supported, including the `SKIP LOCKED` optimization.

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

## How `get()` stays safe under concurrency

Every queue operation goes through the database, so *Alchemical Queues* relies on it, not application-level locks, to keep concurrent workers from stepping on each other. Picking the next entry and removing it from the queue happen as a single atomic `DELETE ... RETURNING` statement, so two workers calling `get()` at the same time can never both receive the same entry, and no extra transaction isolation level or lock is needed to make that guarantee hold.

On PostgreSQL, MySQL and Oracle, the statement also picks its candidate row with `FOR UPDATE SKIP LOCKED`, so workers racing for entries skip past rows another worker already has locked instead of waiting on them — this is what lets many workers `get()` in parallel with minimal contention. *Alchemical Queues* detects this automatically from the SQLAlchemy engine's dialect; there is nothing to configure.

!!! note "entry_id / response_id uniqueness on SQLite"

    On SQLite, `entry_id` and `response_id` are created with `sqlite_autoincrement`, so a deleted row's id is never reused even after the queue empties out and refills. Other backends don't need this: their native auto-incrementing primary keys never reuse ids either.

## Testing against different databases

You can run the test suite against a database of your choice with the `--engine` flag:

```bash
# SQLite (the default)
uv run pytest

# PostgreSQL
uv run pytest --engine "postgresql+psycopg2://user:pass@localhost/testdb"

# MySQL
uv run pytest --engine "mysql+pymysql://user:pass@localhost/testdb"
```

See [Testing against Postgres](../development/postgres.md) for a quick way to spin up a local PostgreSQL instance with Docker.
