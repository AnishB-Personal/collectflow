#!/usr/bin/env python3
"""
CollectFlow WP-06 Deterministic Financial Release Acceptance Pipeline.

Executes the deterministic, noninteractive release acceptance verification sequence:
1. Capture and pin expected source revisions (backend & frontend Git SHAs).
2. Purge stale generated acceptance artifacts (preventing silent reuse or restamping).
3. Execute full backend pytest regression and generate JUnit XML report.
4. Generate validated structured test evidence JSON with XML SHA-256 checksum.
5. Execute genuine five-source cross-layer reconciliation (PostgreSQL, REST API, Playwright DOM, XLSX, CSV).
6. Execute formal financial release readiness gate with exact SHA pinning.
7. Verify source revisions remained unchanged across execution.
8. Enforce fail-closed boundaries (Corporate Finance status: AWAITING_FINANCE_APPROVAL).

Exit Codes:
  0: Complete Acceptance (Technical PASS & Corporate Business APPROVED)
  2: Ready for Independent Re-Review (Technical PASS & Corporate Business BLOCKED)
  1: Pipeline Verification Failure (Technical FAIL or Invariant Violation)
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def get_git_commit_sha(repo_dir: Path) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    sha = res.stdout.strip()
    if len(sha) != 40:
        raise ValueError(f"Invalid Git SHA resolved from {repo_dir}: '{sha}'")
    return sha


def run_command(cmd: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    print(f"\n[EXEC] {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True)
    if res.stdout:
        print(res.stdout)
    if res.stderr:
        print(res.stderr, file=sys.stderr)
    return res


def main():
    parser = argparse.ArgumentParser(description="CollectFlow WP-06 Financial Acceptance Pipeline")
    parser.add_argument(
        "--backend-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent,
        help="Path to backend repository (collectflow)",
    )
    parser.add_argument(
        "--frontend-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent.parent / "cash-flow-buddy-101",
        help="Path to frontend repository (cash-flow-buddy-101)",
    )
    parser.add_argument(
        "--clean-artifacts",
        action="store_true",
        default=True,
        help="Purge prior generated acceptance artifacts before execution (default: True)",
    )
    parser.add_argument(
        "--skip-clean",
        action="store_true",
        help="Do not purge prior generated artifacts",
    )
    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        help="Skip running full pytest suite (uses existing artifacts/backend_pytest_report.xml)",
    )
    parser.add_argument(
        "--expected-backend-sha",
        type=str,
        default=None,
        help="Expected backend Git SHA override",
    )
    parser.add_argument(
        "--expected-frontend-sha",
        type=str,
        default=None,
        help="Expected frontend Git SHA override",
    )
    args = parser.parse_args()

    backend_dir = args.backend_dir.resolve()
    frontend_dir = args.frontend_dir.resolve()
    artifacts_dir = backend_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # 1. Capture and pin expected source revisions
    initial_backend_sha = get_git_commit_sha(backend_dir)
    initial_frontend_sha = get_git_commit_sha(frontend_dir)

    expected_b = args.expected_backend_sha or initial_backend_sha
    expected_f = args.expected_frontend_sha or initial_frontend_sha

    if initial_backend_sha.lower() != expected_b.lower():
        print(f"FATAL: Backend SHA mismatch: current={initial_backend_sha}, expected={expected_b}", file=sys.stderr)
        sys.exit(1)
    if initial_frontend_sha.lower() != expected_f.lower():
        print(f"FATAL: Frontend SHA mismatch: current={initial_frontend_sha}, expected={expected_f}", file=sys.stderr)
        sys.exit(1)

    print("=" * 110)
    print("COLLECTFLOW WP-06 FINANCIAL ACCEPTANCE PIPELINE")
    print(f"Backend SHA:   {initial_backend_sha}")
    print(f"Frontend SHA:  {initial_frontend_sha}")
    print(f"Backend Dir:   {backend_dir}")
    print(f"Frontend Dir:  {frontend_dir}")
    print("=" * 110)

    # Environment setup
    env = os.environ.copy()
    env["COLLECTFLOW_ENV"] = "test"
    if "COLLECTFLOW_DATABASE_URL" not in env:
        env["COLLECTFLOW_DATABASE_URL"] = "postgresql+psycopg://collectflow_user:collectflow_secret@127.0.0.1:5432/collectflow_test"

    venv_python = backend_dir / "venv" / "bin" / "python"
    if not venv_python.is_file():
        venv_python = Path(sys.executable)

    # 2. Clean prior artifacts to prevent silent reuse or restamping (WP06-R06, WP06-R07)
    xml_path = artifacts_dir / "backend_pytest_report.xml"
    ev_path = artifacts_dir / "backend_test_evidence.json"
    recon_path = artifacts_dir / "reconciliation_summary.json"
    manifest_path = artifacts_dir / "financial_readiness_manifest.json"

    if args.clean_artifacts and not args.skip_clean:
        print("\n[STAGE 0: Artifact Cleanup] Purging stale acceptance evidence to guarantee fresh generation...")
        for p in [xml_path, ev_path, recon_path, manifest_path]:
            if p.is_file():
                p.unlink()
                print(f"  Purged: {p.name}")

    # 3. Full backend pytest regression execution
    if not args.skip_pytest:
        print("\n[STAGE 1: Pytest Regression] Executing full backend pytest test suite...")
        pytest_cmd = [
            str(venv_python),
            "-m",
            "pytest",
            "tests/",
            f"--junitxml={xml_path}",
        ]
        res_pytest = run_command(pytest_cmd, cwd=backend_dir, env=env)
        if res_pytest.returncode != 0:
            print(f"FATAL: Full backend pytest failed with exit code {res_pytest.returncode}", file=sys.stderr)
            sys.exit(1)
        if not xml_path.is_file():
            print(f"FATAL: Pytest XML report not generated at {xml_path}", file=sys.stderr)
            sys.exit(1)

    # Verify backend revision didn't mutate during tests
    if get_git_commit_sha(backend_dir) != initial_backend_sha:
        print("FATAL: Source revision mutated during pytest execution!", file=sys.stderr)
        sys.exit(1)

    # 4. Generate validated backend test evidence JSON
    print("\n[STAGE 2: Test Evidence Generation] Generating validated backend test evidence...")
    gen_cmd = [
        str(venv_python),
        str(backend_dir / "scripts" / "release" / "generate_backend_test_evidence.py"),
        "--xml",
        str(xml_path),
        "--output",
        str(ev_path),
        "--repo-path",
        str(backend_dir),
    ]
    res_gen = run_command(gen_cmd, cwd=backend_dir, env=env)
    if res_gen.returncode != 0 or not ev_path.is_file():
        print("FATAL: Failed to generate backend test evidence JSON", file=sys.stderr)
        sys.exit(1)

    # 5. Execute genuine five-source cross-layer reconciliation
    print("\n[STAGE 3: Multi-Source Reconciliation] Executing genuine 5-source cross-layer reconciliation...")
    recon_cmd = [
        str(venv_python),
        str(backend_dir / "scripts" / "verify_cross_layer_reconciliation.py"),
        "--output",
        str(recon_path),
    ]
    res_recon = run_command(recon_cmd, cwd=backend_dir, env=env)
    if res_recon.returncode != 0 or not recon_path.is_file():
        print("FATAL: Multi-source reconciliation failed", file=sys.stderr)
        sys.exit(1)

    # 6. Execute formal financial release readiness gate
    print("\n[STAGE 4: Financial Release Readiness Gate] Running formal readiness verification...")
    gate_cmd = [
        str(venv_python),
        str(backend_dir / "scripts" / "release" / "verify_financial_readiness.py"),
        "--expected-backend-sha",
        initial_backend_sha,
        "--expected-frontend-sha",
        initial_frontend_sha,
        "--output",
        str(manifest_path),
    ]
    res_gate = run_command(gate_cmd, cwd=backend_dir, env=env)
    if res_gate.returncode not in (0, 2):
        print(f"FATAL: Financial readiness verification returned unexpected code: {res_gate.returncode}", file=sys.stderr)
        sys.exit(1)

    if not manifest_path.is_file():
        print(f"FATAL: Financial readiness manifest not generated at {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    tech_verdict = manifest_data.get("technical_financial_integrity", {}).get("verdict")
    biz_verdict = manifest_data.get("business_authorization", {}).get("verdict")
    overall_verdict = manifest_data.get("overall_release_readiness")
    recommendation = manifest_data.get("recommendation")

    # 7. Verify source revisions remained unchanged across entire pipeline
    final_backend_sha = get_git_commit_sha(backend_dir)
    final_frontend_sha = get_git_commit_sha(frontend_dir)
    if final_backend_sha != initial_backend_sha or final_frontend_sha != initial_frontend_sha:
        print("FATAL: Source revision drift detected across pipeline execution!", file=sys.stderr)
        sys.exit(1)

    # 8. Checksums and provenance summary
    xml_sha = compute_file_sha256(xml_path)
    ev_sha = compute_file_sha256(ev_path)
    recon_sha = compute_file_sha256(recon_path)
    manifest_sha = compute_file_sha256(manifest_path)

    print("\n" + "=" * 110)
    print("PIPELINE EXECUTION SUMMARY")
    print("=" * 110)
    print(f"Backend Revision:        {final_backend_sha} (verified unchanged)")
    print(f"Frontend Revision:       {final_frontend_sha} (verified unchanged)")
    print(f"Pytest XML Report:       {xml_path} (SHA-256: {xml_sha})")
    print(f"Backend Test Evidence:   {ev_path} (SHA-256: {ev_sha})")
    print(f"Reconciliation Summary:  {recon_path} (SHA-256: {recon_sha})")
    print(f"Release Gate Manifest:   {manifest_path} (SHA-256: {manifest_sha})")
    print("-" * 110)
    print(f"Technical Financial Integrity: {tech_verdict}")
    print(f"Corporate Business Gate:       {biz_verdict}")
    print(f"Overall Release Readiness:     {overall_verdict}")
    print(f"Recommendation:                {recommendation}")
    print("=" * 110)

    if tech_verdict != "PASS":
        print(f"FAIL: Technical financial integrity verdict is '{tech_verdict}'", file=sys.stderr)
        sys.exit(1)

    if biz_verdict == "BLOCKED" and overall_verdict == "BLOCKED":
        print("\nPIPELINE ACCEPTANCE: Technical criteria fully satisfied; release blocked pending external Finance sign-off.")
        sys.exit(2)
    elif biz_verdict == "PASS" and overall_verdict == "PASS":
        print("\nPIPELINE ACCEPTANCE: Complete release acceptance satisfied.")
        sys.exit(0)
    else:
        print(f"FAIL: Inconsistent gate verdicts: tech={tech_verdict}, biz={biz_verdict}, overall={overall_verdict}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
