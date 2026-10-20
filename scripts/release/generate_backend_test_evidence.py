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


BASELINE_MIN_TOTAL_TESTS = 374
BASELINE_MIN_PASSED_TESTS = 303
BASELINE_MAX_SKIPS = 71

MANDATORY_FINANCIAL_SUITES = [
    "tests.test_wp06_financial_governance_and_integrity",
    "tests.test_stage4c3_orders_invoices",
    "tests.test_stage4c4_payments_adjustments",
    "tests.test_stage4c5_reporting_targets_exports",
    "tests.test_postgres_domain_models",
    "tests.contracts.test_domain_contracts",
]

MANDATORY_FINANCIAL_SUITE_MINIMUMS = {
    "tests.test_wp06_financial_governance_and_integrity": 29,
    "tests.test_stage4c3_orders_invoices": 11,
    "tests.test_stage4c4_payments_adjustments": 23,
    "tests.test_stage4c5_reporting_targets_exports": 18,
    "tests.test_postgres_domain_models": 6,
    "tests.contracts.test_domain_contracts": 3,
}


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
    xml_timestamp = None
    xml_hostname = None
    xml_time_sec = 0.0
    discovered_classes = set()
    suite_testcases = {}

    for suite in suites:
        total_tests += int(suite.attrib.get("tests", 0))
        total_failures += int(suite.attrib.get("failures", 0))
        total_errors += int(suite.attrib.get("errors", 0))
        total_skipped += int(suite.attrib.get("skipped", 0))
        if not xml_timestamp and suite.attrib.get("timestamp"):
            xml_timestamp = suite.attrib.get("timestamp")
        if not xml_hostname and suite.attrib.get("hostname"):
            xml_hostname = suite.attrib.get("hostname")
        if suite.attrib.get("time"):
            try:
                xml_time_sec += float(suite.attrib.get("time"))
            except ValueError:
                pass

        for tc in suite.findall("testcase"):
            cn = tc.attrib.get("classname")
            name = tc.attrib.get("name")
            if cn:
                discovered_classes.add(cn)
                suite_testcases.setdefault(cn, []).append(name)

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

    missing_suites = [s for s in MANDATORY_FINANCIAL_SUITE_MINIMUMS if s not in discovered_classes]
    insufficient_suites = [
        f"{s} (has {len(suite_testcases.get(s, []))}, expected at least {min_c})"
        for s, min_c in MANDATORY_FINANCIAL_SUITE_MINIMUMS.items()
        if len(suite_testcases.get(s, [])) < min_c
    ]

    is_qualifying = (
        total_failures == 0
        and total_errors == 0
        and total_tests >= BASELINE_MIN_TOTAL_TESTS
        and passed >= BASELINE_MIN_PASSED_TESTS
        and total_skipped <= BASELINE_MAX_SKIPS
        and not missing_suites
        and not insufficient_suites
    )
    status = "PASS" if is_qualifying else "FAIL"
    xml_sha256 = compute_file_sha256(xml_path)

    evidence = {
        "test_suite": "backend_pytest_regression",
        "backend_sha": backend_sha,
        "execution_timestamp_utc": xml_timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "hostname": xml_hostname or "unknown",
        "duration_seconds": round(xml_time_sec, 3),
        "status": status,
        "passed": passed,
        "failed": total_failures,
        "skipped": total_skipped,
        "summary": {
            "total": total_tests,
            "passed": passed,
            "failed": total_failures,
            "errors": total_errors,
            "skipped": total_skipped,
        },
        "mandatory_suites_present": not missing_suites and not insufficient_suites,
        "missing_mandatory_suites": missing_suites,
        "insufficient_mandatory_suites": insufficient_suites,
        "test_inventory_summary": {s: len(suite_testcases.get(s, [])) for s in MANDATORY_FINANCIAL_SUITE_MINIMUMS},
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
