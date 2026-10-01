# Distributed API Rate Limiter

A small FastAPI + Redis system built incrementally to learn practical rate
limiting and distributed-systems behavior.

The primary limiter is `GET /demo-sliding`: it enforces at most five accepted
requests per client in any rolling 60 seconds. `/counter`, `/demo`, and
`/demo-atomic` preserve the learning progression; `/demo-token` demonstrates a
different, burst-friendly policy.

## Run locally

Start the counter experiment:

```powershell
docker compose up --build -d
```

Then send a request:

```powershell
curl.exe -H "X-Client-ID: alice" http://localhost:8000/counter
```

The first rate-limited endpoint is available at `/demo`:

```powershell
curl.exe -H "X-Client-ID: alice" http://localhost:8000/demo
```

The corrected Lua-backed version is available at `/demo-atomic`:

```powershell
curl.exe -H "X-Client-ID: alice" http://localhost:8000/demo-atomic
```

The exact sliding-log version is available at `/demo-sliding`:

```powershell
curl.exe -H "X-Client-ID: alice" http://localhost:8000/demo-sliding
```

The token-bucket comparison is available at `/demo-token`:

```powershell
curl.exe -H "X-Client-ID: alice" http://localhost:8000/demo-token
docker compose exec redis redis-cli HGETALL rate_limit:token:alice
docker compose exec redis redis-cli PTTL rate_limit:token:alice
```

Run the concurrent correctness load test:

```powershell
docker compose run --rm api python scripts/load_test.py --url http://api:8000/demo-sliding --requests 100 --concurrency 10 --client-mode shared
```

For the two-instance experiment, start the optional second API:

```powershell
docker compose --profile multi up -d --build api api-second
```

The same API is then reachable on ports 8000 and 8001. Both processes use the
same Redis instance and enforce one global limit.

Run the unit tests and real-Redis integration test:

```powershell
docker compose --profile test run --rm --build tests
```

GitHub Actions runs the same suite on pushes and pull requests with a Redis
service container. The repository is a reproducible local system; no public
FastAPI or Redis deployment is required to evaluate it.
