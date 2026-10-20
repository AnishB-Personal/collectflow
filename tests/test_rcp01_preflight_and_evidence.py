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

    dummy_artifacts = [
        "backend_pytest_report.xml",
        "backend_test_evidence.json",
        "frontend_test_summary.json",
        "secret_scan_report.json",
        "collectflow-backend-sbom.json",
        "collectflow-frontend-sbom.json",
        "backend_image_meta.json",
        "frontend_image_meta.json",
        "cross_repo_report.json",
        "release-manifest.json",
    ]

    inventory = {}
    for filename in dummy_artifacts:
        file_path = artifacts_dir / filename
        file_path.write_text(f"dummy content for {filename}", encoding="utf-8")
        key = filename.replace(".", "_").replace("-", "_")
        if "report_xml" in key:
            key = "backend_pytest_report_xml"
        elif "test_evidence" in key:
            key = "backend_test_evidence_json"
        elif "frontend_test" in key:
            key = "frontend_test_summary_json"
        elif "secret_scan" in key:
            key = "secret_scan_report_json"
        elif "backend_sbom" in key:
            key = "backend_cyclonedx_sbom"
        elif "frontend_sbom" in key:
            key = "frontend_cyclonedx_sbom"
        elif "backend_image" in key:
            key = "backend_image_metadata"
        elif "frontend_image" in key:
            key = "frontend_image_metadata"
        elif "cross_repo" in key:
            key = "cross_repo_pairing_report"
        elif "release_manifest" in key:
            key = "release_candidate_manifest"

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
            "verdict": "CONDITIONAL_GO",
            "compliant_count": 12,
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
                "recommendation": "CONDITIONAL_GO",
                "basis": "12/12 architectural criteria compliant. Zero production provisioning.",
            },
        },
        "developer_recommendation": "READY FOR INDEPENDENT PREFLIGHT REVIEW",
    }

    manifest_file = tmp_path / "rcp01_release_evidence_manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_file, manifest


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
    # When all 12 criteria are compliant, it must yield CONDITIONAL_GO, never unconditional GO
    res = assess_aws_test_readiness()
    assert res["verdict"] == "CONDITIONAL_GO"
    assert len(res["outstanding_conditions"]) >= 3
    assert res["zero_aws_provisioning_enforced"] is True

    # If account boundary isolation is non-compliant, it drops to NO_GO
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
