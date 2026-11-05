Implement `count_primes(n)` in `primes.py`: return how many primes are <= n.

`python3 test_primes.py` checks correctness and `python3 bench.py` measures latency for n = 2,000,000. Both must keep working as they are; don't edit them.

There are two reasonable ways to do this and it isn't obvious up front which one wins on this machine: (1) trial division by odd numbers up to sqrt(k), and (2) a sieve of Eratosthenes over a bytearray. Rather than picking one, use the fork tool to try both, one per branch, then finish your branch.
