from fastapi.testclient import TestClient
from redis.exceptions import TimeoutError as RedisTimeoutError

from app import main
from app.main import app


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.expirations: list[tuple[str, int]] = []
        self.eval_calls: list[tuple[object, ...]] = []
        self.sorted_sets: dict[str, list[tuple[int, str]]] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.expirations.append((key, seconds))
        return True

    async def eval(self, script: str, numkeys: int, *args: object) -> int | list[int]:
        self.eval_calls.append((numkeys, *args))
        key = str(args[0])

        if script == main.SLIDING_WINDOW_LUA:
            window_ms = int(args[1])
            limit = int(args[2])
            member = str(args[3])
            entries = self.sorted_sets.setdefault(key, [])
            if len(entries) >= limit:
                return [0, len(entries), 100_000]
            entries.append((100_000, member))
            await self.expire(key, window_ms)
            return [1, len(entries), 100_000]

        seconds = int(args[1])
        count = await self.incr(key)
        if count == 1:
            await self.expire(key, seconds)
        return count


class TimeoutRedis:
    async def eval(self, script: str, numkeys: int, *args: object) -> None:
        raise RedisTimeoutError("simulated Redis timeout")


def test_counter_requires_a_client_id() -> None:
    response = TestClient(app).get("/counter")

    assert response.status_code == 400
    assert response.json() == {"detail": "X-Client-ID header is required"}


def test_fixed_window_allows_five_requests_then_rejects_the_sixth(monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr(main, "redis_client", fake_redis)
    client = TestClient(app)

    responses = [client.get("/demo", headers={"X-Client-ID": "alice"}) for _ in range(6)]

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 200, 429]
    assert responses[4].json()["remaining"] == 0
    assert fake_redis.expirations == [("rate_limit:fixed:alice", 60)]


def test_failure_between_incr_and_expire_leaves_a_key_without_ttl(monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr(main, "redis_client", fake_redis)

    response = TestClient(app).get(
        "/demo",
        headers={
            "X-Client-ID": "crash-case",
            "X-Simulate-Failure-After-Incr": "true",
        },
    )

    assert response.status_code == 500
    assert fake_redis.values == {"rate_limit:fixed:crash-case": 1}
    assert fake_redis.expirations == []


def test_atomic_fixed_window_runs_increment_and_expiry_as_one_eval(monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr(main, "redis_client", fake_redis)
    client = TestClient(app)

    responses = [
        client.get("/demo-atomic", headers={"X-Client-ID": "alice"})
        for _ in range(6)
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 200, 429]
    assert len(fake_redis.eval_calls) == 6
    assert fake_redis.expirations == [("rate_limit:fixed:atomic:alice", 60)]


def test_sliding_log_keeps_only_allowed_requests(monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr(main, "redis_client", fake_redis)
    client = TestClient(app)

    responses = [
        client.get("/demo-sliding", headers={"X-Client-ID": "alice"})
        for _ in range(6)
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 200, 429]
    entries = fake_redis.sorted_sets["rate_limit:sliding:alice"]
    assert len(entries) == 5
    assert len({member for _, member in entries}) == 5


def test_sliding_limiter_fails_closed_when_redis_times_out(monkeypatch) -> None:
    monkeypatch.setattr(main, "redis_client", TimeoutRedis())

    response = TestClient(app).get(
        "/demo-sliding",
        headers={"X-Client-ID": "alice"},
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json() == {
        "detail": {
            "message": "Rate limiter unavailable",
            "failure_mode": "closed",
        }
    }


def test_metrics_use_bounded_labels_and_exclude_client_id(monkeypatch) -> None:
    fake_redis = FakeRedis()
    monkeypatch.setattr(main, "redis_client", fake_redis)
    client = TestClient(app)

    client.get("/demo-sliding", headers={"X-Client-ID": "private-client-123"})
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "rate_limit_decisions_total" in response.text
    assert 'algorithm="sliding_log",outcome="allowed"' in response.text
    assert "redis_operation_duration_seconds" in response.text
    assert "http_request_duration_seconds" in response.text
    assert "private-client-123" not in response.text
