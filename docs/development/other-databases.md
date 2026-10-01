# Testing against other databases

See [Testing against Postgres](postgres.md) for PostgreSQL. The other backends CI covers (`.github/workflows/testsuite.yml`) can be spun up locally the same way, with Docker.

## MySQL

```bash
docker run --name mysql -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=aq_test -p 3306:3306 mysql:8
uv run --with pymysql pytest -x --engine "mysql+pymysql://root:root@127.0.0.1:3306/aq_test"
```

Async tests need `asyncmy` instead (not `aiomysql` -- see [Driver limitations](../usage/databases.md#driver-limitations)):

```bash
uv run --with asyncmy pytest -x tests/test_async_main.py tests/test_async_task.py --async-engine "mysql+asyncmy://root:root@127.0.0.1:3306/aq_test"
```

## MariaDB

Same as MySQL, but with the `mariadb` image and the `mariadb+...` URL prefix:

```bash
docker run --name mariadb -e MARIADB_ROOT_PASSWORD=root -e MARIADB_DATABASE=aq_test -p 3306:3306 mariadb:11
uv run --with pymysql pytest -x --engine "mariadb+pymysql://root:root@127.0.0.1:3306/aq_test"
```

## Oracle

[`gvenzl/oracle-free`](https://github.com/gvenzl/oci-oracle-free) is the fastest-starting Oracle image available; the default service name is `FREEPDB1`.

```bash
docker run --name oracle -e ORACLE_PASSWORD=oracle -p 1521:1521 gvenzl/oracle-free:23-slim-faststart
uv run --with oracledb pytest -x --engine "oracle+oracledb://system:oracle@127.0.0.1:1521/?service_name=FREEPDB1"
```

`python-oracledb`'s thin mode needs no Oracle Instant Client install, and the same package covers the async driver (`oracle+oracledb_async://...`).

## SQL Server (MSSQL)

```bash
docker run --name mssql -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD=AlchemicalQ1 -p 1433:1433 mcr.microsoft.com/mssql/server:2022-latest
docker exec mssql /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P AlchemicalQ1 -Q "CREATE DATABASE aq_test"
uv run --with pymssql pytest -x --engine "mssql+pymssql://sa:AlchemicalQ1@127.0.0.1:1433/aq_test"
```

`pymssql` bundles FreeTDS, so it needs no system ODBC driver. There's no async driver worth testing against MSSQL this way -- see [Driver limitations](../usage/databases.md#driver-limitations).
