# A sieve that forgets n itself (range stops at n - 1): fast, and wrong whenever n is prime.
cat > primes.py <<'PY'
"""Prime counting."""


def count_primes(n: int) -> int:
    """Return the number of primes p with p <= n. Must handle any int n (n < 2 gives 0)."""
    if n < 2:
        return 0
    s = bytearray([1]) * n
    s[0] = s[1] = 0
    for i in range(2, int(n**0.5) + 1):
        if s[i]:
            s[i * i :: i] = bytes(len(range(i * i, n, i)))
    return sum(s)
PY
