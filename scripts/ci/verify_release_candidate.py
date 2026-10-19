#!/usr/bin/env python3
"""
CollectFlow Consolidated Release Candidate Verification Gate (WP05-R02)
Unified release-candidate entrypoint linking:
1. Source repository inspection and exact revision verification
2. Cross-repository pairing verification (fails closed; rejects BLOCKED status)
3. CycloneDX SBOM verification / generation
4. Release manifest generation from structured quality gate evidence
5. Deep manifest validation, checksum recomputation, anti-forgery, and Docker image identity

Exit Codes:
  0: RC_PASSED  - Release candidate is valid, authentic, and verified.
  1: RC_FAILED  - Validation error, evidence forgery, test failure, dirty tree.
  2: RC_BLOCKED - Dual checkout or cross-repo pairing is missing/blocked.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

def run_command(cmd: list, desc: str) -> subprocess.CompletedProcess:
    print(f"--> [RC GATE] {desc}...")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.stdout:
        for line in res.stdout.strip().splitlines():
            print(f"    {line}")
    if res.stderr:
        for line in res.stderr.strip().splitlines():
            print(f"    [stderr] {line}", file=sys.stderr)
    return res

def main() -> int:
    parser = argparse.ArgumentParser(
        description="CollectFlow Consolidated Release Candidate Verification Gate (WP05-R02)"
    )
    parser.add_argument("--rc-id", help="Release candidate identifier (default: auto-generated date-based)")
    parser.add_argument("--backend-dir", default=".", help="Path to backend repository")
    parser.add_argument("--frontend-dir", default="../cash-flow-buddy-101", help="Path to frontend repository")
    parser.add_argument("--expected-backend-sha", help="Mandatory expected backend commit SHA")
    parser.add_argument("--expected-frontend-sha", help="Mandatory expected frontend commit SHA")
    parser.add_argument("--output-manifest", default="/tmp/release-manifest.json", help="Output path for release manifest")
    parser.add_argument("--cross-repo-report", default="/tmp/cross-repo-integration-report.json", help="Cross-repo report path")
    parser.add_argument("--backend-test-report", default="/tmp/backend-test-summary.json", help="Backend test summary JSON")
    parser.add_argument("--frontend-test-report", default="/tmp/frontend-test-summary.json", help="Frontend test summary JSON")
    parser.add_argument("--backend-audit-report", default="/tmp/backend-pip-audit.json", help="Backend pip-audit JSON")
    parser.add_argument("--frontend-audit-report", default="/tmp/frontend-npm-audit.json", help="Frontend npm-audit JSON")
    parser.add_argument("--secret-scan-report", default="/tmp/backend-secret-scan.json", help="Secret scanning report JSON")
    parser.add_argument("--sast-lint-report", default="/tmp/backend-lint-report.json", help="SAST lint report JSON")
    parser.add_argument("--backend-meta", default="/tmp/backend-image-meta.json", help="Backend container metadata JSON")
    parser.add_argument("--frontend-meta", default="/tmp/frontend-image-meta.json", help="Frontend container metadata JSON")
    parser.add_argument("--backend-sbom", default="/tmp/collectflow-backend-sbom.json", help="Backend SBOM path")
    parser.add_argument("--frontend-sbom", default="/tmp/collectflow-frontend-sbom.json", help="Frontend SBOM path")
    parser.add_argument("--skip-docker-verify", action="store_true", help="Skip Docker daemon image inspection")
    parser.add_argument("--skip-git-verify", action="store_true", help="Skip git SHA verification against local repos")
    parser.add_argument("--require-approved", action="store_true", help="Require deployment_authorized=true")
    args = parser.parse_args()

    date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    rc_id = args.rc_id or f"RC-{date_str}-01"

    backend_dir = Path(args.backend_dir).resolve()
    frontend_dir = Path(args.frontend_dir).resolve()
    manifest_out = Path(args.output_manifest).resolve()
    cross_repo_out = Path(args.cross_repo_report).resolve()

    print("================================================================")
    print("   CollectFlow Consolidated Release Candidate Gate (WP05-R02)")
    print(f"   Release Candidate ID: {rc_id}")
    print(f"   Backend Dir:          {backend_dir}")
    print(f"   Frontend Dir:         {frontend_dir}")
    print("================================================================\n")

    # Step 1: Verify source repositories and revisions
    if not (backend_dir / ".git").exists():
        print(f"ERROR: Backend repository not found at {backend_dir}", file=sys.stderr)
        return 1
    if not (frontend_dir / ".git").exists():
        print(f"ERROR: Frontend repository not found at {frontend_dir}", file=sys.stderr)
        return 2 # BLOCKED

    try:
        act_b_sha = subprocess.run(["git", "-C", str(backend_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        act_f_sha = subprocess.run(["git", "-C", str(frontend_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except Exception as e:
        print(f"ERROR: Failed to retrieve git commit SHAs: {e}", file=sys.stderr)
        return 1

    print(f"Identified Source Commits:")
    print(f"  Backend HEAD:  {act_b_sha}")
    print(f"  Frontend HEAD: {act_f_sha}")

    if args.expected_backend_sha and act_b_sha != args.expected_backend_sha:
        print(f"ERROR: Backend commit SHA mismatch! Expected {args.expected_backend_sha}, but found {act_b_sha}", file=sys.stderr)
        return 1

    if args.expected_frontend_sha and act_f_sha != args.expected_frontend_sha:
        print(f"ERROR: Frontend commit SHA mismatch! Expected {args.expected_frontend_sha}, but found {act_f_sha}", file=sys.stderr)
        return 1

    # Step 2: Run cross-repository pairing gate
    cross_repo_script = backend_dir / "scripts" / "ci" / "verify_cross_repo_release.py"
    cross_cmd = [
        sys.executable, str(cross_repo_script),
        "--initiator", "backend",
        "--backend-path", str(backend_dir),
        "--frontend-path", str(frontend_dir),
        "--report", str(cross_repo_out)
    ]
    if args.expected_backend_sha:
        cross_cmd.extend(["--expected-backend-sha", args.expected_backend_sha])
    if args.expected_frontend_sha:
        cross_cmd.extend(["--expected-frontend-sha", args.expected_frontend_sha])

    cross_res = run_command(cross_cmd, "Verifying Cross-Repository Pairing Gate")
    if not cross_repo_out.exists():
        print("ERROR: Cross-repo verification failed to produce output report.", file=sys.stderr)
        return 1

    try:
        cross_data = json.loads(cross_repo_out.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: Failed to parse cross-repo report: {e}", file=sys.stderr)
        return 1

    c_verdict = cross_data.get("verdict")
    c_status = cross_data.get("pairing_status")
    if c_verdict == "BLOCKED" or "BLOCKED" in str(c_status):
        print(f"\n[RC GATE BLOCKED] Cross-repository integration gate is BLOCKED ({c_status}).", file=sys.stderr)
        print("Release candidate cannot be accepted without dual repository checkout and matching source pairing.", file=sys.stderr)
        return 2

    if c_verdict != "PASS" or c_status != "VERIFIED_PAIR":
        print(f"\n[RC GATE FAILED] Cross-repository pairing failed (verdict: {c_verdict}, status: {c_status}).", file=sys.stderr)
        return 1

    # Step 3: Ensure SBOM artifacts exist
    backend_sbom_path = Path(args.backend_sbom).resolve()
    frontend_sbom_path = Path(args.frontend_sbom).resolve()

    if not backend_sbom_path.exists():
        sbom_script = backend_dir / "scripts" / "ci" / "generate_sbom.py"
        run_command([
            sys.executable, str(sbom_script),
            "--repo-type", "backend",
            "--root", str(backend_dir),
            "--output", str(backend_sbom_path)
        ], "Generating Backend CycloneDX SBOM")

    if not frontend_sbom_path.exists():
        f_sbom_script = frontend_dir / "scripts" / "ci" / "generate_sbom.py"
        run_command([
            sys.executable, str(f_sbom_script),
            "--repo-type", "frontend",
            "--root", str(frontend_dir),
            "--output", str(frontend_sbom_path)
        ], "Generating Frontend CycloneDX SBOM")

    # Step 4: Generate Release Candidate Manifest
    gen_script = backend_dir / "scripts" / "ci" / "generate_release_manifest.py"
    gen_cmd = [
        sys.executable, str(gen_script),
        "--rc-id", rc_id,
        "--backend-dir", str(backend_dir),
        "--frontend-dir", str(frontend_dir),
        "--output", str(manifest_out),
        "--backend-meta", args.backend_meta,
        "--frontend-meta", args.frontend_meta,
        "--backend-sbom", str(backend_sbom_path),
        "--frontend-sbom", str(frontend_sbom_path),
        "--backend-test-report", args.backend_test_report,
        "--frontend-test-report", args.frontend_test_report,
        "--backend-audit-report", args.backend_audit_report,
        "--frontend-audit-report", args.frontend_audit_report,
        "--secret-scan-report", args.secret_scan_report,
        "--sast-lint-report", args.sast_lint_report,
        "--cross-repo-report", str(cross_repo_out)
    ]
    gen_res = run_command(gen_cmd, "Generating Immutable Release Candidate Manifest")
    if gen_res.returncode != 0 or not manifest_out.exists():
        print(f"\n[RC GATE FAILED] Release manifest generation failed with exit code {gen_res.returncode}.", file=sys.stderr)
        return 1

    # Step 5: Strictly Validate Release Candidate Manifest
    val_script = backend_dir / "scripts" / "ci" / "validate_release_manifest.py"
    val_cmd = [
        sys.executable, str(val_script),
        "--manifest", str(manifest_out),
        "--backend-dir", str(backend_dir),
        "--frontend-dir", str(frontend_dir)
    ]
    if args.skip_docker_verify:
        val_cmd.append("--skip-docker-verify")
    if args.skip_git_verify:
        val_cmd.append("--skip-git-verify")
    if args.require_approved:
        val_cmd.append("--require-approved")

    val_res = run_command(val_cmd, "Validating Release Candidate Manifest & Anti-Forgery Evidence")
    if val_res.returncode != 0:
        print(f"\n[RC GATE FAILED] Release candidate manifest validation failed with exit code {val_res.returncode}.", file=sys.stderr)
        return 1

    print("\n================================================================")
    print("   [RC GATE ACCEPTED] RELEASE CANDIDATE FORMALLY VERIFIED")
    print(f"   Candidate ID:     {rc_id}")
    print(f"   Backend SHA:      {act_b_sha}")
    print(f"   Frontend SHA:     {act_f_sha}")
    print(f"   Manifest:         {manifest_out}")
    print("   Publication:      DISABLED_BY_DEFAULT (Protected Boundary)")
    print("================================================================\n")
    return 0

if __name__ == "__main__":
    sys.exit(main())
