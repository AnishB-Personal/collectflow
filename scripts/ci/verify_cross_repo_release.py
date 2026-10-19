#!/usr/bin/env python3
"""
CollectFlow Cross-Repository Integration Gate (WP05-07)
Explicitly pairs and verifies immutable commit SHAs across backend and frontend repositories.
Generates a consolidated integration report.
Handles private cross-repo CI limitations safely (marking BLOCKED_PENDING_CROSS_REPO_AUTHORIZATION).
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

def get_git_info(repo_dir: Path) -> Tuple[Optional[str], Optional[str], bool]:
    """Returns (commit_sha, branch_name, is_clean)."""
    if not repo_dir.exists() or not (repo_dir / ".git").exists():
        return None, None, False
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

        return sha, branch, (len(status) == 0)
    except Exception:
        return None, None, False

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

    b_sha, b_branch, b_clean = get_git_info(backend_dir)
    f_sha, f_branch, f_clean = get_git_info(frontend_dir)

    report: Dict[str, Any] = {
        "gate": "WP05-07_CROSS_REPO_INTEGRATION",
        "timestamp": timestamp,
        "initiator": args.initiator,
        "execution_context": "CI_RUNNER" if in_ci else "LOCAL_RUNNER",
        "backend": {
            "path": str(backend_dir),
            "accessible": b_sha is not None,
            "commit_sha": b_sha,
            "branch": b_branch,
            "clean_worktree": b_clean
        },
        "frontend": {
            "path": str(frontend_dir),
            "accessible": f_sha is not None,
            "commit_sha": f_sha,
            "branch": f_branch,
            "clean_worktree": f_clean
        },
        "pairing_status": "UNKNOWN",
        "verdict": "FAIL",
        "details": []
    }

    # Handle CI cross-repo permission barrier safely
    if in_ci and (b_sha is None or f_sha is None):
        missing_repo = "frontend" if f_sha is None else "backend"
        report["pairing_status"] = "BLOCKED_PENDING_CROSS_REPO_CREDENTIALS"
        report["verdict"] = "BLOCKED"
        report["details"].append(
            f"Cross-repository integration in CI cannot access private {missing_repo} repository "
            "with default single-repository GITHUB_TOKEN. Access is intentionally restricted without "
            "granting excessive organization admin tokens. Use local dual-checkout runner or "
            "provide a repository-scoped deploy key."
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"[CROSS-REPO INTEGRATION] {report['pairing_status']}: {report['details'][0]}")
        return 0  # Do not hard-fail single-repo CI; mark integration gate as BLOCKED_PENDING_CROSS_REPO_CREDENTIALS

    # Local / dual-checkout validation
    if b_sha is None:
        report["details"].append(f"Backend repository not found or invalid at {backend_dir}")
    if f_sha is None:
        report["details"].append(f"Frontend repository not found or invalid at {frontend_dir}")

    if b_sha and f_sha:
        # Check expected SHAs if supplied
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
                f"Successfully paired backend {b_sha[:10]} ({b_branch}) with frontend {f_sha[:10]} ({f_branch})."
            )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"[CROSS-REPO INTEGRATION] Verdict: {report['verdict']} ({report['pairing_status']})")
    for d in report["details"]:
        print(f" - {d}")

    return 0 if report["verdict"] in ["PASS", "BLOCKED"] else 1

if __name__ == "__main__":
    sys.exit(main())
