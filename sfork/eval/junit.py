"""Parse JUnit XML (pytest --junitxml, go-junit-report, surefire, jest-junit, ...) into per-test results."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass(frozen=True)
class CaseResult:
    name: str  # "classname::name", unique within a report
    status: str  # passed | failed | error | skipped
    seconds: float = 0.0
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "passed"


class JUnitError(ValueError):
    pass


def parse_junit(xml_text: str) -> list[CaseResult]:
    """All <testcase> elements under <testsuites>/<testsuite>, in document order.

    Duplicate names (parametrized cases reported twice, repeated runs) get a `#n` suffix so they
    stay distinct. Raises JUnitError on malformed XML or a document with no testcases element at all.
    """
    if not xml_text.strip():
        raise JUnitError("empty junit report")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise JUnitError(f"malformed junit xml: {e}") from e
    if root.tag not in ("testsuites", "testsuite"):
        raise JUnitError(f"unexpected root element <{root.tag}>")
    out: list[CaseResult] = []
    seen: dict[str, int] = {}
    for case in root.iter("testcase"):
        cls, nm = case.get("classname") or "", case.get("name") or "?"
        name = f"{cls}::{nm}" if cls else nm
        if name in seen:
            seen[name] += 1
            name = f"{name}#{seen[name]}"
        else:
            seen[name] = 1
        status, message = "passed", ""
        for child in case:
            tag = child.tag
            if tag in ("failure", "error", "skipped"):
                status = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                message = (child.get("message") or child.text or "").strip()[:500]
                break
        try:
            secs = float(case.get("time") or 0)
        except ValueError:
            secs = 0.0
        out.append(CaseResult(name=name, status=status, seconds=secs, message=message))
    return out


def counts(cases: list[CaseResult]) -> tuple[int, int]:
    """(passed, total), with skipped tests left out of the total."""
    ran = [c for c in cases if c.status != "skipped"]
    return sum(c.ok for c in ran), len(ran)


__all__ = ["CaseResult", "JUnitError", "counts", "parse_junit"]
