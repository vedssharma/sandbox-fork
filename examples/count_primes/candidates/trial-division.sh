# Trial division by 2 and odd numbers up to isqrt(k).
cat > primes.py <<'PY'
"""Prime counting."""

from math import isqrt


def is_prime(k: int) -> bool:
    if k < 2:
        return False
    if k % 2 == 0:
        return k == 2
    for d in range(3, isqrt(k) + 1, 2):
        if k % d == 0:
            return False
    return True


def count_primes(n: int) -> int:
    """Return the number of primes p with p <= n. Must handle any int n (n < 2 gives 0)."""
    return sum(1 for k in range(2, n + 1) if is_prime(k))
PY
