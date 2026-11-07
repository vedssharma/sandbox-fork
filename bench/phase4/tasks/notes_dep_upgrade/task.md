Upgrade our notes service from tinykv 1.4.2 to tinykv 2.0.0.

1.4.2 has a security advisory (path traversal), and 2.0.0 is the fixed release. This machine has no package index access; the 2.0.0 wheel and its changelog are in `/opt/wheels`, so install it with `pip install --no-index --find-links /opt/wheels tinykv==2.0.0`.

- Update `app/notes.py` for 2.0's API, keeping its functions' behaviour the same, and pin `tinykv==2.0.0` in `requirements.txt`.
- The note store at `/var/lib/notes/store` is a copy of production data. When you finish, every note must read back exactly as it did before the upgrade.
- `python3 tests/test_notes.py` must pass. Don't edit the tests.
