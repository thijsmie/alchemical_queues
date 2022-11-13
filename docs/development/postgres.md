# Testing against Postgres

Running against postgres locally with a postgres docker

```bash
docker run --name postgres -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=aq_db -p 5455:5432 postgres:latest
export postgres_ip=`docker inspect -f '\{\{range.NetworkSettings.Networks\}\}\{\{.IPAddress\}\}\{\{end\}\}' postgres`
pytest -x --engine "postgresql+psycopg2://postgres:postgres@${postgres_ip}/aq_db"
```
