"""Latency benchmark. Prints one JSON line: {"ms": float, "n": int}. Exits 1 on a wrong answer."""

import json
import os
import sys
import time

from primes import count_primes

N = int(os.environ.get("SFORK_BENCH_N", "2000000"))
EXPECTED = {2_000_000: 148933, 50_000: 5133, 10_000: 1229}

t0 = time.perf_counter()
got = count_primes(N)
ms = (time.perf_counter() - t0) * 1000
if N in EXPECTED and got != EXPECTED[N]:
    print(f"wrong answer for n={N}: {got}", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"ms": round(ms, 3), "n": N}))
