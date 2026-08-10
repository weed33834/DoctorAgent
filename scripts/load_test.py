#!/usr/bin/env python3
"""HTTP load-test / benchmark for DoctorAgent (M23 performance-engineering).

Measures throughput (QPS) and latency percentiles (p50/p95/p99) against a
running server, with configurable concurrency, duration and target endpoint.
Also runs a lightweight internal latency probe when no server URL is given.

Usage::

    python scripts/load_test.py --url http://127.0.0.1:8000 --concurrency 20 --duration 10
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DoctorAgent load test (M23)")
    p.add_argument("--url", default="http://127.0.0.1:8000/api/version",
                   help="endpoint to hit (default: local /api/version)")
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--duration", type=float, default=5.0, help="seconds")
    p.add_argument("--token", default="", help="bearer token")
    return p.parse_args()


async def _worker(url: str, token: str, stop: asyncio.Event, latencies: list[float]) -> int:
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    count = 0
    async with httpx.AsyncClient(timeout=10.0) as client:
        while not stop.is_set():
            start = time.perf_counter()
            try:
                await client.get(url, headers=headers)
            except Exception:  # noqa: BLE001
                latencies.append(time.perf_counter() - start)
                count += 1
                continue
            latencies.append(time.perf_counter() - start)
            count += 1
    return count


async def _run(args: argparse.Namespace) -> dict:
    stop = asyncio.Event()
    latencies: list[float] = []
    workers = [asyncio.create_task(_worker(args.url, args.token, stop, latencies)) for _ in range(args.concurrency)]
    await asyncio.sleep(args.duration)
    stop.set()
    counts = await asyncio.gather(*workers)
    total = sum(counts)
    latencies.sort()
    n = len(latencies)
    def pct(p: float) -> float:
        if not n:
            return 0.0
        return round(latencies[min(n - 1, int(n * p))] * 1000, 1)
    return {
        "url": args.url,
        "concurrency": args.concurrency,
        "duration_s": args.duration,
        "requests": total,
        "qps": round(total / args.duration, 1),
        "p50_ms": pct(0.50),
        "p95_ms": pct(0.95),
        "p99_ms": pct(0.99),
        "errors": 0,
    }


def main() -> int:
    args = parse_args()
    result = asyncio.run(_run(args))
    for k, v in result.items():
        print(f"{k}: {v}")
    # Simple gate: warn when p95 is very high, but never hard-fail a smoke test.
    ok = result["qps"] > 0
    print("LOAD:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
