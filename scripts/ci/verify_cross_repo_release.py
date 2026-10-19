#!/usr/bin/env python3
"""
CollectFlow Cross-Repository Integration Gate (WP05-03, WP05-07)
Explicitly pairs and verifies immutable commit SHAs across backend and frontend repositories.
- Verifies exact expected commit SHAs.
- Validates repository identity and remote origin pairing.
- Enforces clean working trees for both repositories.
- In single-repository CI runners without cross-repo credentials, safely records BLOCKED without failing the runner.
- Enforces that BLOCKED cannot satisfy release-candidate acceptance during manifest validation.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

def get_git_info(repo_dir: Path) -> Tuple[Optional[str], Optional[str], bool, Optional[str]]:
    """Returns (commit_sha, branch_name, is_clean, remote_url)."""
    if not repo_dir.exists() or not (repo_dir / ".git").exists():
        return None, None, False, None
    try:
        sha = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        ).stdout.strip()

        branch = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        ).stdout.strip()

        status = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True
        ).stdout.strip()

        remote_url_res = subprocess.run(
            ["git", "-C", str(repo_dir), "remote", "get-url", "origin"],
            capture_output=True,
            text=True
        )
        remote_url = remote_url_res.stdout.strip() if remote_url_res.returncode == 0 else ""

        return sha, branch, (len(status) == 0), remote_url
    except Exception:
        return None, None, False, None

def main() -> int:
    parser = argparse.ArgumentParser(description="CollectFlow Cross-Repository Integration Gate")
    parser.add_argument("--initiator", choices=["backend", "frontend"], default="backend", help="Initiating repo")
    parser.add_argument("--backend-path", default=".", help="Path to backend repository")
    parser.add_argument("--frontend-path", default="../cash-flow-buddy-101", help="Path to frontend repository")
    parser.add_argument("--expected-backend-sha", help="Expected backend commit SHA")
    parser.add_argument("--expected-frontend-sha", help="Expected frontend commit SHA")
    parser.add_argument("--report", default="/tmp/cross-repo-integration-report.json", help="Output report JSON")
    args = parser.parse_args()

    backend_dir = Path(args.backend_path).resolve()
    frontend_dir = Path(args.frontend_path).resolve()
    report_path = Path(args.report).resolve()

    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()

    b_sha, b_branch, b_clean, b_remote = get_git_info(backend_dir)
    f_sha, f_branch, f_clean, f_remote = get_git_info(frontend_dir)

    report: Dict[str, Any] = {
        "gate": "WP05-03_CROSS_REPO_INTEGRATION",
        "timestamp": timestamp,
        "initiator": args.initiator,
        "execution_context": "CI_RUNNER" if in_ci else "LOCAL_RUNNER",
        "backend": {
            "path": str(backend_dir),
            "accessible": b_sha is not None,
            "commit_sha": b_sha,
            "branch": b_branch,
            "clean_worktree": b_clean,
            "remote_url": b_remote
        },
        "frontend": {
            "path": str(frontend_dir),
            "accessible": f_sha is not None,
            "commit_sha": f_sha,
            "branch": f_branch,
            "clean_worktree": f_clean,
            "remote_url": f_remote
        },
        "pairing_status": "UNKNOWN",
        "verdict": "FAIL",
        "details": []
    }

    # 1. Handle single-repository CI barrier gracefully without masking error (WP05-03)
    # In CI, if the sibling repo checkout is not present, record BLOCKED.
    # Note: BLOCKED exits 0 in CI so artifact upload can proceed, but manifest validation blocks RC acceptance.
    if in_ci and (b_sha is None or f_sha is None):
        missing_repo = "frontend" if f_sha is None else "backend"
        report["pairing_status"] = "BLOCKED_PENDING_CROSS_REPO_CREDENTIALS"
        report["verdict"] = "BLOCKED"
        report["details"].append(
            f"Single-repository CI runner cannot access private sibling repository '{missing_repo}' "
            "with default single-repository GITHUB_TOKEN. Access is intentionally restricted to avoid "
            "requesting overly broad organization admin tokens. Use local dual-checkout workflow or "
            "provide a repository-scoped deploy key to unblock full release-manifest verification."
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"[CROSS-REPO INTEGRATION] {report['pairing_status']}: {report['details'][0]}")
        return 0

    # 2. Local Dual-Checkout & Full Gate Validation
    if b_sha is None:
        report["details"].append(f"Backend repository not found or not a valid git repository at {backend_dir}")
    if f_sha is None:
        report["details"].append(f"Frontend repository not found or not a valid git repository at {frontend_dir}")

    if b_sha and f_sha:
        # Validate repository identity
        if b_remote and "collectflow" not in b_remote.lower():
            report["details"].append(f"Backend remote origin '{b_remote}' does not match expected CollectFlow backend repository.")
        if f_remote and "cash-flow-buddy-101" not in f_remote.lower():
            report["details"].append(f"Frontend remote origin '{f_remote}' does not match expected CashFlowBuddy frontend repository.")

        # Validate clean working trees
        if not b_clean:
            report["details"].append(f"Backend working tree at {backend_dir} contains uncommitted changes (dirty).")
        if not f_clean:
            report["details"].append(f"Frontend working tree at {frontend_dir} contains uncommitted changes (dirty).")

        # Validate expected SHAs if provided
        if args.expected_backend_sha and b_sha != args.expected_backend_sha:
            report["details"].append(
                f"Backend SHA mismatch: expected {args.expected_backend_sha}, but found {b_sha}"
            )
        if args.expected_frontend_sha and f_sha != args.expected_frontend_sha:
            report["details"].append(
                f"Frontend SHA mismatch: expected {args.expected_frontend_sha}, but found {f_sha}"
            )

        if not report["details"]:
            report["pairing_status"] = "VERIFIED_PAIR"
            report["verdict"] = "PASS"
            report["details"].append(
                f"Successfully paired backend {b_sha[:10]} ({b_branch}) with frontend {f_sha[:10]} ({f_branch}) in dual checkout."
            )
        else:
            report["pairing_status"] = "PAIRING_VERIFICATION_FAILED"
            report["verdict"] = "FAIL"

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"[CROSS-REPO INTEGRATION] Verdict: {report['verdict']} ({report['pairing_status']})")
    for d in report["details"]:
        print(f" - {d}")

    # Exit 0 if PASS, exit 0 if BLOCKED in CI, exit 1 if FAIL
    if report["verdict"] == "PASS":
        return 0
    elif report["verdict"] == "BLOCKED" and in_ci:
        return 0
    else:
        return 1

if __name__ == "__main__":
    sys.exit(main())
