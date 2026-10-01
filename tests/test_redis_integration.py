"""Check the final sliding limiter against real Redis, not a fake client."""

import asyncio
import os
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from app import main
from app.main import app


@pytest.mark.integration
def test_sliding_limiter_uses_one_expiring_sorted_set() -> None:
    client_id = f"integration-{uuid.uuid4().hex}"
    key = f"rate_limit:sliding:{client_id}"
    redis_client = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)

    try:
        with TestClient(app) as client:
            responses = [
                client.get("/demo-sliding", headers={"X-Client-ID": client_id})
                for _ in range(6)
            ]

        assert [response.status_code for response in responses] == [
            200, 200, 200, 200, 200, 429
        ]
        assert redis_client.type(key) == "zset"
        assert redis_client.zcard(key) == 5
        assert 0 < redis_client.pttl(key) <= 60_000
        assert len(set(redis_client.zrange(key, 0, -1))) == 5
    finally:
        redis_client.delete(key)
        redis_client.close()


@pytest.mark.integration
def test_token_bucket_allows_five_concurrent_requests_then_refills(monkeypatch) -> None:
    client_id = f"token-integration-{uuid.uuid4().hex}"
    key = f"rate_limit:token:{client_id}"
    redis_client = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)

    async def exercise_bucket() -> None:
        async_redis = AsyncRedis.from_url(os.environ["REDIS_URL"], decode_responses=True)
        monkeypatch.setattr(main, "redis_client", async_redis)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                burst = await asyncio.gather(*(
                    client.get("/demo-token", headers={"X-Client-ID": client_id})
                    for _ in range(10)
                ))
                assert sorted(response.status_code for response in burst) == [
                    200, 200, 200, 200, 200, 429, 429, 429, 429, 429
                ]
                assert redis_client.type(key) == "hash"
                assert set(redis_client.hgetall(key)) == {"credit", "last_ms"}
                assert 0 < redis_client.pttl(key) <= 60_000

                # Simulate 24 idle seconds using Redis's own clock.
                seconds, microseconds = redis_client.time()
                redis_now_ms = seconds * 1000 + microseconds // 1000
                redis_client.hset(
                    key,
                    mapping={"credit": 0, "last_ms": redis_now_ms - 24_000},
                )
                refilled = [
                    await client.get("/demo-token", headers={"X-Client-ID": client_id})
                    for _ in range(3)
                ]
                assert [response.status_code for response in refilled] == [200, 200, 429]
        finally:
            await async_redis.aclose()

    try:
        asyncio.run(exercise_bucket())
    finally:
        redis_client.delete(key)
        redis_client.close()
