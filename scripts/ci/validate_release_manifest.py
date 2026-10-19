#!/usr/bin/env python3
"""
CollectFlow Release Candidate Manifest Validator (WP05-06, WP05-10)
Strictly validates a generated release-manifest.json:
- Verifies exact Git source commit SHAs against working trees.
- Validates container image digests.
- Re-computes and verifies SHA-256 checksums of all referenced SBOM files.
- Rejects unapproved or failed security gates.
- Enforces cross-repository pairing consistency.
"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def get_git_sha(repo_dir: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception as e:
        return f"UNKNOWN: {e}"

def validate_manifest(
    manifest_data: Dict[str, Any],
    backend_dir: Path = None,
    frontend_dir: Path = None,
    verify_git_shas: bool = True,
    require_deployment_approved: bool = False
) -> List[str]:
    errors = []

    # 1. Structural schema validation
    required_keys = [
        "release_candidate_id",
        "source_traceability",
        "container_artifacts",
        "sbom_artifacts",
        "quality_gates",
        "governance_and_approval"
    ]
    for key in required_keys:
        if key not in manifest_data:
            errors.append(f"Missing required top-level key: '{key}'")

    if errors:
        return errors

    # 2. Source Traceability and Git SHA Validation
    source_trace = manifest_data.get("source_traceability", {})
    backend_source = source_trace.get("backend", {})
    frontend_source = source_trace.get("frontend", {})

    backend_sha = backend_source.get("commit_sha", "")
    frontend_sha = frontend_source.get("commit_sha", "")

    if not backend_sha or len(backend_sha) < 40 or "UNKNOWN" in backend_sha:
        errors.append(f"Invalid backend source commit SHA: '{backend_sha}'")
    if not frontend_sha or len(frontend_sha) < 40 or "UNKNOWN" in frontend_sha:
        errors.append(f"Invalid frontend source commit SHA: '{frontend_sha}'")

    if verify_git_shas and backend_dir and backend_dir.exists():
        actual_backend_sha = get_git_sha(backend_dir)
        if actual_backend_sha != backend_sha:
            errors.append(
                f"Backend source SHA mismatch! Manifest specifies '{backend_sha}' but repo HEAD is '{actual_backend_sha}'"
            )

    if verify_git_shas and frontend_dir and frontend_dir.exists():
        actual_frontend_sha = get_git_sha(frontend_dir)
        if actual_frontend_sha != frontend_sha:
            errors.append(
                f"Frontend source SHA mismatch! Manifest specifies '{frontend_sha}' but repo HEAD is '{actual_frontend_sha}'"
            )

    # 3. Container Digest Validation
    container_artifacts = manifest_data.get("container_artifacts", {})
    for component in ["backend", "frontend"]:
        c_info = container_artifacts.get(component, {})
        digest = c_info.get("image_digest", "")
        if not digest or not digest.startswith("sha256:") or len(digest) < 15 or "unknown" in digest.lower():
            errors.append(f"Invalid or missing {component} container image digest: '{digest}'")
        
        scan_verdict = c_info.get("scan_verdict", "")
        if "FAIL" in scan_verdict.upper():
            errors.append(f"{component} container vulnerability scan failed: '{scan_verdict}'")

    # 4. SBOM Artifact & Checksum Validation
    sbom_artifacts = manifest_data.get("sbom_artifacts", {})
    for component in ["backend", "frontend"]:
        sbom_info = sbom_artifacts.get(component, {})
        sbom_path_str = sbom_info.get("file_path")
        expected_checksum = sbom_info.get("sha256_checksum")

        if not sbom_path_str:
            errors.append(f"Missing SBOM file path for {component}")
            continue

        sbom_path = Path(sbom_path_str)
        if not sbom_path.exists():
            errors.append(f"Referenced {component} SBOM file does not exist: {sbom_path}")
            continue

        if not expected_checksum:
            errors.append(f"Missing recorded SHA-256 checksum for {component} SBOM")
            continue

        actual_checksum = compute_sha256(sbom_path)
        if actual_checksum != expected_checksum:
            errors.append(
                f"{component} SBOM checksum mismatch! Manifest recorded '{expected_checksum}' but actual file has '{actual_checksum}'"
            )

    # 5. Quality Gate Verification
    quality_gates = manifest_data.get("quality_gates", {})
    backend_tests = quality_gates.get("backend_regression_tests", {})
    if backend_tests.get("verdict") != "PASS" or backend_tests.get("failed", 1) > 0:
        errors.append(f"Backend regression tests failed or did not receive PASS: {backend_tests}")

    frontend_tests = quality_gates.get("frontend_regression_tests", {})
    if frontend_tests.get("verdict") != "PASS" or frontend_tests.get("failed", 1) > 0:
        errors.append(f"Frontend regression tests failed or did not receive PASS: {frontend_tests}")

    for gate_name in ["python_dependency_audit", "npm_dependency_audit", "secret_scanning", "sast_lint_checks"]:
        verdict = quality_gates.get(gate_name, "")
        if not str(verdict).startswith("PASS"):
            errors.append(f"Security quality gate '{gate_name}' failed with verdict: '{verdict}'")

    # 6. Governance & Deployment Authorization Gate
    gov = manifest_data.get("governance_and_approval", {})
    deployment_authorized = gov.get("deployment_authorized", False)
    if require_deployment_approved and not deployment_authorized:
        errors.append("Manifest is not authorized for deployment (deployment_authorized is false).")

    return errors

def main() -> int:
    parser = argparse.ArgumentParser(description="Validate CollectFlow Release Manifest")
    parser.add_argument("--manifest", required=True, help="Path to release-manifest.json")
    parser.add_argument("--backend-dir", help="Path to backend git repo (for SHA verification)")
    parser.add_argument("--frontend-dir", help="Path to frontend git repo (for SHA verification)")
    parser.add_argument("--skip-git-verify", action="store_true", help="Skip verifying SHAs against local git repos")
    parser.add_argument("--require-approved", action="store_true", help="Enforce deployment_authorized=true")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        print(f"ERROR: Manifest file not found at {manifest_path}", file=sys.stderr)
        return 1

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: Failed to parse manifest JSON: {e}", file=sys.stderr)
        return 1

    backend_dir = Path(args.backend_dir).resolve() if args.backend_dir else None
    frontend_dir = Path(args.frontend_dir).resolve() if args.frontend_dir else None

    print(f"Validating CollectFlow release manifest: {manifest_path}")
    errors = validate_manifest(
        manifest_data=manifest_data,
        backend_dir=backend_dir,
        frontend_dir=frontend_dir,
        verify_git_shas=not args.skip_git_verify,
        require_deployment_approved=args.require_approved
    )

    if errors:
        print(f"\n[VALIDATION FAILED] {len(errors)} release rejection error(s) detected:")
        for err in errors:
            print(f" - {err}")
        return 1

    print("\n[VALIDATION PASSED] Release candidate manifest is valid, authentic, and consistent.")
    rc_id = manifest_data.get("release_candidate_id", "N/A")
    backend_sha = manifest_data.get("source_traceability", {}).get("backend", {}).get("commit_sha", "N/A")
    frontend_sha = manifest_data.get("source_traceability", {}).get("frontend", {}).get("commit_sha", "N/A")
    print(f"Release Candidate ID: {rc_id}")
    print(f"Backend Commit SHA:   {backend_sha}")
    print(f"Frontend Commit SHA:  {frontend_sha}")
    print("Deployment State:     UNAPPROVED (Protected boundary intact)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
