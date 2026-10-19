"""
Comprehensive Test Suite for WP-05 CI/CD & Security Gates
Covers all review corrections (WP05-01 through WP05-06, WP05-R01 through WP05-R04):
- Elimination of fabricated release evidence (structured evidence ingestion)
- Strict evidence verification, checksum recomputation & anti-forgery checks (WP05-R04)
- Rejection of forged structured PASS objects (WP05-R04)
- Rejection of altered/tampered evidence files (WP05-R04)
- Rejection of zero collected test counts (WP05-R01)
- Rejection of malformed or incomplete evidence reports (WP05-R01)
- Docker daemon image configuration ID and OCI revision label validation (WP05-R04)
- Consolidated release candidate verification gate and rejection of BLOCKED status (WP05-R02)
- Fail-closed container scan verdicts & exceptions handling (WP05-R03)
- Executable negative integration check for disposable compose cleanup under failure injection (WP05-05)
- Disabled-by-default publication enforcement (WP05-05)
- Secret scanner log redaction
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from scripts.ci.validate_release_manifest import validate_manifest, verify_docker_image
from scripts.ci.scan_secrets import redact, scan_file
from scripts.ci.generate_release_manifest import (
    parse_test_report,
    parse_audit_report,
    parse_secret_scan_report,
    parse_lint_report,
    parse_cross_repo_report
)
from scripts.ci.vulnerability_exceptions import (
    validate_exception_record,
    load_and_validate_exceptions,
    evaluate_trivy_report
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

def create_valid_evidence_bundle(tmp_path, b_sha="a" * 40, f_sha="b" * 40):
    """Helper to create real, authentic evidence files and return a matching manifest."""
    b_test = tmp_path / "b_test.json"
    b_test.write_text(json.dumps({"passed": 249, "failed": 0, "verdict": "PASS"}), encoding="utf-8")

    f_test = tmp_path / "f_test.json"
    f_test.write_text(json.dumps({"passed": 620, "failed": 0, "verdict": "PASS"}), encoding="utf-8")

    py_audit = tmp_path / "py_audit.json"
    py_audit.write_text(json.dumps({"dependencies": []}), encoding="utf-8")

    npm_audit = tmp_path / "npm_audit.json"
    npm_audit.write_text(json.dumps({"metadata": {"vulnerabilities": {"high": 0, "critical": 0}}}), encoding="utf-8")

    sec_scan = tmp_path / "sec_scan.json"
    sec_scan.write_text(json.dumps({"findings_count": 0, "findings": []}), encoding="utf-8")

    lint = tmp_path / "lint.json"
    lint.write_text(json.dumps({"lint_errors": 0}), encoding="utf-8")

    cross_repo = tmp_path / "cross_repo.json"
    cross_repo.write_text(json.dumps({
        "gate": "WP05-03_CROSS_REPO_INTEGRATION",
        "verdict": "PASS",
        "pairing_status": "VERIFIED_PAIR",
        "backend": {"commit_sha": b_sha},
        "frontend": {"commit_sha": f_sha}
    }), encoding="utf-8")

    b_sbom = tmp_path / "backend-sbom.json"
    f_sbom = tmp_path / "frontend-sbom.json"
    b_sbom.write_text('{"components": []}', encoding="utf-8")
    f_sbom.write_text('{"components": []}', encoding="utf-8")

    trivy_clean = {
        "SchemaVersion": 2,
        "Results": [
            {
                "Target": "app",
                "Class": "os-pkgs",
                "Type": "alpine",
                "Vulnerabilities": []
            }
        ]
    }
    b_trivy = tmp_path / "b_trivy.json"
    f_trivy = tmp_path / "f_trivy.json"
    b_trivy.write_text(json.dumps(trivy_clean), encoding="utf-8")
    f_trivy.write_text(json.dumps(trivy_clean), encoding="utf-8")

    def sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()

    manifest = {
        "release_candidate_id": "RC-WP05-TEST-VALID",
        "source_traceability": {
            "backend": {"commit_sha": b_sha, "clean_worktree": True},
            "frontend": {"commit_sha": f_sha, "clean_worktree": True}
        },
        "container_artifacts": {
            "backend": {
                "image_name": "collectflow-backend",
                "image_tag": f"collectflow-backend:{b_sha}",
                "image_id": "sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
                "oci_revision": b_sha,
                "scan_verdict": "PASS",
                "security_scan": {
                    "scanner": "trivy",
                    "scanner_version": "Version: 0.76.0",
                    "scanner_provenance": "/usr/local/bin/trivy (Version: 0.76.0)",
                    "verdict": "PASS",
                    "details": "0 CRITICAL and 0 HIGH vulnerabilities detected in container image.",
                    "critical_count": 0,
                    "high_count": 0,
                    "blocking_findings": [],
                    "evidence_file": str(b_trivy),
                    "evidence_sha256": sha(b_trivy)
                },
                "publication_status": "DISABLED_BY_DEFAULT",
                "registry_published": False
            },
            "frontend": {
                "image_name": "collectflow-frontend",
                "image_tag": f"collectflow-frontend:{f_sha}",
                "image_id": "sha256:555566667777888899990000aaaabbbbccccddddeeeeffff1111222233334444",
                "oci_revision": f_sha,
                "scan_verdict": "PASS",
                "security_scan": {
                    "scanner": "trivy",
                    "scanner_version": "Version: 0.76.0",
                    "scanner_provenance": "/usr/local/bin/trivy (Version: 0.76.0)",
                    "verdict": "PASS",
                    "details": "0 CRITICAL and 0 HIGH vulnerabilities detected in container image.",
                    "critical_count": 0,
                    "high_count": 0,
                    "blocking_findings": [],
                    "evidence_file": str(f_trivy),
                    "evidence_sha256": sha(f_trivy)
                },

                "publication_status": "DISABLED_BY_DEFAULT",
                "registry_published": False
            }
        },
        "sbom_artifacts": {
            "backend": {"file_path": str(b_sbom), "sha256_checksum": sha(b_sbom)},
            "frontend": {"file_path": str(f_sbom), "sha256_checksum": sha(f_sbom)}
        },
        "quality_gates": {
            "backend_regression_tests": {"verdict": "PASS", "passed": 249, "failed": 0, "evidence_file": str(b_test), "evidence_sha256": sha(b_test)},
            "frontend_regression_tests": {"verdict": "PASS", "passed": 620, "failed": 0, "evidence_file": str(f_test), "evidence_sha256": sha(f_test)},
            "python_dependency_audit": {"verdict": "PASS", "vulnerabilities": 0, "evidence_file": str(py_audit), "evidence_sha256": sha(py_audit)},
            "npm_dependency_audit": {"verdict": "PASS", "vulnerabilities": 0, "evidence_file": str(npm_audit), "evidence_sha256": sha(npm_audit)},
            "secret_scanning": {"verdict": "PASS", "findings_count": 0, "evidence_file": str(sec_scan), "evidence_sha256": sha(sec_scan)},
            "sast_lint_checks": {"verdict": "PASS", "lint_errors": 0, "evidence_file": str(lint), "evidence_sha256": sha(lint)},
            "cross_repo_pairing": {"verdict": "PASS", "pairing_status": "VERIFIED_PAIR", "evidence_file": str(cross_repo), "evidence_sha256": sha(cross_repo)}
        },
        "governance_and_approval": {
            "deployment_authorized": False
        }
    }
    return manifest

def test_release_manifest_valid_structure(tmp_path):
    """Valid release manifest with authentic structured evidence, verified checksums, and matching SBOMs passes (WP05-01, WP05-R04)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    errors = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False)
    assert errors == []

def test_forged_structured_pass_rejection(tmp_path):
    """Manifest claiming 249 passed tests while evidence file only records 10 passed is rejected as forgery (WP05-R04)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    b_test_file = Path(manifest["quality_gates"]["backend_regression_tests"]["evidence_file"])
    # Write only 10 passed to the evidence file
    b_test_file.write_text(json.dumps({"passed": 10, "failed": 0}), encoding="utf-8")
    manifest["quality_gates"]["backend_regression_tests"]["evidence_sha256"] = hashlib.sha256(b_test_file.read_bytes()).hexdigest()

    errors = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False)
    assert any("Backend test evidence forgery detected" in e for e in errors)
    assert any("Manifest claims 249 passed, but evidence file records 10" in e for e in errors)

def test_altered_evidence_checksum_rejection(tmp_path):
    """Altered evidence file failing SHA-256 verification is rejected (WP05-R04)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    b_test_file = Path(manifest["quality_gates"]["backend_regression_tests"]["evidence_file"])
    # Tamper with evidence file without updating recorded checksum
    b_test_file.write_text(json.dumps({"passed": 249, "failed": 0, "tampered": True}), encoding="utf-8")

    errors = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False)
    assert any("evidence checksum mismatch" in e for e in errors)

def test_zero_tests_rejection(tmp_path):
    """Evidence file recording zero executed tests cannot produce PASS (WP05-R01)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    b_test_file = Path(manifest["quality_gates"]["backend_regression_tests"]["evidence_file"])
    b_test_file.write_text(json.dumps({"passed": 0, "failed": 0}), encoding="utf-8")
    manifest["quality_gates"]["backend_regression_tests"]["passed"] = 0
    manifest["quality_gates"]["backend_regression_tests"]["evidence_sha256"] = hashlib.sha256(b_test_file.read_bytes()).hexdigest()

    errors = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False)
    assert any("zero passed tests" in e or "executed zero tests" in e for e in errors)

def test_malformed_evidence_rejection(tmp_path):
    """Malformed evidence file is rejected fail-closed (WP05-R01)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    b_test_file = Path(manifest["quality_gates"]["backend_regression_tests"]["evidence_file"])
    b_test_file.write_text("{not-valid-json", encoding="utf-8")
    manifest["quality_gates"]["backend_regression_tests"]["evidence_sha256"] = hashlib.sha256(b_test_file.read_bytes()).hexdigest()

    errors = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False)
    assert any("is not valid JSON" in e for e in errors)

def test_missing_evidence_file_rejection(tmp_path):
    """Referenced evidence file that does not exist is rejected (WP05-R04)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    manifest["quality_gates"]["backend_regression_tests"]["evidence_file"] = "/tmp/nonexistent-test-evidence.json"
    errors = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False)
    assert any("referenced evidence file does not exist" in e for e in errors)

def test_dirty_worktree_rejection(tmp_path):
    """Manifest indicating dirty worktrees is rejected (WP05-01)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    manifest["source_traceability"]["backend"]["clean_worktree"] = False
    errors = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False)
    assert any("Backend source working tree is dirty" in e for e in errors)

def test_cross_repo_blocked_status_rejected_for_release_candidate(tmp_path):
    """Cross-repo gate with BLOCKED status must be rejected for release candidate acceptance (WP05-03, WP05-R02)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    manifest["quality_gates"]["cross_repo_pairing"]["verdict"] = "BLOCKED"
    manifest["quality_gates"]["cross_repo_pairing"]["pairing_status"] = "BLOCKED_PENDING_CROSS_REPO_CREDENTIALS"

    errors = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False)
    assert any("Cross-repository integration gate is BLOCKED" in e for e in errors)
    assert any("Release candidate cannot be accepted without verified dual-repository source pairing" in e for e in errors)

def test_oci_revision_mismatch_rejection(tmp_path):
    """Manifest specifying container with OCI revision label not matching source commit SHA is rejected (WP05-04)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    manifest["container_artifacts"]["backend"]["oci_revision"] = "c" * 40  # Mismatch
    errors = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False)
    assert any("backend OCI revision mismatch" in e for e in errors)

def test_container_scan_verdict_fail_closed(tmp_path):
    """Manifest with failed, blocked, or unknown scanner verdict is rejected (WP05-02, WP05-R03)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    manifest["container_artifacts"]["backend"]["scan_verdict"] = "BLOCKED_SCANNER_UNAVAILABLE"
    manifest["container_artifacts"]["frontend"]["scan_verdict"] = "FAIL_CRITICAL_VULNERABILITY"

    errors = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False)
    assert any("backend container vulnerability scan did not pass verified gate" in e for e in errors)
    assert any("frontend container vulnerability scan did not pass verified gate" in e for e in errors)

def test_docker_image_daemon_verification():
    """verify_docker_image correctly detects image ID mismatch and nonexistent images (WP05-R04)."""
    # Nonexistent image should report inspect error / not found
    errs = verify_docker_image("collectflow-nonexistent:fake", "sha256:1111", "a" * 40)
    assert len(errs) > 0
    assert any("not found" in e.lower() or "inspect failed" in e.lower() for e in errs)

def test_generator_strict_parsers(tmp_path):
    """Generator parser functions reject zero tests, incomplete schemas, and malformed JSON (WP05-R01)."""
    # Test parser: zero tests
    t_zero = tmp_path / "test_zero.json"
    t_zero.write_text(json.dumps({"passed": 0, "failed": 0}), encoding="utf-8")
    res = parse_test_report(t_zero, "test_suite")
    assert res["verdict"] == "FAIL_ZERO_TESTS_EXECUTED"

    # Test parser: missing keys
    t_inc = tmp_path / "test_inc.json"
    t_inc.write_text(json.dumps({"something": 123}), encoding="utf-8")
    res = parse_test_report(t_inc, "test_suite")
    assert res["verdict"] == "BLOCKED_EVIDENCE_INCOMPLETE"

    # Audit parser: pip-audit missing dependencies
    a_inc = tmp_path / "audit_inc.json"
    a_inc.write_text(json.dumps({"other": []}), encoding="utf-8")
    res = parse_audit_report(a_inc, "pip-audit")
    assert res["verdict"] == "BLOCKED_EVIDENCE_INCOMPLETE"

    # Audit parser: npm-audit missing metadata
    res = parse_audit_report(a_inc, "npm-audit")
    assert res["verdict"] == "BLOCKED_EVIDENCE_INCOMPLETE"

    # Secret scanner: missing findings_count
    s_inc = tmp_path / "sec_inc.json"
    s_inc.write_text(json.dumps({"records": []}), encoding="utf-8")
    res = parse_secret_scan_report(s_inc)
    assert res["verdict"] == "BLOCKED_EVIDENCE_INCOMPLETE"

    # Lint: malformed JSON
    l_mal = tmp_path / "lint_mal.json"
    l_mal.write_text("{bad", encoding="utf-8")
    res = parse_lint_report(l_mal)
    assert res["verdict"] == "BLOCKED_EVIDENCE_MALFORMED"

def test_consolidated_rc_gate_blocked_status(tmp_path):
    """Consolidated release candidate verification script exits with code 2 on BLOCKED status (WP05-R02)."""
    rc_script = PROJECT_ROOT / "scripts/ci/verify_release_candidate.py"
    # Point frontend dir to non-git directory to force BLOCKED
    non_git_dir = tmp_path / "non_git_dir"
    non_git_dir.mkdir()
    cmd = [
        sys.executable,
        str(rc_script),
        "--backend-dir", str(PROJECT_ROOT),
        "--frontend-dir", str(non_git_dir)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 2

def test_incorrect_cross_repository_pairing(tmp_path):
    """Cross-repo verifier rejects mismatched commit SHAs (WP05-03)."""
    report_file = tmp_path / "cross-repo-report.json"
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scripts/ci/verify_cross_repo_release.py"),
        "--initiator", "backend",
        "--backend-path", str(PROJECT_ROOT),
        "--frontend-path", str(PROJECT_ROOT),
        "--expected-frontend-sha", "0000000000000000000000000000000000000000",
        "--report", str(report_file)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 1
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert data["verdict"] == "FAIL"
    assert any("Frontend SHA mismatch" in d for d in data["details"])

def test_disabled_by_default_publication():
    """Attempting container publication with --publish flag fails closed."""
    cmd = [
        str(PROJECT_ROOT / "scripts/ci/build_and_scan_image.sh"),
        "backend",
        "collectflow-backend-test",
        "/tmp/test-meta.json",
        "--publish"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode != 0
    assert "Automatic external registry publication (GHCR/ECR) is strictly disabled by default" in res.stdout or "Automatic external registry publication (GHCR/ECR) is strictly disabled by default" in res.stderr

def test_disposable_compose_cleanup_trap_executable():
    """Executable negative integration test: verifies disposable compose cleans up containers and temp dir under failure injection (WP05-05)."""
    cmd = [
        str(PROJECT_ROOT / "scripts/ci/test_disposable_compose.sh"),
        "--test-failure-trap"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0
    assert "PASSED: Cleanup trap successfully tore down containers and removed temporary directory" in res.stdout

def test_secret_free_logging(tmp_path):
    """Secret detection scanner masks detected secrets in stdout to prevent log leakage."""
    secret_text = "ghp_" + "FAKE" + "12345678901234567890123456789012345"
    redacted = redact(secret_text)
    assert "REDACTED" in redacted
    assert secret_text not in redacted
    assert redacted.startswith("ghp...")

    test_file = tmp_path / "vulnerable_file.py"
    test_file.write_text(f"API_KEY = '{secret_text}'\n", encoding="utf-8")
    findings = scan_file(test_file)
    assert len(findings) >= 1
    for line_no, desc, masked in findings:
        assert secret_text not in masked
        assert "REDACTED" in masked

def test_blanket_approved_cves_rejection(tmp_path):
    """Bypass using blanket approved_cves array is rejected fail-closed (WP05-F01)."""
    bad_exp_file = tmp_path / "bad_exceptions.json"
    bad_exp_file.write_text(json.dumps({
        "schema_version": "1.0.0",
        "approved_cves": ["CVE-2025-7458", "CVE-2023-45853"],
        "exceptions": []
    }), encoding="utf-8")

    valid_exps, errors = load_and_validate_exceptions(bad_exp_file, "collectflow-backend")
    assert any("Blanket 'approved_cves' list found" in e for e in errors)
    assert any("strictly rejected under WP05-F01" in e for e in errors)

def test_exception_missing_mandatory_fields(tmp_path):
    """Exception missing any required field (e.g. approver, expires_at, approval_reference) fails closed (WP05-F01)."""
    incomplete_rec = {
        "cve_id": "CVE-2026-9999",
        "package_name": "libssl3",
        "affected_image": "collectflow-backend",
        "rationale": "Missing approval fields"
    }
    is_val, err, _ = validate_exception_record(incomplete_rec, "collectflow-backend")
    assert not is_val
    assert "missing mandatory field" in err

def test_exception_expired_date_rejection(tmp_path):
    """Exception with an expired date is rejected fail-closed (WP05-F01)."""
    expired_rec = {
        "cve_id": "CVE-2026-9999",
        "package_name": "libssl3",
        "affected_image": "collectflow-backend",
        "rationale": "Old exception",
        "approver": "SecTeam",
        "approval_reference": "SEC-1234",
        "expires_at": "2020-01-01"
    }
    is_val, err, _ = validate_exception_record(expired_rec, "collectflow-backend")
    assert not is_val
    assert "is EXPIRED" in err

def test_exception_package_and_image_scope_mismatch(tmp_path):
    """Exception for frontend or different package does not suppress backend finding (WP05-F01)."""
    mismatched_img_rec = {
        "cve_id": "CVE-2026-9999",
        "package_name": "libssl3",
        "affected_image": "collectflow-frontend",
        "rationale": "Applies only to frontend",
        "approver": "SecTeam",
        "approval_reference": "SEC-1234",
        "expires_at": "2030-01-01"
    }
    is_val, err, _ = validate_exception_record(mismatched_img_rec, "collectflow-backend")
    assert not is_val
    assert "image scope" in err and "does not match target image" in err

def test_valid_exception_evaluation(tmp_path, monkeypatch):
    """Valid exception with full schema and future expiry properly resolves finding when authorized (WP05-F01, WP05-G01)."""
    monkeypatch.setenv("COLLECTFLOW_EXTERNAL_SECURITY_APPROVAL_TOKEN", "sec_gov_external_auth_token_32chars_long_verified_ok!")
    valid_exp_file = tmp_path / "valid_exceptions.json"
    valid_exp_file.write_text(json.dumps({
        "schema_version": "2.0.0",
        "exceptions": [
            {
                "cve_id": "CVE-2026-1234",
                "package_name": "zlib1g",
                "affected_image": "collectflow-backend",
                "rationale": "Patch pending upstream, mitigated in container",
                "approver": "SecGovernance",
                "approval_reference": "SEC-2026-001",
                "expires_at": "2030-12-31"
            }
        ]
    }), encoding="utf-8")

    trivy_report = tmp_path / "trivy_report.json"
    trivy_report.write_text(json.dumps({
        "SchemaVersion": 2,
        "Results": [
            {
                "Target": "collectflow-backend",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2026-1234",
                        "PkgName": "zlib1g",
                        "Severity": "HIGH"
                    }
                ]
            }
        ]
    }), encoding="utf-8")

    res, errs = evaluate_trivy_report(trivy_report, valid_exp_file, "collectflow-backend")
    assert errs == []
    assert res["verdict"] == "PASS_WITH_APPROVED_EXCEPTIONS"
    assert res["high_count"] == 1
    assert res["unapproved_high_count"] == 0
    assert len(res["applied_exceptions"]) == 1


def test_container_scan_missing_evidence_rejection(tmp_path):
    """Container scan missing evidence_file or checksum is rejected (WP05-F02)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    del manifest["container_artifacts"]["backend"]["security_scan"]["evidence_file"]
    errors = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False)
    assert any("Missing mandatory container scan evidence_file for backend" in e for e in errors)

def test_container_scan_evidence_checksum_mismatch(tmp_path):
    """Tampered or altered container scan report failing SHA-256 is rejected (WP05-F02)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    manifest["container_artifacts"]["backend"]["security_scan"]["evidence_sha256"] = "0" * 64
    errors = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False)
    assert any("backend container scan evidence checksum mismatch" in e for e in errors)

def test_forged_scan_pass_verdict_rejection(tmp_path):
    """Manifest claiming scan_verdict=PASS when Trivy report actually contains HIGH/CRITICAL vulnerabilities is rejected (WP05-F02)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    b_trivy = Path(manifest["container_artifacts"]["backend"]["security_scan"]["evidence_file"])
    b_trivy.write_text(json.dumps({
        "SchemaVersion": 2,
        "Results": [
            {
                "Target": "app",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2026-9999",
                        "PkgName": "libsqlite3-0",
                        "Severity": "CRITICAL"
                    }
                ]
            }
        ]
    }), encoding="utf-8")
    manifest["container_artifacts"]["backend"]["security_scan"]["evidence_sha256"] = hashlib.sha256(b_trivy.read_bytes()).hexdigest()
    manifest["container_artifacts"]["backend"]["scan_verdict"] = "PASS"
    manifest["container_artifacts"]["backend"]["security_scan"]["critical_count"] = 0

    errors = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False)
    assert any("forgery detected" in e or "CRITICAL count mismatch" in e for e in errors)

def test_malformed_trivy_report_rejection(tmp_path):
    """Trivy report missing SchemaVersion or Results is rejected fail-closed (WP05-F02)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    b_trivy = Path(manifest["container_artifacts"]["backend"]["security_scan"]["evidence_file"])
    b_trivy.write_text(json.dumps({"incomplete": True}), encoding="utf-8")
    manifest["container_artifacts"]["backend"]["security_scan"]["evidence_sha256"] = hashlib.sha256(b_trivy.read_bytes()).hexdigest()

    errors = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False)
    assert any("missing required SchemaVersion or Results" in e for e in errors)

def test_verify_release_candidate_fails_when_evidence_missing(tmp_path):
    """verify_release_candidate fails closed (exit code 1) when mandatory evidence files are missing (WP05-F03)."""
    rc_script = PROJECT_ROOT / "scripts/ci/verify_release_candidate.py"
    cmd = [
        sys.executable,
        str(rc_script),
        "--backend-dir", str(PROJECT_ROOT),
        "--frontend-dir", str(PROJECT_ROOT / "../cash-flow-buddy-101"),
        "--backend-test-report", str(tmp_path / "nonexistent-test.json"),
        "--skip-docker-verify",
        "--skip-git-verify"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 1
    assert "Mandatory release candidate quality gate evidence is missing" in res.stderr

def test_self_declared_exceptions_rejected_without_external_token(tmp_path, monkeypatch):
    """Non-empty exceptions in repository JSON are rejected fail-closed without external security token (WP05-G01)."""
    monkeypatch.delenv("COLLECTFLOW_EXTERNAL_SECURITY_APPROVAL_TOKEN", raising=False)
    exp_file = tmp_path / "exceptions.json"
    exp_file.write_text(json.dumps({
        "schema_version": "2.0.0",
        "exceptions": [
            {
                "cve_id": "CVE-2026-1111",
                "package_name": "curl",
                "affected_image": "collectflow-backend",
                "rationale": "Mitigated in container network policy",
                "approver": "SecTeam",
                "approval_reference": "SEC-789",
                "expires_at": "2030-01-01"
            }
        ]
    }), encoding="utf-8")
    valid_exps, errors = load_and_validate_exceptions(exp_file, "collectflow-backend")
    assert len(valid_exps) == 0
    assert any("Self-declared vulnerability exceptions found" in e for e in errors)

def test_wildcard_image_scope_rejected():
    """Wildcard affected_image ('*') is strictly rejected (WP05-G01)."""
    rec = {
        "cve_id": "CVE-2026-1111",
        "package_name": "curl",
        "affected_image": "*",
        "rationale": "Wildcard bypass attempt",
        "approver": "SecTeam",
        "approval_reference": "SEC-789",
        "expires_at": "2030-01-01"
    }
    is_val, err, _ = validate_exception_record(rec, "collectflow-backend")
    assert not is_val
    assert "Wildcard or mismatched image scope" in err

def test_fabricated_approval_token_rejected(tmp_path, monkeypatch):
    """Short or dummy/mock external approval token is strictly rejected (WP05-G01)."""
    for bad_token in ["dummy", "mock_token_12345", "test_token_self_approved", "short_token"]:
        monkeypatch.setenv("COLLECTFLOW_EXTERNAL_SECURITY_APPROVAL_TOKEN", bad_token)
        exp_file = tmp_path / "exceptions.json"
        exp_file.write_text(json.dumps({
            "schema_version": "2.0.0",
            "exceptions": [
                {
                    "cve_id": "CVE-2026-1111",
                    "package_name": "curl",
                    "affected_image": "collectflow-backend",
                    "rationale": "Mitigated",
                    "approver": "SecTeam",
                    "approval_reference": "SEC-789",
                    "expires_at": "2030-01-01"
                }
            ]
        }), encoding="utf-8")
        valid_exps, errors = load_and_validate_exceptions(exp_file, "collectflow-backend")
        assert len(valid_exps) == 0
        assert any("external security approval token" in e.lower() for e in errors)

def test_formal_rc_mode_rejects_diagnostic_bypasses(tmp_path):
    """formal_rc_mode strictly rejects verify_docker_images=False or verify_git_shas=False (WP05-G03)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    errs = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False, formal_rc_mode=True)
    assert any("FORMAL RELEASE CANDIDATE REJECTED" in e and "Docker image verification" in e for e in errs)

def test_missing_scanner_provenance_rejected(tmp_path):
    """Manifest missing scanner_provenance in container security_scan fails validation (WP05-G03)."""
    manifest = create_valid_evidence_bundle(tmp_path)
    del manifest["container_artifacts"]["backend"]["security_scan"]["scanner_provenance"]
    errs = validate_manifest(manifest, verify_git_shas=False, verify_docker_images=False)
    assert any("missing required field 'scanner_provenance'" in e for e in errs)

def test_diagnostic_skip_flags_never_produce_rc_passed(tmp_path):
    """verify_release_candidate.py exits code 3 (RC_SKIPPED_VERIFICATION) and never RC_PASSED when diagnostic skips are present (WP05-G03)."""
    import shutil
    b_repo = tmp_path / "backend_repo"
    f_repo = tmp_path / "frontend_repo"
    b_repo.mkdir()
    f_repo.mkdir()

    subprocess.run(["git", "init"], cwd=str(b_repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=str(b_repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=str(b_repo), check=True, capture_output=True)
    shutil.copytree(PROJECT_ROOT / "scripts", b_repo / "scripts")
    (b_repo / "README.md").write_text("backend")
    subprocess.run(["git", "add", "."], cwd=str(b_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init backend"], cwd=str(b_repo), check=True, capture_output=True)
    b_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(b_repo), capture_output=True, text=True, check=True).stdout.strip()

    subprocess.run(["git", "init"], cwd=str(f_repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=str(f_repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=str(f_repo), check=True, capture_output=True)
    (f_repo / "README.md").write_text("frontend")
    subprocess.run(["git", "add", "."], cwd=str(f_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init frontend"], cwd=str(f_repo), check=True, capture_output=True)
    f_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(f_repo), capture_output=True, text=True, check=True).stdout.strip()

    backend_test = tmp_path / "backend_test.json"
    backend_test.write_text(json.dumps({"passed": 267, "failed": 0, "verdict": "PASS"}))
    frontend_test = tmp_path / "frontend_test.json"
    frontend_test.write_text(json.dumps({"passed": 620, "failed": 0, "verdict": "PASS"}))
    backend_audit = tmp_path / "backend_audit.json"
    backend_audit.write_text(json.dumps({"dependencies": []}))
    frontend_audit = tmp_path / "frontend_audit.json"
    frontend_audit.write_text(json.dumps({"metadata": {"vulnerabilities": {"high": 0, "critical": 0}}}))
    secret_scan = tmp_path / "secret_scan.json"
    secret_scan.write_text(json.dumps({"findings_count": 0, "findings": []}))
    sast_lint = tmp_path / "sast_lint.json"
    sast_lint.write_text(json.dumps({"lint_errors": 0}))

    b_trivy = tmp_path / "b_trivy.json"
    f_trivy = tmp_path / "f_trivy.json"
    trivy_clean = {"SchemaVersion": 2, "Results": [{"Target": "app", "Class": "os-pkgs", "Type": "alpine", "Vulnerabilities": []}]}
    b_trivy.write_text(json.dumps(trivy_clean))
    f_trivy.write_text(json.dumps(trivy_clean))

    backend_meta = tmp_path / "backend_meta.json"
    backend_meta.write_text(json.dumps({
        "image_name": "collectflow-backend",
        "image_tag": f"collectflow-backend:{b_sha}",
        "image_id": "sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
        "oci_revision": b_sha,
        "scan_verdict": "PASS",
        "security_scan": {
            "scanner": "trivy",
            "scanner_version": "Version: 0.76.0",
            "scanner_provenance": "/usr/local/bin/trivy (Version: 0.76.0)",
            "verdict": "PASS",
            "details": "0 CRITICAL and 0 HIGH vulnerabilities detected in container image.",
            "critical_count": 0,
            "high_count": 0,
            "blocking_findings": [],
            "evidence_file": str(b_trivy),
            "evidence_sha256": hashlib.sha256(b_trivy.read_bytes()).hexdigest()
        },
        "publication_status": "DISABLED_BY_DEFAULT",
        "registry_published": False
    }))
    frontend_meta = tmp_path / "frontend_meta.json"
    frontend_meta.write_text(json.dumps({
        "image_name": "collectflow-frontend",
        "image_tag": f"collectflow-frontend:{f_sha}",
        "image_id": "sha256:222233334444555566667777888899990000aaaabbbbccccddddeeeeffff1111",
        "oci_revision": f_sha,
        "scan_verdict": "PASS",
        "security_scan": {
            "scanner": "trivy",
            "scanner_version": "Version: 0.76.0",
            "scanner_provenance": "/usr/local/bin/trivy (Version: 0.76.0)",
            "verdict": "PASS",
            "details": "0 CRITICAL and 0 HIGH vulnerabilities detected in container image.",
            "critical_count": 0,
            "high_count": 0,
            "blocking_findings": [],
            "evidence_file": str(f_trivy),
            "evidence_sha256": hashlib.sha256(f_trivy.read_bytes()).hexdigest()
        },
        "publication_status": "DISABLED_BY_DEFAULT",
        "registry_published": False
    }))

    rc_script = b_repo / "scripts/ci/verify_release_candidate.py"
    cmd = [
        sys.executable,
        str(rc_script),
        "--rc-id", "RC-TEST-DIAGNOSTIC",
        "--backend-dir", str(b_repo),
        "--frontend-dir", str(f_repo),
        "--backend-test-report", str(backend_test),
        "--frontend-test-report", str(frontend_test),
        "--backend-audit-report", str(backend_audit),
        "--frontend-audit-report", str(frontend_audit),
        "--secret-scan-report", str(secret_scan),
        "--sast-lint-report", str(sast_lint),
        "--backend-meta", str(backend_meta),
        "--frontend-meta", str(frontend_meta),
        "--output-manifest", str(tmp_path / "manifest.json"),
        "--skip-docker-verify",
        "--skip-git-verify"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 3
    assert "DIAGNOSTIC NON-ACCEPTANCE" in res.stderr
    assert "FORMAL ACCEPTANCE NOT GRANTED" in res.stderr
    assert "RC_SKIPPED_VERIFICATION" in res.stderr

