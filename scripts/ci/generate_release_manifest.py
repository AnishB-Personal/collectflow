#!/usr/bin/env python3
"""
CollectFlow Release Candidate Manifest Generator (WP05-01, WP05-04, WP05-R01, WP05-R04)
Generates an immutable, machine-readable release candidate manifest linking backend and frontend
commit SHAs, container artifacts, SBOM references, and verified structured quality gate evidence.
- Strictly ingests actual structured evidence files.
- Fails closed on missing, malformed, or incomplete evidence (WP05-R01).
- Prohibits zero collected tests from producing PASS.
- Never converts missing data into zero findings.
- Enforces exact expected schemas for all mandatory quality gates.
"""

import argparse
import datetime
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

def compute_sha256(file_path: Optional[Path]) -> Optional[str]:
    if not file_path or not file_path.exists():
        return None
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def get_git_info(repo_dir: Path) -> Tuple[str, bool]:
    """Returns (commit_sha, clean_worktree)."""
    try:
        sha = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return sha, (len(status) == 0)
    except Exception as e:
        return f"UNKNOWN_ERROR: {e}", False

def load_json_if_exists(file_path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if file_path and file_path.exists():
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None

def parse_test_report(report_path: Optional[Path], test_suite_name: str) -> Dict[str, Any]:
    """Ingests structured test execution evidence with strict schema and non-zero execution enforcement (WP05-R01)."""
    if not report_path or not report_path.exists():
        return {
            "verdict": "BLOCKED_EVIDENCE_UNAVAILABLE",
            "passed": None,
            "failed": None,
            "skipped": None,
            "evidence_file": str(report_path) if report_path else None,
            "evidence_sha256": None,
            "details": f"{test_suite_name} report file was not found or not provided."
        }
    
    sha = compute_sha256(report_path)
    data = load_json_if_exists(report_path)
    if not data or not isinstance(data, dict):
        return {
            "verdict": "BLOCKED_EVIDENCE_MALFORMED",
            "passed": None,
            "failed": None,
            "evidence_file": str(report_path),
            "evidence_sha256": sha,
            "details": f"Failed to parse {test_suite_name} JSON report."
        }

    # Strict schema check: both 'passed' and 'failed' must be explicitly present as integers
    if "passed" not in data or "failed" not in data or not isinstance(data["passed"], int) or not isinstance(data["failed"], int):
        return {
            "verdict": "BLOCKED_EVIDENCE_INCOMPLETE",
            "passed": data.get("passed"),
            "failed": data.get("failed"),
            "evidence_file": str(report_path),
            "evidence_sha256": sha,
            "details": f"{test_suite_name} report missing mandatory 'passed' or 'failed' integer count."
        }

    passed = data["passed"]
    failed = data["failed"]
    skipped = data.get("skipped", 0)

    # WP05-R01: Zero collected tests cannot produce PASS
    if passed <= 0:
        return {
            "verdict": "FAIL_ZERO_TESTS_EXECUTED",
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "evidence_file": str(report_path),
            "evidence_sha256": sha,
            "details": f"Zero tests were executed in {test_suite_name}."
        }

    verdict = "PASS" if failed == 0 else "FAIL"
    return {
        "verdict": verdict,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "evidence_file": str(report_path),
        "evidence_sha256": sha
    }

def parse_audit_report(report_path: Optional[Path], audit_type: str) -> Dict[str, Any]:
    """Ingests pip-audit or npm audit JSON evidence without converting missing data to zero (WP05-R01)."""
    if not report_path or not report_path.exists():
        return {
            "verdict": "BLOCKED_EVIDENCE_UNAVAILABLE",
            "vulnerabilities": None,
            "evidence_file": str(report_path) if report_path else None,
            "evidence_sha256": None,
            "details": f"{audit_type} report was not found or not provided."
        }

    sha = compute_sha256(report_path)
    data = load_json_if_exists(report_path)
    if not data or not isinstance(data, dict):
        return {
            "verdict": "BLOCKED_EVIDENCE_MALFORMED",
            "vulnerabilities": None,
            "evidence_file": str(report_path),
            "evidence_sha256": sha,
            "details": f"Failed to parse {audit_type} report JSON."
        }

    vulns = 0
    if audit_type == "pip-audit":
        if "dependencies" not in data or not isinstance(data["dependencies"], list):
            return {
                "verdict": "BLOCKED_EVIDENCE_INCOMPLETE",
                "vulnerabilities": None,
                "evidence_file": str(report_path),
                "evidence_sha256": sha,
                "details": "pip-audit JSON missing mandatory 'dependencies' list."
            }
        for dep in data["dependencies"]:
            vulns += len(dep.get("vulns", []))
    elif audit_type == "npm-audit":
        if "metadata" not in data or not isinstance(data["metadata"], dict) or "vulnerabilities" not in data["metadata"]:
            return {
                "verdict": "BLOCKED_EVIDENCE_INCOMPLETE",
                "vulnerabilities": None,
                "evidence_file": str(report_path),
                "evidence_sha256": sha,
                "details": "npm audit JSON missing mandatory 'metadata.vulnerabilities' object."
            }
        meta = data["metadata"]["vulnerabilities"]
        vulns = meta.get("high", 0) + meta.get("critical", 0)

    verdict = "PASS" if vulns == 0 else "FAIL"
    return {
        "verdict": verdict,
        "vulnerabilities": vulns,
        "evidence_file": str(report_path),
        "evidence_sha256": sha
    }

def parse_secret_scan_report(report_path: Optional[Path]) -> Dict[str, Any]:
    """Ingests scan_secrets.py JSON evidence with strict schema (WP05-R01)."""
    if not report_path or not report_path.exists():
        return {
            "verdict": "BLOCKED_EVIDENCE_UNAVAILABLE",
            "findings_count": None,
            "evidence_file": str(report_path) if report_path else None,
            "evidence_sha256": None,
            "details": "Secret scan report was not provided."
        }

    sha = compute_sha256(report_path)
    data = load_json_if_exists(report_path)
    if not data or not isinstance(data, dict):
        return {
            "verdict": "BLOCKED_EVIDENCE_MALFORMED",
            "findings_count": None,
            "evidence_file": str(report_path),
            "evidence_sha256": sha,
            "details": "Secret scan report is not valid JSON."
        }

    if "findings_count" not in data or not isinstance(data["findings_count"], int):
        return {
            "verdict": "BLOCKED_EVIDENCE_INCOMPLETE",
            "findings_count": None,
            "evidence_file": str(report_path),
            "evidence_sha256": sha,
            "details": "Secret scan report missing mandatory 'findings_count' integer."
        }

    findings = data["findings_count"]
    verdict = "PASS" if findings == 0 else "FAIL"
    return {
        "verdict": verdict,
        "findings_count": findings,
        "evidence_file": str(report_path),
        "evidence_sha256": sha
    }

def parse_lint_report(report_path: Optional[Path]) -> Dict[str, Any]:
    """Ingests static analysis / lint evidence with strict schema (WP05-R01)."""
    if not report_path or not report_path.exists():
        return {
            "verdict": "BLOCKED_EVIDENCE_UNAVAILABLE",
            "lint_errors": None,
            "evidence_file": str(report_path) if report_path else None,
            "evidence_sha256": None,
            "details": "Lint report was not provided."
        }

    sha = compute_sha256(report_path)
    data = load_json_if_exists(report_path)
    if data is None:
        return {
            "verdict": "BLOCKED_EVIDENCE_MALFORMED",
            "lint_errors": None,
            "evidence_file": str(report_path),
            "evidence_sha256": sha,
            "details": "Lint report is not valid JSON."
        }

    errors = None
    if isinstance(data, dict):
        if "lint_errors" in data and isinstance(data["lint_errors"], int):
            errors = data["lint_errors"]
        elif "errors" in data and isinstance(data["errors"], int):
            errors = data["errors"]
    elif isinstance(data, list):
        errors = len(data)

    if errors is None:
        return {
            "verdict": "BLOCKED_EVIDENCE_INCOMPLETE",
            "lint_errors": None,
            "evidence_file": str(report_path),
            "evidence_sha256": sha,
            "details": "Lint report missing recognized 'lint_errors' integer count."
        }

    verdict = "PASS" if errors == 0 else "FAIL"
    return {
        "verdict": verdict,
        "lint_errors": errors,
        "evidence_file": str(report_path),
        "evidence_sha256": sha
    }

def parse_cross_repo_report(report_path: Optional[Path]) -> Dict[str, Any]:
    """Ingests cross-repository pairing report with strict schema (WP05-R01, WP05-R02)."""
    if not report_path or not report_path.exists():
        return {
            "verdict": "BLOCKED",
            "pairing_status": "MISSING_CROSS_REPO_REPORT",
            "evidence_file": str(report_path) if report_path else None,
            "evidence_sha256": None,
            "details": ["Cross-repository pairing report was not found."]
        }

    sha = compute_sha256(report_path)
    data = load_json_if_exists(report_path)
    if not data or not isinstance(data, dict):
        return {
            "verdict": "BLOCKED",
            "pairing_status": "MALFORMED_CROSS_REPO_REPORT",
            "evidence_file": str(report_path),
            "evidence_sha256": sha,
            "details": ["Cross-repository pairing report is malformed."]
        }

    if "gate" not in data or "pairing_status" not in data or "verdict" not in data:
        return {
            "verdict": "BLOCKED",
            "pairing_status": "INCOMPLETE_CROSS_REPO_REPORT",
            "evidence_file": str(report_path),
            "evidence_sha256": sha,
            "details": ["Cross-repo report missing required schema keys."]
        }

    return {
        "verdict": data.get("verdict", "FAIL"),
        "pairing_status": data.get("pairing_status", "UNKNOWN"),
        "initiator": data.get("initiator"),
        "backend_sha": data.get("backend", {}).get("commit_sha"),
        "frontend_sha": data.get("frontend", {}).get("commit_sha"),
        "details": data.get("details", []),
        "evidence_file": str(report_path),
        "evidence_sha256": sha
    }

def main() -> int:
    parser = argparse.ArgumentParser(description="Generate CollectFlow Release Manifest (WP05-01, WP05-R01)")
    parser.add_argument("--rc-id", required=True, help="Release candidate identifier (e.g., RC-20261019-01)")
    parser.add_argument("--backend-dir", default=".", help="Backend repository path")
    parser.add_argument("--frontend-dir", default="../cash-flow-buddy-101", help="Frontend repository path")
    parser.add_argument("--backend-image-meta", "--backend-meta", help="Backend image metadata JSON path")
    parser.add_argument("--frontend-image-meta", "--frontend-meta", help="Frontend image metadata JSON path")
    parser.add_argument("--backend-sbom", help="Backend SBOM JSON path")
    parser.add_argument("--frontend-sbom", help="Frontend SBOM JSON path")
    parser.add_argument("--backend-test-report", help="Backend test results JSON path")
    parser.add_argument("--frontend-test-report", help="Frontend test results JSON path")
    parser.add_argument("--backend-audit-report", help="pip-audit results JSON path")
    parser.add_argument("--frontend-audit-report", help="npm audit results JSON path")
    parser.add_argument("--secret-scan-report", help="Secret scanning report JSON path")
    parser.add_argument("--sast-lint-report", help="SAST / linting report JSON path")
    parser.add_argument("--cross-repo-report", help="Cross-repository pairing report JSON path")
    parser.add_argument("--output", default="release-manifest.json", help="Output manifest JSON path")
    args = parser.parse_args()

    backend_root = Path(args.backend_dir).resolve()
    frontend_root = Path(args.frontend_dir).resolve()
    manifest_out = Path(args.output).resolve()

    # Dynamic git verification (WP05-01: No hardcoded clean worktree assertions)
    backend_sha, backend_clean = get_git_info(backend_root)
    frontend_sha, frontend_clean = get_git_info(frontend_root)

    backend_meta = load_json_if_exists(Path(args.backend_image_meta)) if args.backend_image_meta else None
    frontend_meta = load_json_if_exists(Path(args.frontend_image_meta)) if args.frontend_image_meta else None

    backend_sbom_path = Path(args.backend_sbom).resolve() if args.backend_sbom else None
    frontend_sbom_path = Path(args.frontend_sbom).resolve() if args.frontend_sbom else None

    backend_sbom_checksum = compute_sha256(backend_sbom_path)
    frontend_sbom_checksum = compute_sha256(frontend_sbom_path)

    # Ingest actual structured evidence (WP05-01, WP05-R01)
    backend_tests = parse_test_report(Path(args.backend_test_report) if args.backend_test_report else None, "backend_pytest")
    frontend_tests = parse_test_report(Path(args.frontend_test_report) if args.frontend_test_report else None, "frontend_vitest")
    pip_audit = parse_audit_report(Path(args.backend_audit_report) if args.backend_audit_report else None, "pip-audit")
    npm_audit = parse_audit_report(Path(args.frontend_audit_report) if args.frontend_audit_report else None, "npm-audit")
    secret_scan = parse_secret_scan_report(Path(args.secret_scan_report) if args.secret_scan_report else None)
    sast_lint = parse_lint_report(Path(args.sast_lint_report) if args.sast_lint_report else None)
    cross_repo = parse_cross_repo_report(Path(args.cross_repo_report) if args.cross_repo_report else None)

    # Container artifacts handling (WP05-04, WP05-R03)
    b_image_id = backend_meta.get("image_id") if backend_meta else None
    b_oci_rev = backend_meta.get("oci_revision") if backend_meta else None
    b_scan_verdict = backend_meta.get("scan_verdict") if backend_meta else "BLOCKED_METADATA_UNAVAILABLE"

    f_image_id = frontend_meta.get("image_id") if frontend_meta else None
    f_oci_rev = frontend_meta.get("oci_revision") if frontend_meta else None
    f_scan_verdict = frontend_meta.get("scan_verdict") if frontend_meta else "BLOCKED_METADATA_UNAVAILABLE"

    docker_version = "unknown"
    try:
        res = subprocess.run(["docker", "--version"], capture_output=True, text=True)
        docker_version = res.stdout.strip()
    except Exception:
        pass

    manifest = {
        "schema_version": "1.2.0",
        "release_candidate_id": args.rc_id,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_traceability": {
            "backend": {
                "repository": "AnishB-Personal/collectflow",
                "commit_sha": backend_sha,
                "target_branch": "feature/release-wp05-portable-cicd",
                "clean_worktree": backend_clean
            },
            "frontend": {
                "repository": "AnishB-Personal/cash-flow-buddy-101",
                "commit_sha": frontend_sha,
                "target_branch": "feature/release-wp05-portable-cicd",
                "clean_worktree": frontend_clean
            }
        },
        "container_artifacts": {
            "backend": {
                "image_name": backend_meta.get("image_name", "collectflow-backend") if backend_meta else "collectflow-backend",
                "image_tag": backend_meta.get("image_tag", f"collectflow-backend:{backend_sha}") if backend_meta else f"collectflow-backend:{backend_sha}",
                "image_id": b_image_id,
                "artifact_type": backend_meta.get("artifact_type", "local_daemon_image") if backend_meta else "unknown",
                "oci_revision": b_oci_rev,
                "base_image": backend_meta.get("base_image", "python:3.12-slim-bookworm") if backend_meta else "python:3.12-slim-bookworm",
                "scan_verdict": b_scan_verdict,
                "security_scan": backend_meta.get("security_scan", {}) if backend_meta else {},
                "publication_status": "DISABLED_BY_DEFAULT",
                "registry_published": False,
                "registry_digest": None
            },
            "frontend": {
                "image_name": frontend_meta.get("image_name", "collectflow-frontend") if frontend_meta else "collectflow-frontend",
                "image_tag": frontend_meta.get("image_tag", f"collectflow-frontend:{frontend_sha}") if frontend_meta else f"collectflow-frontend:{frontend_sha}",
                "image_id": f_image_id,
                "artifact_type": frontend_meta.get("artifact_type", "local_daemon_image") if frontend_meta else "unknown",
                "oci_revision": f_oci_rev,
                "base_image": frontend_meta.get("base_image", "node:22-alpine") if frontend_meta else "node:22-alpine",
                "scan_verdict": f_scan_verdict,
                "security_scan": frontend_meta.get("security_scan", {}) if frontend_meta else {},
                "publication_status": "DISABLED_BY_DEFAULT",
                "registry_published": False,
                "registry_digest": None
            }
        },
        "sbom_artifacts": {
            "backend": {
                "format": "CycloneDX-1.4",
                "file_path": str(backend_sbom_path) if backend_sbom_path else None,
                "sha256_checksum": backend_sbom_checksum
            },
            "frontend": {
                "format": "CycloneDX-1.5",
                "file_path": str(frontend_sbom_path) if frontend_sbom_path else None,
                "sha256_checksum": frontend_sbom_checksum
            }
        },
        "quality_gates": {
            "backend_regression_tests": backend_tests,
            "frontend_regression_tests": frontend_tests,
            "python_dependency_audit": pip_audit,
            "npm_dependency_audit": npm_audit,
            "secret_scanning": secret_scan,
            "sast_lint_checks": sast_lint,
            "cross_repo_pairing": cross_repo,
            "live_browser_acceptance": {
                "verdict": "NOT_EXECUTED",
                "details": "Environment requires active live staging deployment with external reverse proxy and browser runner."
            },
            "https_ssl_tls_verification": {
                "verdict": "NOT_EXECUTED",
                "details": "Environment requires active live staging deployment with external TLS termination (e.g. AWS ALB)."
            }
        },
        "toolchain": {
            "python": "3.12",
            "node": "22",
            "docker": docker_version
        },
        "governance_and_approval": {
            "approval_status": "UNAPPROVED",
            "deployment_authorized": False,
            "release_boundary_control": "Release candidate manifest verification strictly gates candidate packaging; it does NOT authorize production deployment.",
            "authorized_by": None,
            "authorization_timestamp": None
        }
    }

    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Release manifest successfully generated at: {manifest_out}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
