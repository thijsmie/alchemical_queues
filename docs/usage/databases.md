# Database Support

*Alchemical Queues* works with any database backend supported by SQLAlchemy, but different databases have different performance characteristics for concurrent queue operations.

## Supported Databases

### PostgreSQL (Recommended)
PostgreSQL is the recommended database for production deployments with high concurrency requirements.

**Advantages:**
- Full support for `SKIP LOCKED` optimization
- Excellent concurrent performance
- Reliable row-level locking
- Battle-tested for queue workloads

**Connection string example:**
```python
from sqlalchemy import create_engine

engine = create_engine("postgresql+psycopg2://user:password@localhost/dbname")
```

### MySQL / MariaDB
MySQL 8.0+ and MariaDB 10.3+ support `SKIP LOCKED` and work well with *Alchemical Queues*.

**Connection string example:**
```python
engine = create_engine("mysql+pymysql://user:password@localhost/dbname")
```

### SQLite
SQLite works great for development, testing, and low-concurrency production deployments.

**Limitations:**
- No `SKIP LOCKED` support (uses fallback implementation)
- Slightly slower under high concurrency
- Still completely safe and reliable

**Connection string example:**
```python
engine = create_engine("sqlite:///path/to/database.db")
```

For in-memory testing:
```python
engine = create_engine("sqlite:///:memory:")
```

### Oracle
Oracle databases are fully supported with `SKIP LOCKED` optimization.

## Performance Considerations

### SKIP LOCKED Optimization
*Alchemical Queues* automatically detects your database and uses `SKIP LOCKED` when available (PostgreSQL, MySQL, Oracle). This provides significant performance benefits under high concurrency:

**Without SKIP LOCKED (SQLite):**
- Consumer A locks row 1
- Consumer B waits on row 1
- Consumer A deletes row 1
- Consumer B gets nothing, tries again

**With SKIP LOCKED (PostgreSQL/MySQL/Oracle):**
- Consumer A locks row 1
- Consumer B skips row 1, immediately locks row 2
- Both consumers work in parallel

### Concurrency Safety
All queue operations use atomic `DELETE ... RETURNING` statements, ensuring that:
- Each item is dequeued exactly once
- No race conditions or duplicate processing
- Works correctly even with many concurrent consumers

## Testing Against Different Databases

You can run the test suite against different databases using the `--engine` flag:

```bash
# SQLite (default)
uv run pytest

# PostgreSQL
uv run pytest --engine "postgresql+psycopg2://user:pass@localhost/testdb"

# MySQL
uv run pytest --engine "mysql+pymysql://user:pass@localhost/testdb"
```

## Connection Pooling

For production deployments, configure connection pooling:

```python
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool

engine = create_engine(
    "postgresql+psycopg2://user:pass@localhost/dbname",
    poolclass=QueuePool,
    pool_size=10,
    max_overflow=20,
)
```
