#!/usr/bin/env python3
"""
Generate backend PostgreSQL regression test evidence for WP-06 release readiness verification.
Parses artifacts/backend_pytest_report.xml and writes artifacts/backend_test_evidence.json.
"""

import argparse
import datetime
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def generate_backend_test_evidence(
    xml_path: Path,
    output_json: Path,
    repo_path: Path,
) -> dict:
    if not xml_path.is_file():
        raise FileNotFoundError(f"Pytest XML report not found at: {xml_path}")

    tree = ET.parse(xml_path)
    root = tree.getroot()

    # <testsuites> or <testsuite>
    if root.tag == "testsuites":
        suites = root.findall("testsuite")
    elif root.tag == "testsuite":
        suites = [root]
    else:
        raise ValueError(f"Unrecognized XML root element: {root.tag}")

    total_tests = 0
    total_failures = 0
    total_errors = 0
    total_skipped = 0

    for suite in suites:
        total_tests += int(suite.attrib.get("tests", 0))
        total_failures += int(suite.attrib.get("failures", 0))
        total_errors += int(suite.attrib.get("errors", 0))
        total_skipped += int(suite.attrib.get("skipped", 0))

    passed = total_tests - total_failures - total_errors - total_skipped
    if passed < 0:
        passed = 0

    # Resolve git commit SHA
    res = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    backend_sha = res.stdout.strip()

    status = "PASS" if total_failures == 0 and total_errors == 0 and passed > 0 else "FAIL"
    xml_sha256 = compute_file_sha256(xml_path)

    evidence = {
        "test_suite": "backend_pytest_regression",
        "backend_sha": backend_sha,
        "execution_timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": status,
        "summary": {
            "total": total_tests,
            "passed": passed,
            "failed": total_failures,
            "errors": total_errors,
            "skipped": total_skipped,
        },
        "xml_report_sha256": xml_sha256,
        "report_xml": str(xml_path.resolve()),
    }

    output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(evidence, f, indent=2)

    print(f"Generated backend test evidence: {output_json}")
    print(f"Status: {status} | Total: {total_tests} | Passed: {passed} | Failed: {total_failures} | Errors: {total_errors} | Skipped: {total_skipped}")
    return evidence


def main():
    parser = argparse.ArgumentParser(description="Generate Backend Pytest Evidence JSON")
    parser.add_argument(
        "--xml",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent / "artifacts" / "backend_pytest_report.xml",
        help="Path to backend pytest report XML",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent / "artifacts" / "backend_test_evidence.json",
        help="Path to output test evidence JSON",
    )
    parser.add_argument(
        "--repo-path",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent,
        help="Path to backend git repository",
    )
    args = parser.parse_args()

    evidence = generate_backend_test_evidence(args.xml, args.output, args.repo_path)
    if evidence["status"] == "PASS":
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
