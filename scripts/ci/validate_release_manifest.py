#!/usr/bin/env python3
"""
CollectFlow Release Candidate Manifest Validator (WP05-01, WP05-03, WP05-04, WP05-R01, WP05-R03, WP05-R04)
Strictly validates a generated release-manifest.json:
- Verifies exact Git source commit SHAs and clean-worktree assertions.
- Enforces OCI revision labels matching exact source commit SHAs.
- Distinguishes Image Config IDs from remote registry digests.
- Strictly validates structured quality gate evidence (rejects fabricated or arbitrary string PASS values).
- Requires mandatory evidence paths and independently recomputes SHA-256 checksums (WP05-R04).
- Confirms evidence files contain claimed results / anti-forgery verification (WP05-R04).
- Rejects zero collected tests from producing PASS (WP05-R01).
- Enforces cross-repository pairing status (rejects BLOCKED, missing, or failed pairing).
- Verifies Docker daemon image configuration ID and OCI revision label against actual daemon images (WP05-R04).
- Re-computes and verifies SHA-256 checksums of all referenced SBOM files.
- Rejects missing, unverified, BLOCKED, ERROR, or failed container vulnerability scans (WP05-R03).
- Enforces disabled-by-default publication boundary.
"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from scripts.ci.vulnerability_exceptions import validate_exception_record
except ImportError:
    try:
        from vulnerability_exceptions import validate_exception_record
    except ImportError:
        validate_exception_record = None

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

def verify_evidence_file(
    gate_name: str,
    gate_obj: Dict[str, Any]
) -> Tuple[Optional[Any], List[str]]:
    """Verifies evidence file existence, SHA-256 integrity, and parses JSON (WP05-R04)."""
    errs: List[str] = []
    if not isinstance(gate_obj, dict):
        return None, [f"{gate_name} evidence is missing or not a structured object."]

    file_path_str = gate_obj.get("evidence_file")
    if not file_path_str:
        return None, [f"{gate_name} is missing mandatory 'evidence_file' path."]

    ev_path = Path(file_path_str)
    if not ev_path.exists():
        return None, [f"{gate_name} referenced evidence file does not exist: {ev_path}"]

    expected_sha = gate_obj.get("evidence_sha256")
    if not expected_sha or len(expected_sha) != 64:
        return None, [f"{gate_name} missing or invalid recorded SHA-256 evidence checksum."]

    actual_sha = compute_sha256(ev_path)
    if actual_sha != expected_sha:
        return None, [
            f"{gate_name} evidence checksum mismatch! Recorded '{expected_sha}', but actual file has '{actual_sha}'."
        ]

    try:
        content = json.loads(ev_path.read_text(encoding="utf-8"))
        return content, []
    except Exception as e:
        return None, [f"{gate_name} evidence file is not valid JSON: {e}"]

def verify_docker_image(
    image_tag: str,
    expected_image_id: str,
    expected_commit_sha: str
) -> List[str]:
    """Verifies local Docker daemon image configuration ID and OCI revision label (WP05-R04)."""
    errors: List[str] = []
    try:
        res = subprocess.run(
            ["docker", "inspect", image_tag],
            capture_output=True,
            text=True,
            check=True
        )
        inspect_data = json.loads(res.stdout)
        if not inspect_data or not isinstance(inspect_data, list):
            return [f"Docker inspect for '{image_tag}' returned invalid JSON structure."]
        img = inspect_data[0]
        actual_id = img.get("Id", "")
        if actual_id != expected_image_id:
            errors.append(
                f"Docker daemon image ID mismatch for '{image_tag}': manifest records '{expected_image_id}', but daemon reports '{actual_id}'"
            )
        labels = img.get("Config", {}).get("Labels") or {}
        actual_oci_rev = labels.get("org.opencontainers.image.revision", "")
        if actual_oci_rev != expected_commit_sha:
            errors.append(
                f"Docker daemon image OCI revision label mismatch for '{image_tag}': expected commit SHA '{expected_commit_sha}', but daemon image label is '{actual_oci_rev}'"
            )
    except subprocess.CalledProcessError as e:
        errors.append(f"Docker image '{image_tag}' not found in local daemon or inspect failed: {e.stderr.strip()}")
    except Exception as e:
        errors.append(f"Failed to inspect Docker image '{image_tag}': {e}")
    return errors

def validate_manifest(
    manifest_data: Dict[str, Any],
    backend_dir: Optional[Path] = None,
    frontend_dir: Optional[Path] = None,
    verify_git_shas: bool = True,
    verify_evidence_integrity: bool = True,
    verify_docker_images: bool = True,
    require_deployment_approved: bool = False,
    formal_rc_mode: bool = False
) -> List[str]:
    errors: List[str] = []

    # WP05-G03: Formal release-candidate mode strictly prohibits diagnostic bypasses
    if formal_rc_mode:
        if not verify_docker_images:
            errors.append("Diagnostic bypass violation: FORMAL RELEASE CANDIDATE REJECTED when Docker image verification (verify_docker_images=False) is bypassed (WP05-G03).")
        if not verify_git_shas:
            errors.append("Diagnostic bypass violation: FORMAL RELEASE CANDIDATE REJECTED when Git SHA verification (verify_git_shas=False) is bypassed (WP05-G03).")
        if not verify_evidence_integrity:
            errors.append("Diagnostic bypass violation: FORMAL RELEASE CANDIDATE REJECTED when evidence integrity verification (verify_evidence_integrity=False) is bypassed (WP05-G03).")

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


    # 2. Source Traceability, Git SHA & Clean Worktree Validation (WP05-01)
    source_trace = manifest_data.get("source_traceability", {})
    backend_source = source_trace.get("backend", {})
    frontend_source = source_trace.get("frontend", {})

    backend_sha = backend_source.get("commit_sha", "")
    frontend_sha = frontend_source.get("commit_sha", "")

    if not backend_sha or len(backend_sha) < 40 or "UNKNOWN" in backend_sha:
        errors.append(f"Invalid backend source commit SHA: '{backend_sha}'")
    if not frontend_sha or len(frontend_sha) < 40 or "UNKNOWN" in frontend_sha:
        errors.append(f"Invalid frontend source commit SHA: '{frontend_sha}'")

    # Clean worktree enforcement (WP05-01)
    if not isinstance(backend_source.get("clean_worktree"), bool) or backend_source.get("clean_worktree") is not True:
        errors.append("Backend source working tree is dirty or clean_worktree evidence is missing (must be boolean true).")

    if not isinstance(frontend_source.get("clean_worktree"), bool) or frontend_source.get("clean_worktree") is not True:
        errors.append("Frontend source working tree is dirty or clean_worktree evidence is missing (must be boolean true).")

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

    # 3. Container Artifacts, OCI Revisions & Vulnerability Scan Validation (WP05-02, WP05-04, WP05-R03, WP05-R04)
    container_artifacts = manifest_data.get("container_artifacts", {})
    for component, expected_sha in [("backend", backend_sha), ("frontend", frontend_sha)]:
        c_info = container_artifacts.get(component, {})
        image_id = c_info.get("image_id", "")
        if not image_id or not image_id.startswith("sha256:") or len(image_id) < 15 or "unknown" in image_id.lower():
            errors.append(f"Invalid or missing {component} container image ID / digest: '{image_id}'")

        # Verify OCI revision label matches source commit SHA (WP05-04)
        oci_rev = c_info.get("oci_revision")
        if not oci_rev or oci_rev != expected_sha:
            errors.append(
                f"{component} OCI revision mismatch! Expected commit SHA '{expected_sha}', but recorded '{oci_rev}'"
            )

        # Enforce fail-closed scan verdict & mandatory evidence (WP05-F02)
        sec_scan_meta = c_info.get("security_scan")
        if not isinstance(sec_scan_meta, dict) or not sec_scan_meta:
            errors.append(f"Missing mandatory container security scan evidence object for {component}")
            continue

        ev_file_str = sec_scan_meta.get("evidence_file")
        ev_sha_str = sec_scan_meta.get("evidence_sha256")
        if not ev_file_str:
            errors.append(f"Missing mandatory container scan evidence_file for {component} (cannot accept unverified scan_verdict)")
        if not ev_sha_str:
            errors.append(f"Missing mandatory container scan evidence_sha256 for {component}")

        scan_verdict = c_info.get("scan_verdict") or sec_scan_meta.get("verdict", "")
        if scan_verdict not in ["PASS", "PASS_WITH_APPROVED_EXCEPTIONS"]:
            errors.append(
                f"{component} container vulnerability scan did not pass verified gate (verdict: '{scan_verdict}')"
            )

        # Independent verification of Trivy report integrity and vulnerability counts (WP05-F02)
        if verify_evidence_integrity and ev_file_str:
            ev_path = Path(ev_file_str)
            if not ev_path.exists():
                errors.append(f"{component} container scan evidence file not found: {ev_path}")
            else:
                act_sha = compute_sha256(ev_path)
                if ev_sha_str and act_sha != ev_sha_str:
                    errors.append(f"{component} container scan evidence checksum mismatch: {ev_path}")

                scanner_name = sec_scan_meta.get("scanner", "")
                if scanner_name != "trivy":
                    errors.append(f"{component} container scanner must be 'trivy', found '{scanner_name}'")

                scanner_version = sec_scan_meta.get("scanner_version", "")
                if not scanner_version or scanner_version == "unavailable":
                    errors.append(f"{component} container scanner version is missing or unavailable")

                scanner_provenance = sec_scan_meta.get("scanner_provenance", "")
                if not scanner_provenance or scanner_provenance == "unavailable":
                    errors.append(f"{component} container scanner installation provenance is missing required field 'scanner_provenance' or unavailable (WP05-G03)")


                try:
                    rep_data = json.loads(ev_path.read_text(encoding="utf-8"))
                    if "SchemaVersion" not in rep_data or "Results" not in rep_data:
                        errors.append(f"{component} container scan evidence is missing required SchemaVersion or Results: {ev_path}")
                    else:
                        rep_crit = 0
                        rep_high = 0
                        findings_list = []
                        for r in rep_data.get("Results", []) or []:
                            for v in r.get("Vulnerabilities", []) or []:
                                sev = str(v.get("Severity", "")).upper()
                                cve = str(v.get("VulnerabilityID", "UNKNOWN")).strip().upper()
                                pkg = str(v.get("PkgName", "unknown")).strip()
                                if sev == "CRITICAL":
                                    rep_crit += 1
                                    findings_list.append((cve, pkg, sev))
                                elif sev == "HIGH":
                                    rep_high += 1
                                    findings_list.append((cve, pkg, sev))

                        claim_crit = sec_scan_meta.get("critical_count")
                        claim_high = sec_scan_meta.get("high_count")
                        if claim_crit is not None and claim_crit != rep_crit:
                            errors.append(
                                f"{component} container scan CRITICAL count mismatch! Manifest claims {claim_crit} but report contains {rep_crit}"
                            )
                        if claim_high is not None and claim_high != rep_high:
                            errors.append(
                                f"{component} container scan HIGH count mismatch! Manifest claims {claim_high} but report contains {rep_high}"
                            )

                        if scan_verdict == "PASS":
                            if rep_crit > 0 or rep_high > 0:
                                errors.append(
                                    f"{component} container scan claimed PASS, but report contains {rep_crit} CRITICAL and {rep_high} HIGH vulnerabilities (forgery detected)"
                                )
                        elif scan_verdict == "PASS_WITH_APPROVED_EXCEPTIONS":
                            import os
                            ext_token = os.environ.get("COLLECTFLOW_EXTERNAL_SECURITY_APPROVAL_TOKEN")
                            if not ext_token or len(ext_token.strip()) < 32 or "self" in ext_token.lower() or "dummy" in ext_token.lower():
                                errors.append(
                                    f"{component} vulnerability exceptions rejected: repository cannot self-approve exceptions without independently controlled external approval (WP05-G01)"
                                )
                            applied = sec_scan_meta.get("applied_exceptions", [])
                            if not isinstance(applied, list):
                                errors.append(f"{component} container scan applied_exceptions must be a structured list")
                            else:
                                target_img = c_info.get("image_name", f"collectflow-{component}")

                                for f_cve, f_pkg, f_sev in findings_list:
                                    matched = any(
                                        exp.get("cve_id") == f_cve and exp.get("package_name") == f_pkg
                                        for exp in applied
                                    )
                                    if not matched:
                                        errors.append(
                                            f"{component} container finding {f_cve} in {f_pkg} ({f_sev}) is not covered by approved exceptions"
                                        )
                                for exp in applied:
                                    if validate_exception_record:
                                        is_val, val_err, _ = validate_exception_record(exp, target_img)
                                        if not is_val:
                                            errors.append(f"{component} invalid applied exception ({exp.get('cve_id')}): {val_err}")
                except Exception as e:
                    errors.append(f"Failed to parse {component} container scan report JSON: {e}")


        # Docker daemon image inspection (WP05-R04)
        if verify_docker_images and image_id:
            image_tag = c_info.get("image_tag") or f"collectflow-{component}:{expected_sha}"
            docker_errs = verify_docker_image(image_tag, image_id, expected_sha)
            errors.extend(docker_errs)

        # Verify disabled publication policy
        if c_info.get("publication_status") != "DISABLED_BY_DEFAULT" or c_info.get("registry_published") is True:
            errors.append(f"{component} image publication policy violation: must be DISABLED_BY_DEFAULT.")

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

    # 5. Quality Gate Evidence Verification & Anti-Forgery (WP05-01, WP05-R01, WP05-R04)
    quality_gates = manifest_data.get("quality_gates", {})

    # 5a. Backend Regression Tests
    backend_tests = quality_gates.get("backend_regression_tests")
    if not isinstance(backend_tests, dict):
        errors.append("Backend regression tests evidence is missing or not a structured object.")
    else:
        if backend_tests.get("verdict") != "PASS" or backend_tests.get("failed", 1) > 0 or not isinstance(backend_tests.get("passed"), int):
            errors.append(f"Backend regression tests failed or did not receive structured PASS verdict: {backend_tests}")
        if backend_tests.get("passed", 0) <= 0:
            errors.append("Backend regression tests executed zero tests (cannot produce PASS).")

        if verify_evidence_integrity:
            ev_data, ev_errs = verify_evidence_file("Backend regression tests", backend_tests)
            if ev_errs:
                errors.extend(ev_errs)
            elif isinstance(ev_data, dict):
                # Anti-forgery check against evidence content
                if "passed" not in ev_data or "failed" not in ev_data:
                    errors.append("Backend test evidence file missing mandatory 'passed' or 'failed' counts.")
                else:
                    if ev_data["passed"] != backend_tests["passed"]:
                        errors.append(
                            f"Backend test evidence forgery detected! Manifest claims {backend_tests['passed']} passed, but evidence file records {ev_data['passed']}."
                        )
                    if ev_data["failed"] != backend_tests["failed"]:
                        errors.append(
                            f"Backend test evidence forgery detected! Manifest claims {backend_tests['failed']} failed, but evidence file records {ev_data['failed']}."
                        )
                    if ev_data["failed"] > 0:
                        errors.append(f"Backend test evidence records {ev_data['failed']} test failure(s).")
                    if ev_data["passed"] <= 0:
                        errors.append("Backend test evidence records zero passed tests.")

    # 5b. Frontend Regression Tests
    frontend_tests = quality_gates.get("frontend_regression_tests")
    if not isinstance(frontend_tests, dict):
        errors.append("Frontend regression tests evidence is missing or not a structured object.")
    else:
        if frontend_tests.get("verdict") != "PASS" or frontend_tests.get("failed", 1) > 0 or not isinstance(frontend_tests.get("passed"), int):
            errors.append(f"Frontend regression tests failed or did not receive structured PASS verdict: {frontend_tests}")
        if frontend_tests.get("passed", 0) <= 0:
            errors.append("Frontend regression tests executed zero tests (cannot produce PASS).")

        if verify_evidence_integrity:
            ev_data, ev_errs = verify_evidence_file("Frontend regression tests", frontend_tests)
            if ev_errs:
                errors.extend(ev_errs)
            elif isinstance(ev_data, dict):
                # Anti-forgery check against evidence content
                if "passed" not in ev_data or "failed" not in ev_data:
                    errors.append("Frontend test evidence file missing mandatory 'passed' or 'failed' counts.")
                else:
                    if ev_data["passed"] != frontend_tests["passed"]:
                        errors.append(
                            f"Frontend test evidence forgery detected! Manifest claims {frontend_tests['passed']} passed, but evidence file records {ev_data['passed']}."
                        )
                    if ev_data["failed"] != frontend_tests["failed"]:
                        errors.append(
                            f"Frontend test evidence forgery detected! Manifest claims {frontend_tests['failed']} failed, but evidence file records {ev_data['failed']}."
                        )
                    if ev_data["failed"] > 0:
                        errors.append(f"Frontend test evidence records {ev_data['failed']} test failure(s).")
                    if ev_data["passed"] <= 0:
                        errors.append("Frontend test evidence records zero passed tests.")

    # 5c. Python Dependency Audit (pip-audit)
    py_audit = quality_gates.get("python_dependency_audit")
    if not isinstance(py_audit, dict):
        errors.append("Python dependency audit evidence is missing or not a structured object.")
    else:
        if py_audit.get("verdict") != "PASS" or py_audit.get("vulnerabilities", 1) > 0:
            errors.append(f"Python dependency audit failed or has vulnerabilities: {py_audit}")

        if verify_evidence_integrity:
            ev_data, ev_errs = verify_evidence_file("Python dependency audit", py_audit)
            if ev_errs:
                errors.extend(ev_errs)
            elif isinstance(ev_data, dict):
                if "dependencies" not in ev_data or not isinstance(ev_data["dependencies"], list):
                    errors.append("Python audit evidence missing mandatory 'dependencies' list.")
                else:
                    actual_vulns = sum(len(d.get("vulns", [])) for d in ev_data["dependencies"])
                    if py_audit.get("vulnerabilities") != actual_vulns:
                        errors.append(
                            f"Python audit evidence forgery detected! Manifest claims {py_audit.get('vulnerabilities')} vulnerabilities, but evidence records {actual_vulns}."
                        )
                    if actual_vulns > 0:
                        errors.append(f"Python audit evidence records {actual_vulns} dependency vulnerability(ies).")

    # 5d. NPM Dependency Audit (npm-audit)
    npm_audit = quality_gates.get("npm_dependency_audit")
    if not isinstance(npm_audit, dict):
        errors.append("NPM dependency audit evidence is missing or not a structured object.")
    else:
        if npm_audit.get("verdict") != "PASS" or npm_audit.get("vulnerabilities", 1) > 0:
            errors.append(f"NPM dependency audit failed or has vulnerabilities: {npm_audit}")

        if verify_evidence_integrity:
            ev_data, ev_errs = verify_evidence_file("NPM dependency audit", npm_audit)
            if ev_errs:
                errors.extend(ev_errs)
            elif isinstance(ev_data, dict):
                if "metadata" not in ev_data or "vulnerabilities" not in ev_data.get("metadata", {}):
                    errors.append("NPM audit evidence missing mandatory 'metadata.vulnerabilities' object.")
                else:
                    meta = ev_data["metadata"]["vulnerabilities"]
                    actual_vulns = meta.get("high", 0) + meta.get("critical", 0)
                    if npm_audit.get("vulnerabilities") != actual_vulns:
                        errors.append(
                            f"NPM audit evidence forgery detected! Manifest claims {npm_audit.get('vulnerabilities')} vulnerabilities, but evidence records {actual_vulns}."
                        )
                    if actual_vulns > 0:
                        errors.append(f"NPM audit evidence records {actual_vulns} high/critical vulnerability(ies).")

    # 5e. Secret Scanning Gate
    sec_scan = quality_gates.get("secret_scanning")
    if not isinstance(sec_scan, dict):
        errors.append("Secret scanning evidence is missing or not a structured object.")
    else:
        if sec_scan.get("verdict") != "PASS" or sec_scan.get("findings_count", sec_scan.get("credentials_detected", 1)) > 0:
            errors.append(f"Secret scanning gate failed or credentials detected: {sec_scan}")

        if verify_evidence_integrity:
            ev_data, ev_errs = verify_evidence_file("Secret scanning", sec_scan)
            if ev_errs:
                errors.extend(ev_errs)
            elif isinstance(ev_data, dict):
                if "findings_count" not in ev_data or not isinstance(ev_data["findings_count"], int):
                    errors.append("Secret scanning evidence missing mandatory 'findings_count' integer.")
                else:
                    act_findings = ev_data["findings_count"]
                    if sec_scan.get("findings_count") != act_findings:
                        errors.append(
                            f"Secret scanning evidence forgery detected! Manifest claims {sec_scan.get('findings_count')} findings, but evidence records {act_findings}."
                        )
                    if act_findings > 0:
                        errors.append(f"Secret scanning evidence records {act_findings} active credential/secret finding(s).")

    # 5f. SAST / Lint Checks
    sast_lint = quality_gates.get("sast_lint_checks")
    if not isinstance(sast_lint, dict):
        errors.append("SAST / lint check evidence is missing or not a structured object.")
    else:
        if sast_lint.get("verdict") != "PASS" or sast_lint.get("lint_errors", 1) > 0:
            errors.append(f"SAST / lint gate failed with errors: {sast_lint}")

        if verify_evidence_integrity:
            ev_data, ev_errs = verify_evidence_file("SAST lint checks", sast_lint)
            if ev_errs:
                errors.extend(ev_errs)
            else:
                act_errors = 0
                if isinstance(ev_data, dict):
                    act_errors = ev_data.get("lint_errors", ev_data.get("errors", 0))
                elif isinstance(ev_data, list):
                    act_errors = len(ev_data)
                if sast_lint.get("lint_errors") != act_errors:
                    errors.append(
                        f"Lint evidence forgery detected! Manifest claims {sast_lint.get('lint_errors')} errors, but evidence records {act_errors}."
                    )
                if act_errors > 0:
                    errors.append(f"SAST / lint evidence records {act_errors} static analysis error(s).")

    # 5g. Cross-repository pairing gate enforcement (WP05-03, WP05-R02, WP05-R04)
    cross_repo = quality_gates.get("cross_repo_pairing")
    if not isinstance(cross_repo, dict):
        errors.append("Cross-repository pairing report evidence is missing or not a structured object.")
    else:
        cross_verdict = cross_repo.get("verdict")
        pairing_status = cross_repo.get("pairing_status")
        if cross_verdict == "BLOCKED" or "BLOCKED" in str(pairing_status):
            errors.append(
                f"Cross-repository integration gate is BLOCKED ({pairing_status}). "
                "Release candidate cannot be accepted without verified dual-repository source pairing."
            )
        elif cross_verdict != "PASS" or pairing_status != "VERIFIED_PAIR":
            errors.append(
                f"Cross-repository integration gate failed (verdict: '{cross_verdict}', status: '{pairing_status}')."
            )

        if verify_evidence_integrity:
            ev_data, ev_errs = verify_evidence_file("Cross-repo pairing", cross_repo)
            if ev_errs:
                errors.extend(ev_errs)
            elif isinstance(ev_data, dict):
                r_verdict = ev_data.get("verdict")
                r_status = ev_data.get("pairing_status")
                if r_verdict == "BLOCKED" or "BLOCKED" in str(r_status):
                    errors.append(
                        f"Cross-repo pairing evidence file indicates BLOCKED status ({r_status}). Release candidate cannot be accepted."
                    )
                elif r_verdict != "PASS" or r_status != "VERIFIED_PAIR":
                    errors.append(
                        f"Cross-repo pairing evidence file records failed status (verdict: '{r_verdict}', status: '{r_status}')."
                    )

                # Source SHA pairing anti-forgery verification
                b_ev_sha = ev_data.get("backend", {}).get("commit_sha")
                f_ev_sha = ev_data.get("frontend", {}).get("commit_sha")
                if b_ev_sha and b_ev_sha != backend_sha:
                    errors.append(
                        f"Cross-repo report backend SHA mismatch! Manifest source is '{backend_sha}', but report recorded '{b_ev_sha}'."
                    )
                if f_ev_sha and f_ev_sha != frontend_sha:
                    errors.append(
                        f"Cross-repo report frontend SHA mismatch! Manifest source is '{frontend_sha}', but report recorded '{f_ev_sha}'."
                    )

    # 6. Governance & Deployment Authorization Gate
    gov = manifest_data.get("governance_and_approval", {})
    deployment_authorized = gov.get("deployment_authorized", False)
    if require_deployment_approved and not deployment_authorized:
        errors.append("Manifest is not authorized for deployment (deployment_authorized is false).")

    return errors

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate CollectFlow Release Manifest (WP05-01, WP05-03, WP05-04, WP05-R01, WP05-R04)"
    )
    parser.add_argument("--manifest", required=True, help="Path to release-manifest.json")
    parser.add_argument("--backend-dir", help="Path to backend git repo (for SHA verification)")
    parser.add_argument("--frontend-dir", help="Path to frontend git repo (for SHA verification)")
    parser.add_argument("--skip-git-verify", action="store_true", help="Skip verifying SHAs against local git repos")
    parser.add_argument("--skip-evidence-verify", action="store_true", help="Skip evidence file content and checksum verification")
    parser.add_argument("--skip-docker-verify", action="store_true", help="Skip Docker daemon image inspection")
    parser.add_argument("--require-approved", action="store_true", help="Enforce deployment_authorized=true")
    parser.add_argument("--formal-rc", action="store_true", help="Enforce formal release candidate mode (prohibits diagnostic skips)")
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
        verify_evidence_integrity=not args.skip_evidence_verify,
        verify_docker_images=not args.skip_docker_verify,
        require_deployment_approved=args.require_approved,
        formal_rc_mode=args.formal_rc
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
