"""Require real, nonempty, successful and unskipped integration tests."""

import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
cases = list(root.iter("testcase"))
if not cases or any(
    list(case.iter(tag)) for case in cases for tag in ("failure", "error", "skipped")
):
    raise SystemExit("Integration report failed: no tests, failure, error, or skip")
print(f"Integration report passed: {len(cases)} tests, zero failures/errors/skips")
