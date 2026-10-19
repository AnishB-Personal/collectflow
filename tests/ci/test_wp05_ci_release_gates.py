"""
Comprehensive Test Suite for WP-05 CI/CD & Security Gates
Covers all review corrections (WP05-01 through WP05-06):
- Elimination of fabricated release evidence (structured evidence ingestion)
- Rejection of arbitrary string pass values & missing evidence
- Rejection of dirty working trees
- Fail-closed container scan verdicts & OCI revision label validation
- Cross-repository pairing verification and rejection of BLOCKED status for release candidates
- Executable negative integration check for disposable compose cleanup under failure injection
- Disabled-by-default publication enforcement
- Secret scanner log redaction
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from scripts.ci.validate_release_manifest import validate_manifest
from scripts.ci.scan_secrets import redact, scan_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

def test_release_manifest_valid_structure(tmp_path):
    """Valid release manifest with structured quality gates, verified SHAs, OCI revisions, and matching SBOMs passes."""
    backend_sbom = tmp_path / "backend-sbom.json"
    frontend_sbom = tmp_path / "frontend-sbom.json"
    backend_sbom.write_text('{"components": []}', encoding="utf-8")
    frontend_sbom.write_text('{"components": []}', encoding="utf-8")

    b_sum = hashlib.sha256(backend_sbom.read_bytes()).hexdigest()
    f_sum = hashlib.sha256(frontend_sbom.read_bytes()).hexdigest()

    b_sha = "a" * 40
    f_sha = "b" * 40

    manifest = {
        "release_candidate_id": "RC-WP05-TEST-VALID",
        "source_traceability": {
            "backend": {"commit_sha": b_sha, "clean_worktree": True},
            "frontend": {"commit_sha": f_sha, "clean_worktree": True}
        },
        "container_artifacts": {
            "backend": {
                "image_id": "sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
                "oci_revision": b_sha,
                "scan_verdict": "PASS",
                "publication_status": "DISABLED_BY_DEFAULT",
                "registry_published": False
            },
            "frontend": {
                "image_id": "sha256:555566667777888899990000aaaabbbbccccddddeeeeffff1111222233334444",
                "oci_revision": f_sha,
                "scan_verdict": "PASS",
                "publication_status": "DISABLED_BY_DEFAULT",
                "registry_published": False
            }
        },
        "sbom_artifacts": {
            "backend": {"file_path": str(backend_sbom), "sha256_checksum": b_sum},
            "frontend": {"file_path": str(frontend_sbom), "sha256_checksum": f_sum}
        },
        "quality_gates": {
            "backend_regression_tests": {"verdict": "PASS", "passed": 249, "failed": 0},
            "frontend_regression_tests": {"verdict": "PASS", "passed": 620, "failed": 0},
            "python_dependency_audit": {"verdict": "PASS", "vulnerabilities": 0},
            "npm_dependency_audit": {"verdict": "PASS", "vulnerabilities": 0},
            "secret_scanning": {"verdict": "PASS", "findings_count": 0},
            "sast_lint_checks": {"verdict": "PASS", "lint_errors": 0},
            "cross_repo_pairing": {"verdict": "PASS", "pairing_status": "VERIFIED_PAIR"}
        },
        "governance_and_approval": {
            "deployment_authorized": False
        }
    }

    errors = validate_manifest(manifest, verify_git_shas=False)
    assert errors == []

def test_dirty_worktree_rejection(tmp_path):
    """Manifest indicating dirty worktrees is rejected (WP05-01)."""
    manifest = {
        "release_candidate_id": "RC-WP05-DIRTY",
        "source_traceability": {
            "backend": {"commit_sha": "a" * 40, "clean_worktree": False},
            "frontend": {"commit_sha": "b" * 40, "clean_worktree": True}
        },
        "container_artifacts": {},
        "sbom_artifacts": {},
        "quality_gates": {},
        "governance_and_approval": {"deployment_authorized": False}
    }
    errors = validate_manifest(manifest, verify_git_shas=False)
    assert any("Backend source working tree is dirty" in e for e in errors)

def test_arbitrary_string_pass_verdict_rejection():
    """Manifest validator rejects arbitrary string pass values; quality gates must be structured objects (WP05-01)."""
    manifest = {
        "release_candidate_id": "RC-WP05-STRING-VERDICT",
        "source_traceability": {
            "backend": {"commit_sha": "a" * 40, "clean_worktree": True},
            "frontend": {"commit_sha": "b" * 40, "clean_worktree": True}
        },
        "container_artifacts": {
            "backend": {"image_id": "sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff", "oci_revision": "a" * 40, "scan_verdict": "PASS", "publication_status": "DISABLED_BY_DEFAULT", "registry_published": False},
            "frontend": {"image_id": "sha256:555566667777888899990000aaaabbbbccccddddeeeeffff1111222233334444", "oci_revision": "b" * 40, "scan_verdict": "PASS", "publication_status": "DISABLED_BY_DEFAULT", "registry_published": False}
        },
        "sbom_artifacts": {},
        "quality_gates": {
            "backend_regression_tests": "PASS (hardcoded fabricated string)",
            "frontend_regression_tests": "PASS",
            "python_dependency_audit": "PASS (0 known vulnerabilities)",
            "npm_dependency_audit": "PASS (0 vulnerabilities)",
            "secret_scanning": "PASS (0 credentials detected)",
            "sast_lint_checks": "PASS",
            "cross_repo_pairing": "PASS"
        },
        "governance_and_approval": {"deployment_authorized": False}
    }
    errors = validate_manifest(manifest, verify_git_shas=False)
    assert any("Backend regression tests evidence is missing or not a structured object" in e for e in errors)
    assert any("Python dependency audit evidence is missing or not a structured object" in e for e in errors)
    assert any("Cross-repository pairing report evidence is missing or not a structured object" in e for e in errors)

def test_missing_evidence_rejection():
    """Manifest with missing evidence or failed test counts is rejected (WP05-01)."""
    manifest = {
        "release_candidate_id": "RC-WP05-MISSING-EVID",
        "source_traceability": {
            "backend": {"commit_sha": "a" * 40, "clean_worktree": True},
            "frontend": {"commit_sha": "b" * 40, "clean_worktree": True}
        },
        "container_artifacts": {
            "backend": {"image_id": "sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff", "oci_revision": "a" * 40, "scan_verdict": "PASS", "publication_status": "DISABLED_BY_DEFAULT", "registry_published": False},
            "frontend": {"image_id": "sha256:555566667777888899990000aaaabbbbccccddddeeeeffff1111222233334444", "oci_revision": "b" * 40, "scan_verdict": "PASS", "publication_status": "DISABLED_BY_DEFAULT", "registry_published": False}
        },
        "sbom_artifacts": {},
        "quality_gates": {
            "backend_regression_tests": {"verdict": "BLOCKED_EVIDENCE_UNAVAILABLE", "passed": None, "failed": None},
            "frontend_regression_tests": {"verdict": "PASS", "passed": 620, "failed": 3},
            "python_dependency_audit": {"verdict": "BLOCKED_EVIDENCE_UNAVAILABLE"},
            "npm_dependency_audit": {"verdict": "PASS", "vulnerabilities": 1},
            "secret_scanning": {"verdict": "FAIL", "findings_count": 2},
            "sast_lint_checks": {"verdict": "FAIL", "lint_errors": 4},
            "cross_repo_pairing": {"verdict": "FAIL", "pairing_status": "UNKNOWN"}
        },
        "governance_and_approval": {"deployment_authorized": False}
    }
    errors = validate_manifest(manifest, verify_git_shas=False)
    assert any("Backend regression tests failed" in e for e in errors)
    assert any("Frontend regression tests failed" in e for e in errors)
    assert any("Python dependency audit failed" in e for e in errors)
    assert any("NPM dependency audit failed" in e for e in errors)
    assert any("Secret scanning gate failed" in e for e in errors)
    assert any("SAST / lint gate failed" in e for e in errors)

def test_cross_repo_blocked_status_rejected_for_release_candidate():
    """In CI, cross-repo gate may be BLOCKED due to single-repo credentials, but manifest validation must reject it for release candidate acceptance (WP05-03)."""
    manifest = {
        "release_candidate_id": "RC-WP05-BLOCKED-PAIRING",
        "source_traceability": {
            "backend": {"commit_sha": "a" * 40, "clean_worktree": True},
            "frontend": {"commit_sha": "b" * 40, "clean_worktree": True}
        },
        "container_artifacts": {
            "backend": {"image_id": "sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff", "oci_revision": "a" * 40, "scan_verdict": "PASS", "publication_status": "DISABLED_BY_DEFAULT", "registry_published": False},
            "frontend": {"image_id": "sha256:555566667777888899990000aaaabbbbccccddddeeeeffff1111222233334444", "oci_revision": "b" * 40, "scan_verdict": "PASS", "publication_status": "DISABLED_BY_DEFAULT", "registry_published": False}
        },
        "sbom_artifacts": {},
        "quality_gates": {
            "backend_regression_tests": {"verdict": "PASS", "passed": 249, "failed": 0},
            "frontend_regression_tests": {"verdict": "PASS", "passed": 620, "failed": 0},
            "python_dependency_audit": {"verdict": "PASS", "vulnerabilities": 0},
            "npm_dependency_audit": {"verdict": "PASS", "vulnerabilities": 0},
            "secret_scanning": {"verdict": "PASS", "findings_count": 0},
            "sast_lint_checks": {"verdict": "PASS", "lint_errors": 0},
            "cross_repo_pairing": {
                "verdict": "BLOCKED",
                "pairing_status": "BLOCKED_PENDING_CROSS_REPO_CREDENTIALS"
            }
        },
        "governance_and_approval": {"deployment_authorized": False}
    }
    errors = validate_manifest(manifest, verify_git_shas=False)
    assert any("Cross-repository integration gate is BLOCKED" in e for e in errors)
    assert any("Release candidate cannot be accepted without verified dual-repository source pairing" in e for e in errors)

def test_oci_revision_mismatch_rejection():
    """Manifest specifying container with OCI revision label not matching source commit SHA is rejected (WP05-04)."""
    manifest = {
        "release_candidate_id": "RC-WP05-OCI-MISMATCH",
        "source_traceability": {
            "backend": {"commit_sha": "a" * 40, "clean_worktree": True},
            "frontend": {"commit_sha": "b" * 40, "clean_worktree": True}
        },
        "container_artifacts": {
            "backend": {
                "image_id": "sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
                "oci_revision": "c" * 40,  # Mismatched!
                "scan_verdict": "PASS",
                "publication_status": "DISABLED_BY_DEFAULT",
                "registry_published": False
            },
            "frontend": {
                "image_id": "sha256:555566667777888899990000aaaabbbbccccddddeeeeffff1111222233334444",
                "oci_revision": "b" * 40,
                "scan_verdict": "PASS",
                "publication_status": "DISABLED_BY_DEFAULT",
                "registry_published": False
            }
        },
        "sbom_artifacts": {},
        "quality_gates": {},
        "governance_and_approval": {"deployment_authorized": False}
    }
    errors = validate_manifest(manifest, verify_git_shas=False)
    assert any("backend OCI revision mismatch" in e for e in errors)

def test_container_scan_verdict_fail_closed(tmp_path):
    """Manifest with failed, blocked, or unknown scanner verdict is rejected (WP05-02)."""
    manifest = {
        "release_candidate_id": "RC-WP05-SCAN-FAIL",
        "source_traceability": {
            "backend": {"commit_sha": "a" * 40, "clean_worktree": True},
            "frontend": {"commit_sha": "b" * 40, "clean_worktree": True}
        },
        "container_artifacts": {
            "backend": {
                "image_id": "sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
                "oci_revision": "a" * 40,
                "scan_verdict": "BLOCKED_SCANNER_UNAVAILABLE",
                "publication_status": "DISABLED_BY_DEFAULT",
                "registry_published": False
            },
            "frontend": {
                "image_id": "sha256:555566667777888899990000aaaabbbbccccddddeeeeffff1111222233334444",
                "oci_revision": "b" * 40,
                "scan_verdict": "FAIL_CRITICAL_VULNERABILITY",
                "publication_status": "DISABLED_BY_DEFAULT",
                "registry_published": False
            }
        },
        "sbom_artifacts": {},
        "quality_gates": {},
        "governance_and_approval": {"deployment_authorized": False}
    }
    errors = validate_manifest(manifest, verify_git_shas=False)
    assert any("backend container vulnerability scan did not pass verified gate" in e for e in errors)
    assert any("frontend container vulnerability scan did not pass verified gate" in e for e in errors)

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
