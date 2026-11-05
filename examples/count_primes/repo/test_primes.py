"""Correctness tests for count_primes. Prints one JSON line with "passed", "total" and "failures"."""

import json
import sys
import traceback

CASES = [
    (-5, 0),
    (0, 0),
    (1, 0),
    (2, 1),
    (3, 2),
    (4, 2),
    (10, 4),
    (29, 10),
    (30, 10),
    (97, 25),
    (100, 25),
    (1000, 168),
    (7919, 1000),
    (10**5, 9592),
    (10**6, 78498),
]


def main() -> None:
    failures = []
    try:
        from primes import count_primes
    except Exception:
        print(json.dumps({"passed": 0, "total": len(CASES), "failures": [traceback.format_exc(limit=1)]}))
        sys.exit(1)
    passed = 0
    for n, want in CASES:
        try:
            got = count_primes(n)
        except Exception as e:
            failures.append(f"count_primes({n}) raised {type(e).__name__}: {e}")
            continue
        if got == want:
            passed += 1
        else:
            failures.append(f"count_primes({n}) = {got!r}, want {want}")
    print(json.dumps({"passed": passed, "total": len(CASES), "failures": failures}))
    sys.exit(0 if passed == len(CASES) else 1)


if __name__ == "__main__":
    main()
