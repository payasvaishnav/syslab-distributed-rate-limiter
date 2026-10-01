"""The first Redis experiment: a per-client counter, not a rate limiter yet."""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_CONNECT_TIMEOUT_SECONDS = float(
    os.getenv("REDIS_CONNECT_TIMEOUT_SECONDS", "0.2")
)
REDIS_SOCKET_TIMEOUT_SECONDS = float(
    os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "0.2")
)
REDIS_OPERATION_DEADLINE_SECONDS = float(
    os.getenv("REDIS_OPERATION_DEADLINE_SECONDS", "0.25")
)
redis_client = Redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
    socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    # Keep the HTTP request's latency bounded. Higher-level retries would need
    # their own explicit end-to-end time budget.
    retry=Retry(NoBackoff(), 0),
)
REQUEST_LIMIT = 5
WINDOW_SECONDS = 60
WINDOW_MILLISECONDS = WINDOW_SECONDS * 1000
TOKEN_REFILL_MILLISECONDS = 12_000

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("rate_limiter")

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "HTTP requests handled by the API.",
    ("method", "endpoint", "status"),
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "End-to-end HTTP request duration.",
    ("method", "endpoint"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
RATE_LIMIT_DECISIONS_TOTAL = Counter(
    "rate_limit_decisions_total",
    "Rate-limit decisions made by algorithm and outcome.",
    ("algorithm", "outcome"),
)
REDIS_OPERATION_DURATION_SECONDS = Histogram(
    "redis_operation_duration_seconds",
    "Redis rate-limit operation duration.",
    ("operation", "outcome"),
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5),
)

# KEYS contains Redis keys; ARGV contains ordinary arguments. Keeping keys in
# KEYS matters for Redis Cluster, which must know which hash slots a script uses.
FIXED_WINDOW_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""

SLIDING_WINDOW_LUA = """
local redis_time = redis.call('TIME')
local now_ms = (redis_time[1] * 1000) + math.floor(redis_time[2] / 1000)
local cutoff_ms = now_ms - tonumber(ARGV[1])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff_ms)
local count = redis.call('ZCARD', KEYS[1])

if count >= tonumber(ARGV[2]) then
    return {0, count, now_ms}
end

redis.call('ZADD', KEYS[1], now_ms, ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[1])
return {1, count + 1, now_ms}
"""

# One token is represented by 12,000 integer credits. Redis time adds one
# credit per millisecond, avoiding floating-point rounding in stored state.
TOKEN_BUCKET_LUA = """
local redis_time = redis.call('TIME')
local now_ms = (redis_time[1] * 1000) + math.floor(redis_time[2] / 1000)
local token_cost = tonumber(ARGV[2])
local full_credit = tonumber(ARGV[1]) * token_cost
local state = redis.call('HMGET', KEYS[1], 'credit', 'last_ms')
local credit = tonumber(state[1]) or full_credit
local last_ms = tonumber(state[2]) or now_ms

now_ms = math.max(now_ms, last_ms)
credit = math.min(full_credit, credit + (now_ms - last_ms))

local allowed = 0
if credit >= token_cost then
    credit = credit - token_cost
    allowed = 1
end

redis.call('HSET', KEYS[1], 'credit', credit, 'last_ms', now_ms)
redis.call('PEXPIRE', KEYS[1], full_credit)
local retry_after_ms = 0
if allowed == 0 then
    retry_after_ms = token_cost - credit
end
return {allowed, credit, now_ms, retry_after_ms}
"""

app = FastAPI(title="Distributed Rate Limiter")


def log_event(event: str, **fields: object) -> None:
    """Write one machine-parseable JSON object per log line."""
    logger.info(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event,
                **fields,
            },
            separators=(",", ":"),
        )
    )


@app.middleware("http")
async def observe_http_requests(request: Request, call_next):
    started_at = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        endpoint = getattr(route, "path", "unmatched")
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method,
            endpoint=endpoint,
            status=str(status_code),
        ).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(
            method=request.method,
            endpoint=endpoint,
        ).observe(time.perf_counter() - started_at)


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Expose this process's metrics in the Prometheus text format."""
    return Response(
        content=generate_latest(),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )


def fail_closed_response() -> HTTPException:
    """Reject traffic when Redis cannot make a trustworthy limit decision."""
    return HTTPException(
        status_code=503,
        detail={
            "message": "Rate limiter unavailable",
            "failure_mode": "closed",
        },
        headers={"Retry-After": "1"},
    )


@app.get("/counter")
async def increment_counter(x_client_id: str | None = Header(default=None)) -> dict[str, object]:
    """Increment and return this client's Redis-backed request counter."""
    if not x_client_id:
        raise HTTPException(status_code=400, detail="X-Client-ID header is required")

    key = f"experiment:counter:{x_client_id}"
    try:
        async with asyncio.timeout(REDIS_OPERATION_DEADLINE_SECONDS):
            count = await redis_client.incr(key)
    except (RedisError, TimeoutError) as error:
        raise fail_closed_response() from error

    return {"client_id": x_client_id, "redis_key": key, "count": count}


@app.get("/demo")
async def limited_demo(
    x_client_id: str | None = Header(default=None),
    x_simulate_failure_after_incr: bool = Header(default=False),
) -> dict[str, object]:
    """A first, intentionally non-atomic fixed-window rate limiter."""
    if not x_client_id:
        raise HTTPException(status_code=400, detail="X-Client-ID header is required")

    key = f"rate_limit:fixed:{x_client_id}"
    try:
        async with asyncio.timeout(REDIS_OPERATION_DEADLINE_SECONDS):
            count = await redis_client.incr(key)
            # Learning-only fault injection. A real process crash here would
            # produce the same Redis state: EXPIRE was never sent.
            if x_simulate_failure_after_incr:
                raise HTTPException(
                    status_code=500,
                    detail="Simulated failure after INCR; EXPIRE was not executed",
                )
            if count == 1:
                await redis_client.expire(key, WINDOW_SECONDS)
    except (RedisError, TimeoutError) as error:
        raise fail_closed_response() from error

    if count > REQUEST_LIMIT:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Rate limit exceeded",
                "limit": REQUEST_LIMIT,
                "window_seconds": WINDOW_SECONDS,
            },
        )

    return {
        "message": "Request allowed",
        "client_id": x_client_id,
        "redis_key": key,
        "count_in_window": count,
        "remaining": REQUEST_LIMIT - count,
    }


@app.get("/demo-atomic")
async def atomic_limited_demo(
    x_client_id: str | None = Header(default=None),
) -> dict[str, object]:
    """Fixed-window limiting with INCR and EXPIRE in one atomic Lua script."""
    if not x_client_id:
        raise HTTPException(status_code=400, detail="X-Client-ID header is required")

    key = f"rate_limit:fixed:atomic:{x_client_id}"
    try:
        async with asyncio.timeout(REDIS_OPERATION_DEADLINE_SECONDS):
            # EVAL script numkeys key... arg...
            count = int(
                await redis_client.eval(FIXED_WINDOW_LUA, 1, key, WINDOW_SECONDS)
            )
    except (RedisError, TimeoutError) as error:
        raise fail_closed_response() from error

    if count > REQUEST_LIMIT:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Rate limit exceeded",
                "limit": REQUEST_LIMIT,
                "window_seconds": WINDOW_SECONDS,
            },
        )

    return {
        "message": "Request allowed",
        "client_id": x_client_id,
        "redis_key": key,
        "count_in_window": count,
        "remaining": REQUEST_LIMIT - count,
    }


@app.get("/demo-sliding")
async def sliding_window_demo(
    x_client_id: str | None = Header(default=None),
) -> dict[str, object]:
    """An exact sliding-log limiter backed by a Redis sorted set."""
    if not x_client_id:
        raise HTTPException(status_code=400, detail="X-Client-ID header is required")

    key = f"rate_limit:sliding:{x_client_id}"
    request_id = uuid.uuid4().hex
    redis_started_at = time.perf_counter()

    try:
        async with asyncio.timeout(REDIS_OPERATION_DEADLINE_SECONDS):
            allowed, count, now_ms = await redis_client.eval(
                SLIDING_WINDOW_LUA,
                1,
                key,
                WINDOW_MILLISECONDS,
                REQUEST_LIMIT,
                request_id,
            )
    except (RedisError, TimeoutError) as error:
        redis_duration = time.perf_counter() - redis_started_at
        REDIS_OPERATION_DURATION_SECONDS.labels(
            operation="sliding_log",
            outcome="error",
        ).observe(redis_duration)
        RATE_LIMIT_DECISIONS_TOTAL.labels(
            algorithm="sliding_log",
            outcome="dependency_error",
        ).inc()
        log_event(
            "rate_limit_decision",
            client_id=x_client_id,
            request_id=request_id,
            algorithm="sliding_log",
            outcome="dependency_error",
            redis_duration_ms=round(redis_duration * 1000, 3),
            error_type=type(error).__name__,
        )
        raise fail_closed_response() from error

    redis_duration = time.perf_counter() - redis_started_at
    REDIS_OPERATION_DURATION_SECONDS.labels(
        operation="sliding_log",
        outcome="success",
    ).observe(redis_duration)
    outcome = "allowed" if bool(allowed) else "rejected"
    RATE_LIMIT_DECISIONS_TOTAL.labels(
        algorithm="sliding_log",
        outcome=outcome,
    ).inc()
    log_event(
        "rate_limit_decision",
        client_id=x_client_id,
        request_id=request_id,
        algorithm="sliding_log",
        outcome=outcome,
        requests_in_window=int(count),
        redis_duration_ms=round(redis_duration * 1000, 3),
    )

    if not bool(allowed):
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Rate limit exceeded",
                "limit": REQUEST_LIMIT,
                "window_seconds": WINDOW_SECONDS,
                "requests_in_window": int(count),
            },
        )

    return {
        "message": "Request allowed",
        "client_id": x_client_id,
        "redis_key": key,
        "count_in_window": int(count),
        "remaining": REQUEST_LIMIT - int(count),
        "redis_time_ms": int(now_ms),
    }


@app.get("/demo-token", response_model=None)
async def token_bucket_demo(
    x_client_id: str | None = Header(default=None),
) -> dict[str, object] | Response:
    """Allow short bursts using one constant-size Redis hash per client."""
    if not x_client_id:
        raise HTTPException(status_code=400, detail="X-Client-ID header is required")

    key = f"rate_limit:token:{x_client_id}"
    redis_started_at = time.perf_counter()
    try:
        async with asyncio.timeout(REDIS_OPERATION_DEADLINE_SECONDS):
            allowed, credit, now_ms, retry_after_ms = await redis_client.eval(
                TOKEN_BUCKET_LUA,
                1,
                key,
                REQUEST_LIMIT,
                TOKEN_REFILL_MILLISECONDS,
            )
    except (RedisError, TimeoutError) as error:
        redis_duration = time.perf_counter() - redis_started_at
        REDIS_OPERATION_DURATION_SECONDS.labels(
            operation="token_bucket", outcome="error"
        ).observe(redis_duration)
        RATE_LIMIT_DECISIONS_TOTAL.labels(
            algorithm="token_bucket", outcome="dependency_error"
        ).inc()
        log_event(
            "rate_limit_decision",
            client_id=x_client_id,
            algorithm="token_bucket",
            outcome="dependency_error",
            redis_duration_ms=round(redis_duration * 1000, 3),
            error_type=type(error).__name__,
        )
        raise fail_closed_response() from error

    redis_duration = time.perf_counter() - redis_started_at
    REDIS_OPERATION_DURATION_SECONDS.labels(
        operation="token_bucket", outcome="success"
    ).observe(redis_duration)
    outcome = "allowed" if bool(allowed) else "rejected"
    RATE_LIMIT_DECISIONS_TOTAL.labels(
        algorithm="token_bucket", outcome=outcome
    ).inc()
    log_event(
        "rate_limit_decision",
        client_id=x_client_id,
        algorithm="token_bucket",
        outcome=outcome,
        credit=int(credit),
        redis_duration_ms=round(redis_duration * 1000, 3),
    )

    if not bool(allowed):
        return Response(
            content=json.dumps({
                "detail": "Rate limit exceeded",
                "retry_after_ms": int(retry_after_ms),
            }),
            status_code=429,
            media_type="application/json",
            headers={"Retry-After": str((int(retry_after_ms) + 999) // 1000)},
        )

    return {
        "message": "Request allowed",
        "client_id": x_client_id,
        "redis_key": key,
        "whole_tokens_remaining": int(credit) // TOKEN_REFILL_MILLISECONDS,
        "credit": int(credit),
        "redis_time_ms": int(now_ms),
    }
