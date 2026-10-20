"""
Unit and Negative Acceptance Tests for CollectFlow RCP-01 Release Candidate Preflight.

Verifies all 10 negative criteria specified in Section 12 of RCP-01:
1. Missing raw evidence file causes BLOCKED or FAIL.
2. Source SHA mismatch between declared release candidate and evidence causes verification failure.
3. Dirty source tree causes preflight rejection.
4. Diagnostic bypass option causes non-acceptance classification (DIAGNOSTIC_NON_ACCEPTANCE).
5. Incomplete regression test count or failed test causes BLOCKED.
6. Checksum mismatch in raw evidence payload causes tampering detection.
7. Missing GitHub-hosted release candidate run evidence causes GITHUB_HOSTED_RC to be marked BLOCKED.
8. Stale or mismatched financial acceptance evidence causes rejection.
9. Self-declared Finance approval without corporate officer record causes rejection.
10. Missing test account isolation causes AWS_TEST_READINESS to not be UNCONDITIONAL_GO.
11. Verification that container publication and cloud deployment remain disabled.
"""

import copy
import json
from pathlib import Path
from typing import Tuple
import pytest

from scripts.release.run_rcp01_preflight import (
    ACCEPTED_WP06_BACKEND_SHA,
    ACCEPTED_WP06_FRONTEND_SHA,
    EXPECTED_ALEMBIC_REVISION,
    assess_aws_test_readiness,
    compute_file_sha256,
    evaluate_business_governance,
    probe_github_hosted_rc,
    verify_git_ancestry,
    verify_rcp01_evidence_manifest,
)


@pytest.fixture
def valid_rcp01_manifest(tmp_path: Path) -> Tuple[Path, dict]:
    """Creates a fully valid synthetic RCP-01 master evidence manifest and mock raw evidence files."""
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    inventory = {}

    # Write valid backend JUnit XML
    b_xml = artifacts_dir / "backend_pytest_report.xml"
    b_xml.write_text('<testsuites><testsuite name="pytest" tests="305" failures="0" errors="0" skipped="0"></testsuite></testsuites>', encoding="utf-8")
    inventory["backend_pytest_report_xml"] = {
        "path": str(b_xml),
        "sha256": compute_file_sha256(b_xml),
        "producing_tool": "pytest",
        "status": "PASS",
    }

    # Write backend dependency audit
    b_aud = artifacts_dir / "backend_audit_report.json"
    b_aud.write_text(json.dumps({"dependencies": [{"name": "fastapi", "version": "0.110.0", "vulns": []}]}), encoding="utf-8")
    inventory["backend_audit_report"] = {
        "path": str(b_aud),
        "sha256": compute_file_sha256(b_aud),
        "producing_tool": "pip_audit",
        "status": "PASS",
    }

    # Write frontend dependency audit
    f_aud = artifacts_dir / "frontend_audit_report.json"
    f_aud.write_text(json.dumps({"metadata": {"vulnerabilities": {"info": 0, "low": 0, "moderate": 0, "high": 0, "critical": 0, "total": 0}}}), encoding="utf-8")
    inventory["frontend_audit_report"] = {
        "path": str(f_aud),
        "sha256": compute_file_sha256(f_aud),
        "producing_tool": "npm_audit",
        "status": "PASS",
    }

    # Write SAST report
    sast_file = artifacts_dir / "sast_lint_report.json"
    sast_file.write_text(json.dumps({"scanner_provenance": "bandit_and_ruff", "bandit": {"results": []}, "ruff": {"results": []}}), encoding="utf-8")
    inventory["sast_lint_report"] = {
        "path": str(sast_file),
        "sha256": compute_file_sha256(sast_file),
        "producing_tool": "bandit_and_ruff_sast",
        "status": "PASS",
    }

    # Write Trivy reports
    b_trivy = artifacts_dir / "backend_trivy_report.json"
    b_trivy.write_text(json.dumps({"SchemaVersion": 2, "ArtifactName": "collectflow-backend", "Results": []}), encoding="utf-8")
    inventory["backend_trivy_report"] = {
        "path": str(b_trivy),
        "sha256": compute_file_sha256(b_trivy),
        "producing_tool": "trivy-0.76.0",
        "status": "PASS",
    }

    f_trivy = artifacts_dir / "frontend_trivy_report.json"
    f_trivy.write_text(json.dumps({"SchemaVersion": 2, "ArtifactName": "collectflow-frontend", "Results": []}), encoding="utf-8")
    inventory["frontend_trivy_report"] = {
        "path": str(f_trivy),
        "sha256": compute_file_sha256(f_trivy),
        "producing_tool": "trivy-0.76.0",
        "status": "PASS",
    }

    dummy_artifacts = [
        ("backend_test_evidence.json", "backend_test_evidence_json"),
        ("frontend_test_summary.json", "frontend_test_summary_json"),
        ("secret_scan_report.json", "secret_scan_report_json"),
        ("collectflow-backend-sbom.json", "backend_cyclonedx_sbom"),
        ("collectflow-frontend-sbom.json", "frontend_cyclonedx_sbom"),
        ("backend_image_meta.json", "backend_image_metadata"),
        ("frontend_image_meta.json", "frontend_image_metadata"),
        ("cross_repo_report.json", "cross_repo_pairing_report"),
        ("release-manifest.json", "release_candidate_manifest"),
    ]

    for filename, key in dummy_artifacts:
        file_path = artifacts_dir / filename
        file_path.write_text(f"dummy content for {filename}", encoding="utf-8")
        inventory[key] = {
            "path": str(file_path),
            "sha256": compute_file_sha256(file_path),
            "producing_tool": "test_generator",
            "status": "PASS",
        }

    # Financial readiness manifest with matching revisions
    fin_manifest_path = artifacts_dir / "financial_readiness_manifest.json"
    fin_data = {
        "manifest_type": "COLLECTFLOW_WP06_FINANCIAL_READINESS",
        "source_revisions": {
            "backend_sha": ACCEPTED_WP06_BACKEND_SHA,
            "frontend_sha": ACCEPTED_WP06_FRONTEND_SHA,
        },
        "technical_financial_integrity": {"verdict": "PASS"},
    }
    fin_manifest_path.write_text(json.dumps(fin_data, indent=2), encoding="utf-8")
    inventory["financial_readiness_manifest"] = {
        "path": str(fin_manifest_path),
        "sha256": compute_file_sha256(fin_manifest_path),
        "producing_tool": "verify_financial_readiness.py",
        "status": "PASS_WITH_BUSINESS_BLOCKED",
    }

    manifest = {
        "manifest_type": "COLLECTFLOW_RCP01_RELEASE_EVIDENCE_INDEX",
        "release_candidate_id": "RC-20261020-01",
        "source_freeze_timestamp_utc": "2026-10-20T10:00:00Z",
        "diagnostic_mode": False,
        "source_integrity": {
            "backend_sha": ACCEPTED_WP06_BACKEND_SHA,
            "frontend_sha": ACCEPTED_WP06_FRONTEND_SHA,
            "backend_branch": "feature/release-rcp01-evidence-preflight",
            "frontend_branch": "feature/release-rcp01-evidence-preflight",
            "backend_ancestor_wp06": ACCEPTED_WP06_BACKEND_SHA,
            "frontend_ancestor_wp06": ACCEPTED_WP06_FRONTEND_SHA,
            "working_trees_clean": True,
        },
        "component_ci": {
            "backend_pytest": {
                "total": 305,
                "passed": 305,
                "skipped": 0,
                "failed": 0,
                "verdict": "PASS",
            },
            "frontend_vitest": {
                "test_files_passed": 44,
                "tests_passed": 620,
                "tests_failed": 0,
                "verdict": "PASS",
            },
            "frontend_typescript": {"status": "PASS"},
            "database_schema": {"schema_revision": EXPECTED_ALEMBIC_REVISION, "status": "PASS"},
        },
        "consolidated_rc_execution": {
            "cross_repo_pairing": "VERIFIED_PAIR",
            "release_manifest_validation": "RC_PASSED",
            "publication_disabled": True,
        },
        "security_findings": {
            "secret_scanning": {"findings": 0, "status": "PASS"},
            "dependency_vulnerabilities": {"active_exceptions": 0, "status": "PASS"},
            "container_vulnerability_scan": {"status": "PASS"},
        },
        "oci_image_verification": {
            "backend_image": f"collectflow-backend:{ACCEPTED_WP06_BACKEND_SHA}",
            "frontend_image": f"collectflow-frontend:{ACCEPTED_WP06_FRONTEND_SHA}",
            "publication_status": "DISABLED_BY_DEFAULT",
        },
        "financial_acceptance": {
            "reconciliation_level": "FULL_FIVE_SOURCE",
            "sources": [
                "1. PostgreSQL Database (collectflow_test)",
                "2. REST API (/api/v1/reporting/cross-layer-reconciliation)",
                "3. Browser DOM (TanStack Start SSR UI)",
                "4. XLSX Export (/api/v1/reporting/export/xlsx)",
                "5. CSV Export (/api/v1/reporting/export/csv)",
            ],
            "metrics_reconciled": 10,
            "maximum_delta": "₹0.00",
            "technical_financial_integrity": "PASS",
        },
        "business_approvals": {
            "bpd01_status": "AWAITING_FINANCE_APPROVAL",
            "bpd02_status": "AWAITING_FINANCE_APPROVAL",
            "business_acceptance_checklist_status": "AWAITING_FINANCE_APPROVAL",
            "statutory_prod_config": "INACTIVE / NOT AUTHORIZED",
            "corporate_finance_verdict": "BLOCKED",
            "business_release_verdict": "NO_GO",
        },
        "github_hosted_rc_gate": {
            "status": "BLOCKED",
            "is_indexed_on_default_branch": False,
            "blocking_reason": "Workflow 'release-candidate.yml' is not indexed on default branch 'main'.",
        },
        "aws_test_preflight_readiness": {
            "verdict": "NO_GO",
            "compliant_count": 2,
            "criteria_count": 12,
            "zero_aws_provisioning_enforced": True,
        },
        "artifact_inventory": inventory,
        "go_no_go_decision_matrix": {
            "technical_application_release_candidate": {
                "recommendation": "GO",
                "basis": "All local automated technical verification gates satisfied.",
            },
            "corporate_business_release": {
                "recommendation": "NO_GO",
                "basis": "Statutory policies remain AWAITING_FINANCE_APPROVAL.",
            },
            "first_isolated_aws_test_deployment": {
                "recommendation": "NO_GO",
                "basis": "Only 2/12 architectural criteria compliant. Missing Terraform in deploy/ and unprovisioned test account boundaries.",
            },
        },
        "developer_recommendation": "READY FOR INDEPENDENT RE-REVIEW",
    }

    manifest_file = tmp_path / "rcp01_release_evidence_manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_file, manifest


def test_00_valid_manifest_passes_verification(valid_rcp01_manifest):
    """Test 0: A complete, untampered manifest with valid evidence passes verification."""
    manifest_file, manifest = valid_rcp01_manifest
    is_valid, errors = verify_rcp01_evidence_manifest(
        manifest_file,
        expected_backend_sha=ACCEPTED_WP06_BACKEND_SHA,
        expected_frontend_sha=ACCEPTED_WP06_FRONTEND_SHA,
        check_disk_files=True,
    )
    assert is_valid, f"Expected manifest to pass, but got errors: {errors}"
    assert len(errors) == 0


def test_01_missing_raw_evidence_file_fails_verification(valid_rcp01_manifest):
    """Test 1: Missing raw evidence file causes overall verification to fail."""
    manifest_file, manifest = valid_rcp01_manifest

    # Delete one raw evidence file
    inv = manifest["artifact_inventory"]
    backend_xml_path = Path(inv["backend_pytest_report_xml"]["path"])
    backend_xml_path.unlink()

    is_valid, errors = verify_rcp01_evidence_manifest(manifest_file, check_disk_files=True)
    assert not is_valid
    assert any("Missing raw evidence file" in err for err in errors)


def test_02_source_sha_mismatch_fails_verification(valid_rcp01_manifest):
    """Test 2: Source SHA mismatch between declared candidate and manifest causes verification failure."""
    manifest_file, manifest = valid_rcp01_manifest
    wrong_backend_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    is_valid, errors = verify_rcp01_evidence_manifest(
        manifest_file,
        expected_backend_sha=wrong_backend_sha,
        expected_frontend_sha=ACCEPTED_WP06_FRONTEND_SHA,
    )
    assert not is_valid
    assert any("Backend SHA mismatch" in err for err in errors)


def test_03_dirty_source_tree_rejection(valid_rcp01_manifest):
    """Test 3: Dirty source tree recorded in manifest causes verification rejection."""
    manifest_file, manifest = valid_rcp01_manifest

    manifest["source_integrity"]["working_trees_clean"] = False
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    is_valid, errors = verify_rcp01_evidence_manifest(manifest_file)
    assert not is_valid
    assert any("Source working trees were not clean" in err for err in errors)


def test_04_diagnostic_bypass_causes_non_acceptance(valid_rcp01_manifest):
    """Test 4: Diagnostic bypass causes DIAGNOSTIC_NON_ACCEPTANCE classification and verification failure."""
    manifest_file, manifest = valid_rcp01_manifest

    manifest["diagnostic_mode"] = True
    manifest["go_no_go_decision_matrix"]["technical_application_release_candidate"]["recommendation"] = "DIAGNOSTIC_NON_ACCEPTANCE"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    is_valid, errors = verify_rcp01_evidence_manifest(manifest_file)
    assert not is_valid
    assert any("diagnostic execution" in err for err in errors)


def test_05_incomplete_regression_or_failed_test_fails(valid_rcp01_manifest):
    """Test 5: Incomplete regression count or failed test causes verification failure."""
    manifest_file, manifest = valid_rcp01_manifest

    # Incomplete test count (< 300)
    manifest["component_ci"]["backend_pytest"]["total"] = 100
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    is_valid, errors = verify_rcp01_evidence_manifest(manifest_file)
    assert not is_valid
    assert any("Incomplete backend pytest regression" in err for err in errors)

    # Failed test in suite
    manifest["component_ci"]["backend_pytest"]["total"] = 305
    manifest["component_ci"]["backend_pytest"]["failed"] = 1
    manifest["component_ci"]["backend_pytest"]["verdict"] = "FAIL"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    is_valid, errors = verify_rcp01_evidence_manifest(manifest_file)
    assert not is_valid
    assert any("Backend pytest did not PASS" in err for err in errors)


def test_06_checksum_mismatch_detects_tampering(valid_rcp01_manifest):
    """Test 6: Checksum mismatch in raw evidence payload causes tampering detection."""
    manifest_file, manifest = valid_rcp01_manifest

    # Tamper with the raw backend evidence file content
    inv = manifest["artifact_inventory"]
    backend_ev_path = Path(inv["backend_test_evidence_json"]["path"])
    backend_ev_path.write_text("TAMPERED CONTENT", encoding="utf-8")

    is_valid, errors = verify_rcp01_evidence_manifest(manifest_file, check_disk_files=True)
    assert not is_valid
    assert any("tampering detected" in err for err in errors)


def test_07_missing_github_hosted_run_evidence_marked_blocked():
    """Test 7: Missing GitHub-hosted release candidate execution causes GITHUB_HOSTED_RC to be marked BLOCKED."""
    # Probe nonexistent or unindexed repository/workflow
    res = probe_github_hosted_rc("AnishB-Personal/collectflow")
    assert res["status"] == "BLOCKED"
    assert res["is_indexed_on_default_branch"] is False
    assert "release-candidate.yml" in res["workflow_file"]
    assert res["prohibit_premature_main_merge"] is True


def test_08_stale_or_mismatched_financial_evidence_rejected(valid_rcp01_manifest):
    """Test 8: Stale or mismatched financial acceptance evidence is rejected."""
    manifest_file, manifest = valid_rcp01_manifest

    # Point financial readiness manifest to an older/stale Git SHA
    fin_path = Path(manifest["artifact_inventory"]["financial_readiness_manifest"]["path"])
    stale_fin_data = {
        "manifest_type": "COLLECTFLOW_WP06_FINANCIAL_READINESS",
        "source_revisions": {
            "backend_sha": "0000000000000000000000000000000000000000",
            "frontend_sha": ACCEPTED_WP06_FRONTEND_SHA,
        },
        "technical_financial_integrity": {"verdict": "PASS"},
    }
    fin_path.write_text(json.dumps(stale_fin_data, indent=2), encoding="utf-8")
    # Update checksum in manifest for the changed file so tampering check passes, exposing the SHA mismatch
    manifest["artifact_inventory"]["financial_readiness_manifest"]["sha256"] = compute_file_sha256(fin_path)
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    is_valid, errors = verify_rcp01_evidence_manifest(manifest_file, check_disk_files=True)
    assert not is_valid
    assert any("stale or mismatched financial evidence" in err for err in errors)


def test_09_self_declared_finance_approval_rejected(tmp_path: Path):
    """Test 9: Self-declared Finance approval without corporate officer record causes rejection."""
    fake_backend = tmp_path / "backend"
    finance_docs = fake_backend / "docs" / "finance"
    finance_docs.mkdir(parents=True, exist_ok=True)

    # Developer writes "APPROVED" in BPD01 without authorized officer credentials
    bpd01 = finance_docs / "COLLECTFLOW_BPD01_REFERENCE_MASTERS_REVIEW.md"
    bpd01.write_text("# BPD-01 Review\nStatus: APPROVED\nSigned off by developer.", encoding="utf-8")

    bpd02 = finance_docs / "COLLECTFLOW_BPD02_TAX_CONFIGURATION_REVIEW.md"
    bpd02.write_text("# BPD-02 Review\nStatus: AWAITING_FINANCE_APPROVAL\n", encoding="utf-8")

    checklist = finance_docs / "COLLECTFLOW_BUSINESS_ACCEPTANCE_CHECKLIST.md"
    checklist.write_text("# Checklist\nStatus: AWAITING_FINANCE_APPROVAL\n", encoding="utf-8")

    gov = evaluate_business_governance(fake_backend)
    assert gov["bpd01_status"] == "SELF_DECLARED_REJECTED"
    assert gov["corporate_finance_verdict"] == "SELF_DECLARED_REJECTED"
    assert gov["business_release_verdict"] == "NO_GO"


def test_10_missing_aws_isolation_prevents_unconditional_go():
    """Test 10: Missing AWS isolation authorization prevents an unconditional GO."""
    # When evaluated against deploy directory (or default), missing Terraform and account isolation yields NO_GO
    res = assess_aws_test_readiness()
    assert res["verdict"] == "NO_GO"
    assert len(res["outstanding_conditions"]) >= 3
    assert res["zero_aws_provisioning_enforced"] is True

    # If overridden with non-isolated account boundary, it also yields NO_GO
    res_non_isolated = assess_aws_test_readiness(
        criteria_overrides={"account_boundary": {"compliant": False, "status": "NON_ISOLATED"}}
    )
    assert res_non_isolated["verdict"] == "NO_GO"


def test_11_zero_publication_and_zero_deployment_enforced(valid_rcp01_manifest):
    """Test 11: Validates that container publication and AWS cloud deployment remain disabled."""
    manifest_file, manifest = valid_rcp01_manifest

    assert manifest["consolidated_rc_execution"]["publication_disabled"] is True
    assert manifest["oci_image_verification"]["publication_status"] == "DISABLED_BY_DEFAULT"
    assert manifest["aws_test_preflight_readiness"]["zero_aws_provisioning_enforced"] is True

    # Violating publication disabling causes verification failure
    manifest["consolidated_rc_execution"]["publication_disabled"] = False
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    is_valid, errors = verify_rcp01_evidence_manifest(manifest_file)
    assert not is_valid
    assert any("Container publication must be disabled" in err for err in errors)


def test_12_fabricated_audit_or_false_zero_vulnerability_rejected(valid_rcp01_manifest):
    """Test 12: Fabricated audit JSON or false-zero vulnerability declaration is rejected."""
    manifest_file, manifest = valid_rcp01_manifest
    inv = manifest["artifact_inventory"]

    # 1. Empty placeholder backend audit
    b_aud_path = Path(inv["backend_audit_report"]["path"])
    b_aud_path.write_text(json.dumps({"dependencies": []}), encoding="utf-8")
    inv["backend_audit_report"]["sha256"] = compute_file_sha256(b_aud_path)
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    is_valid, errors = verify_rcp01_evidence_manifest(manifest_file, check_disk_files=True)
    assert not is_valid
    assert any("empty placeholder" in err for err in errors)

    # 2. False-zero vulnerability declaration: raw audit has vulnerabilities, but manifest claims PASS
    b_aud_path.write_text(
        json.dumps({
            "dependencies": [
                {
                    "name": "vulnerable-lib",
                    "version": "1.0.0",
                    "vulns": [{"id": "PYSEC-2026-9999", "fix_versions": ["1.0.1"]}],
                }
            ]
        }),
        encoding="utf-8",
    )
    inv["backend_audit_report"]["sha256"] = compute_file_sha256(b_aud_path)
    manifest["security_findings"]["dependency_vulnerabilities"]["status"] = "PASS"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    is_valid, errors = verify_rcp01_evidence_manifest(manifest_file, check_disk_files=True)
    assert not is_valid
    assert any("false-zero detected" in err for err in errors)


def test_13_missing_terraform_and_security_boundaries_rejects_12_of_12_compliance(valid_rcp01_manifest):
    """Test 13: Reject false 12/12 compliance when Terraform and account isolation are absent."""
    manifest_file, manifest = valid_rcp01_manifest

    # Claiming 12/12 compliance when IaC is not implemented is rejected
    manifest["aws_test_preflight_readiness"]["compliant_count"] = 12
    manifest["aws_test_preflight_readiness"]["criteria_count"] = 12
    manifest["aws_test_preflight_readiness"]["verdict"] = "CONDITIONAL_GO"
    manifest["go_no_go_decision_matrix"]["first_isolated_aws_test_deployment"]["recommendation"] = "CONDITIONAL_GO"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    is_valid, errors = verify_rcp01_evidence_manifest(manifest_file, check_disk_files=True)
    assert not is_valid
    assert any("cannot claim 12/12 compliance" in err for err in errors)

    # Claiming unconditional GO for AWS TEST is also rejected
    manifest["aws_test_preflight_readiness"]["compliant_count"] = 2
    manifest["go_no_go_decision_matrix"]["first_isolated_aws_test_deployment"]["recommendation"] = "GO"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    is_valid, errors = verify_rcp01_evidence_manifest(manifest_file, check_disk_files=True)
    assert not is_valid
    assert any("cannot be unconditional GO" in err for err in errors)
