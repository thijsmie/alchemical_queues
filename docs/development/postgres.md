# Testing against Postgres

Running against postgres locally with a postgres docker

```bash
docker run --name postgres -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=aq_db -p 5455:5432 postgres:latest
export postgres_ip=`docker inspect -f '\{\{range.NetworkSettings.Networks\}\}\{\{.IPAddress\}\}\{\{end\}\}' postgres`
pytest -x --engine "postgresql+psycopg2://postgres:postgres@${postgres_ip}/aq_db"
```

The async tests (`tests/test_async_main.py`/`tests/test_async_task.py`) take a separate `--async-engine` flag instead, since they need an async driver (`sqlalchemy+asyncpg`/`sqlalchemy+psycopg` rather than `psycopg2`):

```bash
pip install asyncpg  # or: pip install "psycopg[binary]"
pytest -x tests/test_async_main.py tests/test_async_task.py --async-engine "postgresql+asyncpg://postgres:postgres@${postgres_ip}/aq_db"
```
