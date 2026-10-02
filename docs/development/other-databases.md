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

## SQL Server (MSSQL)

```bash
docker run --name mssql -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD=AlchemicalQ1 -p 1433:1433 mcr.microsoft.com/mssql/server:2022-latest
docker exec mssql /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P AlchemicalQ1 -Q "CREATE DATABASE aq_test"
uv run --with pymssql pytest -x --engine "mssql+pymssql://sa:AlchemicalQ1@127.0.0.1:1433/aq_test"
```

`pymssql` bundles FreeTDS, so it needs no system ODBC driver. There's no async driver worth testing against MSSQL this way -- see [Driver limitations](../usage/databases.md#driver-limitations).
