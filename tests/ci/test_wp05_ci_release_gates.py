"""
Focused Test Suite for WP-05 CI/CD & Security Gates (WP05-10)
Covers:
- Release manifest validation and schema verification
- Wrong source SHA rejection
- Container digest mismatch rejection
- Missing or corrupted SBOM rejection
- Failed security gate rejection
- Incorrect cross-repository pairing rejection
- Disabled-by-default publication enforcement
- Disposable integration cleanup trap verification
- Secret-free logging and redaction verification
"""

import hashlib
import json
import subprocess
from pathlib import Path
from scripts.ci.validate_release_manifest import validate_manifest
from scripts.ci.scan_secrets import redact, scan_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

def test_release_manifest_valid_structure(tmp_path):
    """Valid release manifest with consistent SHAs, valid digests, and matching SBOM checksums passes."""
    backend_sbom = tmp_path / "backend-sbom.json"
    frontend_sbom = tmp_path / "frontend-sbom.json"
    backend_sbom.write_text('{"components": []}', encoding="utf-8")
    frontend_sbom.write_text('{"components": []}', encoding="utf-8")

    b_sum = hashlib.sha256(backend_sbom.read_bytes()).hexdigest()
    f_sum = hashlib.sha256(frontend_sbom.read_bytes()).hexdigest()

    manifest = {
        "release_candidate_id": "RC-WP05-TEST-VALID",
        "source_traceability": {
            "backend": {"commit_sha": "a" * 40},
            "frontend": {"commit_sha": "b" * 40}
        },
        "container_artifacts": {
            "backend": {
                "image_digest": "sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
                "scan_verdict": "PASS"
            },
            "frontend": {
                "image_digest": "sha256:555566667777888899990000aaaabbbbccccddddeeeeffff1111222233334444",
                "scan_verdict": "PASS"
            }
        },
        "sbom_artifacts": {
            "backend": {"file_path": str(backend_sbom), "sha256_checksum": b_sum},
            "frontend": {"file_path": str(frontend_sbom), "sha256_checksum": f_sum}
        },
        "quality_gates": {
            "backend_regression_tests": {"verdict": "PASS", "failed": 0},
            "frontend_regression_tests": {"verdict": "PASS", "failed": 0},
            "python_dependency_audit": "PASS (0 known vulnerabilities)",
            "npm_dependency_audit": "PASS (0 vulnerabilities)",
            "secret_scanning": "PASS (0 credentials detected)",
            "sast_lint_checks": "PASS"
        },
        "governance_and_approval": {
            "deployment_authorized": False
        }
    }

    errors = validate_manifest(manifest, verify_git_shas=False)
    assert errors == []

def test_wrong_source_sha_rejection(tmp_path):
    """Manifest specifying wrong source SHA is rejected."""
    manifest = {
        "release_candidate_id": "RC-WP05-TEST-WRONG-SHA",
        "source_traceability": {
            "backend": {"commit_sha": "0000000000000000000000000000000000000000"},
            "frontend": {"commit_sha": "1111111111111111111111111111111111111111"}
        },
        "container_artifacts": {
            "backend": {"image_digest": "sha256:aaaa", "scan_verdict": "PASS"},
            "frontend": {"image_digest": "sha256:bbbb", "scan_verdict": "PASS"}
        },
        "sbom_artifacts": {
            "backend": {"file_path": None, "sha256_checksum": None},
            "frontend": {"file_path": None, "sha256_checksum": None}
        },
        "quality_gates": {},
        "governance_and_approval": {"deployment_authorized": False}
    }

    errors = validate_manifest(manifest, backend_dir=PROJECT_ROOT, verify_git_shas=True)
    assert any("Backend source SHA mismatch" in e for e in errors)

def test_digest_mismatch_rejection(tmp_path):
    """Manifest with invalid, empty, or missing container image digest is rejected."""
    manifest = {
        "release_candidate_id": "RC-WP05-TEST-DIGEST",
        "source_traceability": {
            "backend": {"commit_sha": "a" * 40},
            "frontend": {"commit_sha": "b" * 40}
        },
        "container_artifacts": {
            "backend": {"image_digest": "invalid-digest", "scan_verdict": "PASS"},
            "frontend": {"image_digest": "sha256:unknown", "scan_verdict": "PASS"}
        },
        "sbom_artifacts": {},
        "quality_gates": {},
        "governance_and_approval": {"deployment_authorized": False}
    }

    errors = validate_manifest(manifest, verify_git_shas=False)
    assert any("Invalid or missing backend container image digest" in e for e in errors)
    assert any("Invalid or missing frontend container image digest" in e for e in errors)

def test_missing_sbom_rejection(tmp_path):
    """Manifest referencing non-existent SBOM or mismatched checksum is rejected."""
    non_existent = tmp_path / "missing-sbom.json"
    existing_sbom = tmp_path / "existing-sbom.json"
    existing_sbom.write_text('{"components": []}', encoding="utf-8")

    manifest = {
        "release_candidate_id": "RC-WP05-TEST-SBOM",
        "source_traceability": {
            "backend": {"commit_sha": "a" * 40},
            "frontend": {"commit_sha": "b" * 40}
        },
        "container_artifacts": {
            "backend": {"image_digest": "sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff", "scan_verdict": "PASS"},
            "frontend": {"image_digest": "sha256:555566667777888899990000aaaabbbbccccddddeeeeffff1111222233334444", "scan_verdict": "PASS"}
        },
        "sbom_artifacts": {
            "backend": {"file_path": str(non_existent), "sha256_checksum": "dummy"},
            "frontend": {"file_path": str(existing_sbom), "sha256_checksum": "incorrect_checksum"}
        },
        "quality_gates": {},
        "governance_and_approval": {"deployment_authorized": False}
    }

    errors = validate_manifest(manifest, verify_git_shas=False)
    assert any("Referenced backend SBOM file does not exist" in e for e in errors)
    assert any("frontend SBOM checksum mismatch" in e for e in errors)

def test_failed_security_gate_rejection(tmp_path):
    """Manifest with failed dependency audits, secret scanning, or image scans is rejected."""
    manifest = {
        "release_candidate_id": "RC-WP05-TEST-GATES",
        "source_traceability": {
            "backend": {"commit_sha": "a" * 40},
            "frontend": {"commit_sha": "b" * 40}
        },
        "container_artifacts": {
            "backend": {"image_digest": "sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff", "scan_verdict": "FAIL_CRITICAL_VULNERABILITY"},
            "frontend": {"image_digest": "sha256:555566667777888899990000aaaabbbbccccddddeeeeffff1111222233334444", "scan_verdict": "PASS"}
        },
        "sbom_artifacts": {},
        "quality_gates": {
            "backend_regression_tests": {"verdict": "PASS", "failed": 0},
            "frontend_regression_tests": {"verdict": "FAIL", "failed": 2},
            "python_dependency_audit": "FAIL (1 Critical vulnerability)",
            "npm_dependency_audit": "PASS (0 vulnerabilities)",
            "secret_scanning": "FAIL (1 secret detected)",
            "sast_lint_checks": "PASS"
        },
        "governance_and_approval": {"deployment_authorized": False}
    }

    errors = validate_manifest(manifest, verify_git_shas=False)
    assert any("backend container vulnerability scan failed" in e for e in errors)
    assert any("Frontend regression tests failed" in e for e in errors)
    assert any("python_dependency_audit" in e for e in errors)
    assert any("secret_scanning" in e for e in errors)

def test_incorrect_cross_repository_pairing(tmp_path):
    """Cross-repo verifier rejects mismatched commit SHAs."""
    report_file = tmp_path / "cross-repo-report.json"
    cmd = [
        "python3",
        str(PROJECT_ROOT / "scripts/ci/verify_cross_repo_release.py"),
        "--initiator", "backend",
        "--backend-path", str(PROJECT_ROOT),
        "--frontend-path", str(PROJECT_ROOT),  # Deliberately point both to backend to simulate mismatch
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

def test_disposable_compose_cleanup_trap():
    """Disposable compose test runner contains robust shell cleanup trap."""
    script_text = (PROJECT_ROOT / "scripts/ci/test_disposable_compose.sh").read_text(encoding="utf-8")
    assert "trap cleanup EXIT INT TERM ERR" in script_text
    assert "down -v --remove-orphans" in script_text

def test_secret_free_logging(tmp_path):
    """Secret detection scanner masks detected secrets in stdout to prevent log leakage."""
    secret_text = "ghp_ABC12345678901234567890123456789012345"
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
