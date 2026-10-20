#!/usr/bin/env python3
"""
CollectFlow WP-06 Deterministic Financial Release Acceptance Pipeline.

Executes the deterministic, noninteractive release acceptance verification sequence:
1. Verify clean working tree on both backend and frontend repositories (reject uncommitted/untracked changes).
2. Capture and pin expected source revisions (backend & frontend Git SHAs).
3. Purge stale generated acceptance artifacts (preventing silent reuse or restamping).
4. Execute full backend pytest regression and generate JUnit XML report.
5. Generate validated structured test evidence JSON with XML SHA-256 checksum.
6. Execute genuine five-source cross-layer reconciliation (PostgreSQL, REST API, Playwright DOM, XLSX, CSV).
7. Execute formal financial release readiness gate with exact SHA pinning.
8. Verify source revisions remained unchanged across execution.
9. Enforce fail-closed boundaries (Corporate Finance status: AWAITING_FINANCE_APPROVAL).

Diagnostic bypasses (--skip-pytest, --skip-clean, --allow-dirty) are strictly prohibited in formal mode
and cannot authorize formal release acceptance.

Exit Codes:
  0: Complete Acceptance (Technical PASS & Corporate Business APPROVED)
  2: Ready for Independent Re-Review (Technical PASS & Corporate Business BLOCKED)
  1: Pipeline Verification Failure (Technical FAIL, Invariant Violation, or Unauthorized Diagnostic Bypass)
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Optional, Tuple


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


def check_clean_working_tree(repo_dir: Path) -> Tuple[bool, str]:
    """Checks whether a git repository has a completely clean working tree (no uncommitted or untracked changes)."""
    res = subprocess.run(
        ["git", "-C", str(repo_dir), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    status_output = res.stdout.strip()
    if status_output:
        return False, status_output
    return True, ""


def run_command(cmd: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    print(f"\n[EXEC] {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True)
    if res.stdout:
        print(res.stdout)
    if res.stderr:
        print(res.stderr, file=sys.stderr)
    return res


def run_pipeline(
    backend_dir: Path,
    frontend_dir: Path,
    artifacts_dir: Optional[Path] = None,
    expected_backend_sha: Optional[str] = None,
    expected_frontend_sha: Optional[str] = None,
    skip_pytest: bool = False,
    skip_clean: bool = False,
    allow_dirty: bool = False,
    diagnostic: bool = False,
) -> int:
    backend_dir = backend_dir.resolve()
    frontend_dir = frontend_dir.resolve()
    if artifacts_dir is None:
        artifacts_dir = backend_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    is_diagnostic = diagnostic or skip_pytest or skip_clean or allow_dirty

    # 1. Enforce strict prohibition of diagnostic bypasses in formal mode (WP06-R08)
    if (skip_pytest or skip_clean or allow_dirty) and not diagnostic:
        print(
            "FATAL: Diagnostic bypass options (--skip-pytest, --skip-clean, --allow-dirty) "
            "are strictly prohibited in formal financial release acceptance mode.",
            file=sys.stderr,
        )
        print(
            "Formal release acceptance mandates clean working trees, fresh artifact purge, and full backend regression execution.",
            file=sys.stderr,
        )
        return 1

    if is_diagnostic:
        print("\n" + "!" * 110)
        print("WARNING: EXECUTING IN DIAGNOSTIC MODE. THIS RUN CANNOT PRODUCE AN ACCEPTED FORMAL RELEASE GATE PASS.")
        print("!" * 110)

    # 2. Check clean working trees on both repositories (WP06-R08)
    if not allow_dirty:
        b_clean, b_diff = check_clean_working_tree(backend_dir)
        if not b_clean:
            print(
                f"FATAL: Backend repository '{backend_dir.name}' working tree is dirty (uncommitted/untracked changes detected):",
                file=sys.stderr,
            )
            print(b_diff, file=sys.stderr)
            print("Formal financial release acceptance strictly requires clean working trees.", file=sys.stderr)
            return 1

        f_clean, f_diff = check_clean_working_tree(frontend_dir)
        if not f_clean:
            print(
                f"FATAL: Frontend repository '{frontend_dir.name}' working tree is dirty (uncommitted/untracked changes detected):",
                file=sys.stderr,
            )
            print(f_diff, file=sys.stderr)
            print("Formal financial release acceptance strictly requires clean working trees.", file=sys.stderr)
            return 1

    # 3. Capture and pin expected source revisions
    initial_backend_sha = get_git_commit_sha(backend_dir)
    initial_frontend_sha = get_git_commit_sha(frontend_dir)

    expected_b = expected_backend_sha or initial_backend_sha
    expected_f = expected_frontend_sha or initial_frontend_sha

    if initial_backend_sha.lower() != expected_b.lower():
        print(f"FATAL: Backend SHA mismatch: current={initial_backend_sha}, expected={expected_b}", file=sys.stderr)
        return 1
    if initial_frontend_sha.lower() != expected_f.lower():
        print(f"FATAL: Frontend SHA mismatch: current={initial_frontend_sha}, expected={expected_f}", file=sys.stderr)
        return 1

    print("=" * 110)
    print("COLLECTFLOW WP-06 FINANCIAL ACCEPTANCE PIPELINE")
    print(f"Mode:          {'DIAGNOSTIC (NON-ACCEPTABLE)' if is_diagnostic else 'FORMAL RELEASE ACCEPTANCE'}")
    print(f"Backend SHA:   {initial_backend_sha}")
    print(f"Frontend SHA:  {initial_frontend_sha}")
    print(f"Backend Dir:   {backend_dir}")
    print(f"Frontend Dir:  {frontend_dir}")
    print(f"Artifacts Dir: {artifacts_dir}")
    print("=" * 110)

    # Environment setup
    env = os.environ.copy()
    env["COLLECTFLOW_ENV"] = "test"
    if "COLLECTFLOW_DATABASE_URL" not in env:
        env["COLLECTFLOW_DATABASE_URL"] = (
            "postgresql+psycopg://collectflow_user:collectflow_secret@127.0.0.1:5432/collectflow_test"
        )

    venv_python = backend_dir / "venv" / "bin" / "python"
    if not venv_python.is_file():
        venv_python = Path(sys.executable)

    # 4. Clean prior artifacts to prevent silent reuse or restamping (WP06-R06, WP06-R07, WP06-R08)
    xml_path = artifacts_dir / "backend_pytest_report.xml"
    ev_path = artifacts_dir / "backend_test_evidence.json"
    recon_path = artifacts_dir / "reconciliation_summary.json"
    manifest_path = artifacts_dir / "financial_readiness_manifest.json"

    if not skip_clean:
        print("\n[STAGE 0: Artifact Cleanup] Purging stale acceptance evidence to guarantee fresh generation...")
        for p in [xml_path, ev_path, recon_path, manifest_path]:
            if p.is_file():
                p.unlink()
                print(f"  Purged: {p.name}")

    # 5. Full backend pytest regression execution (WP06-R08: cannot be skipped in formal mode)
    if not skip_pytest:
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
            return 1
        if not xml_path.is_file():
            print(f"FATAL: Pytest XML report not generated at {xml_path}", file=sys.stderr)
            return 1
    else:
        if not xml_path.is_file():
            print(f"FATAL: Cannot run diagnostic pipeline with skip_pytest without existing XML at {xml_path}", file=sys.stderr)
            return 1

    # Verify backend revision didn't mutate during tests
    if get_git_commit_sha(backend_dir) != initial_backend_sha:
        print("FATAL: Source revision mutated during pytest execution!", file=sys.stderr)
        return 1

    # 6. Generate validated backend test evidence JSON
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
        return 1

    # 7. Execute genuine five-source cross-layer reconciliation
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
        return 1

    # 8. Execute formal financial release readiness gate
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
        return 1

    if not manifest_path.is_file():
        print(f"FATAL: Financial readiness manifest not generated at {manifest_path}", file=sys.stderr)
        return 1

    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    tech_verdict = manifest_data.get("technical_financial_integrity", {}).get("verdict")
    biz_verdict = manifest_data.get("business_authorization", {}).get("verdict")
    overall_verdict = manifest_data.get("overall_release_readiness")
    recommendation = manifest_data.get("recommendation")

    # 9. Verify source revisions remained unchanged across entire pipeline
    final_backend_sha = get_git_commit_sha(backend_dir)
    final_frontend_sha = get_git_commit_sha(frontend_dir)
    if final_backend_sha != initial_backend_sha or final_frontend_sha != initial_frontend_sha:
        print("FATAL: Source revision drift detected across pipeline execution!", file=sys.stderr)
        return 1

    # 10. Checksums and provenance summary
    xml_sha = compute_file_sha256(xml_path)
    ev_sha = compute_file_sha256(ev_path)
    recon_sha = compute_file_sha256(recon_path)
    manifest_sha = compute_file_sha256(manifest_path)

    print("\n" + "=" * 110)
    print("PIPELINE EXECUTION SUMMARY")
    print("=" * 110)
    print(f"Execution Mode:          {'DIAGNOSTIC (NON-ACCEPTABLE)' if is_diagnostic else 'FORMAL RELEASE ACCEPTANCE'}")
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

    if is_diagnostic:
        print("\n" + "=" * 110)
        print("DIAGNOSTIC PIPELINE EXECUTION COMPLETED")
        print("Verdict: BLOCKED_DIAGNOSTIC_MODE")
        print("Reason: Pipeline was executed with diagnostic bypasses (--skip-pytest, --skip-clean, or --allow-dirty).")
        print("Diagnostic execution CANNOT authorize formal financial release acceptance.")
        print("=" * 110)
        return 1

    if tech_verdict != "PASS":
        print(f"FAIL: Technical financial integrity verdict is '{tech_verdict}'", file=sys.stderr)
        return 1

    if biz_verdict == "BLOCKED" and overall_verdict == "BLOCKED":
        print("\nPIPELINE ACCEPTANCE: Technical criteria fully satisfied; release blocked pending external Finance sign-off.")
        return 2
    elif biz_verdict == "PASS" and overall_verdict == "PASS":
        print("\nPIPELINE ACCEPTANCE: Complete release acceptance satisfied.")
        return 0
    else:
        print(f"FAIL: Inconsistent gate verdicts: tech={tech_verdict}, biz={biz_verdict}, overall={overall_verdict}", file=sys.stderr)
        return 1


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
        "--artifacts-dir",
        type=Path,
        default=None,
        help="Path to artifacts directory (default: backend_dir/artifacts)",
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
    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        help="Diagnostic only: skip pytest regression execution",
    )
    parser.add_argument(
        "--skip-clean",
        action="store_true",
        help="Diagnostic only: do not purge prior generated artifacts",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Diagnostic only: bypass clean working tree check",
    )
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help="Explicitly mark run as diagnostic (incapable of producing formal acceptance PASS)",
    )
    args = parser.parse_args()

    exit_code = run_pipeline(
        backend_dir=args.backend_dir,
        frontend_dir=args.frontend_dir,
        artifacts_dir=args.artifacts_dir,
        expected_backend_sha=args.expected_backend_sha,
        expected_frontend_sha=args.expected_frontend_sha,
        skip_pytest=args.skip_pytest,
        skip_clean=args.skip_clean,
        allow_dirty=args.allow_dirty,
        diagnostic=args.diagnostic,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
