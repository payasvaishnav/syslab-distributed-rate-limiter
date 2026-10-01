"""Small concurrent HTTP load generator for the rate-limiter experiments."""

import argparse
import asyncio
import json
import math
import time
import uuid
from collections import Counter

import httpx


def percentile(values: list[float], percentage: int) -> float:
    """Return a nearest-rank percentile from a non-empty sample."""
    ordered = sorted(values)
    index = max(0, math.ceil((percentage / 100) * len(ordered)) - 1)
    return ordered[index]


async def run_request(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    url: str,
    client_id: str,
) -> tuple[str, float]:
    async with semaphore:
        started_at = time.perf_counter()
        try:
            response = await client.get(url, headers={"X-Client-ID": client_id})
            outcome = str(response.status_code)
        except httpx.HTTPError as error:
            outcome = f"error:{type(error).__name__}"
        return outcome, time.perf_counter() - started_at


async def run_load_test(args: argparse.Namespace) -> dict[str, object]:
    run_id = f"load-{uuid.uuid4().hex[:10]}"
    semaphore = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(
        max_connections=args.concurrency,
        max_keepalive_connections=args.concurrency,
    )

    started_at = time.perf_counter()
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as client:
        tasks = []
        for index in range(args.requests):
            client_id = run_id if args.client_mode == "shared" else f"{run_id}-{index}"
            tasks.append(run_request(client, semaphore, args.url, client_id))
        results = await asyncio.gather(*tasks)
    total_duration = time.perf_counter() - started_at

    status_counts = Counter(outcome for outcome, _ in results)
    latency_ms = [duration * 1000 for _, duration in results]
    return {
        "url": args.url,
        "client_mode": args.client_mode,
        "shared_client_id": run_id if args.client_mode == "shared" else None,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "status_counts": dict(sorted(status_counts.items())),
        "total_seconds": round(total_duration, 3),
        "requests_per_second": round(args.requests / total_duration, 2),
        "latency_ms": {
            "p50": round(percentile(latency_ms, 50), 3),
            "p95": round(percentile(latency_ms, 95), 3),
            "p99": round(percentile(latency_ms, 99), 3),
            "max": round(max(latency_ms), 3),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="http://localhost:8000/demo-sliding",
    )
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument(
        "--client-mode",
        choices=("shared", "unique"),
        default="shared",
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1:
        parser.error("--requests and --concurrency must be positive")
    return args


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_load_test(parse_args())), indent=2))
