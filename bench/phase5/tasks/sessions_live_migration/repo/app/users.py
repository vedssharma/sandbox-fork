"""User ids.

Canonical ids look like "U-000123" and map to the integer 123. Accounts imported from the old forum
keep their legacy "L-" ids in some places; they were renumbered on import to LEGACY_OFFSET + n, so
"L-000123" is user 500123 (a different person from user 123).
"""

LEGACY_OFFSET = 500_000


def parse_user_id(value: "str | int") -> int:
    if isinstance(value, int):
        return value
    prefix, _, num = value.partition("-")
    if prefix == "U":
        return int(num)
    if prefix == "L":
        return LEGACY_OFFSET + int(num)
    raise ValueError(f"not a user id: {value!r}")
