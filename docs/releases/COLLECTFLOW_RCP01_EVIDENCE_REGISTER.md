# CollectFlow Release Candidate Evidence Register (RCP-01)

## 1. Scope & Purpose

This Evidence Register documents the complete inventory of verifiable, machine-readable artifacts generated during the Release Candidate preflight execution for **RC-20261020-01** following security and preflight remediation.

Every artifact listed below is bound to the frozen source commits of both repositories:
- Backend (`collectflow`): `<BACKEND_FROZEN_SHA>`
- Frontend (`cash-flow-buddy-101`): `<FRONTEND_FROZEN_SHA>`

All artifacts are hashed using SHA-256 and indexed in the master manifest [`artifacts/rcp01/rcp01_release_evidence_manifest.json`](file:///home/anishb/projects/personal/collectflow/artifacts/rcp01/rcp01_release_evidence_manifest.json).

---

## 2. Master Evidence Manifest Summary

| Manifest Property | Specification |
| :--- | :--- |
| **Manifest Identifier** | `COLLECTFLOW_RCP01_RELEASE_EVIDENCE_INDEX` |
| **Release Candidate ID** | `RC-20261020-01` |
| **Backend Commit SHA** | `<BACKEND_FROZEN_SHA>` |
| **Frontend Commit SHA** | `<FRONTEND_FROZEN_SHA>` |
| **Branch (Both Repos)** | `feature/rcp01-security-evidence-remediation` |
| **Backend Ancestor** | `12582f7abb3c391e2d80d61ada179629d7825eac` (WP-06 Accepted) |
| **Frontend Ancestor** | `c6fc51a75135b0343d9c7ad90a80ba5fb5b61260` (WP-06 Accepted) |
| **Master Manifest Location** | `artifacts/rcp01/rcp01_release_evidence_manifest.json` |
| **Master Manifest SHA-256** | `<MASTER_MANIFEST_SHA256>` |
| **Verification Tool** | `python3 scripts/release/run_rcp01_preflight.py --verify-manifest` |

---

## 3. Raw Evidence Artifact Inventory (16 Artifacts)

| Artifact Key | Relative Path / Location | Producing Tool | Status | Checksum (SHA-256) |
| :--- | :--- | :--- | :--- | :--- |
| **backend_pytest_report_xml** | `artifacts/rcp01/backend_pytest_report.xml` | `pytest 9.1.1` | **PASS** (387 total: 316 passed, 71 skipped) | `<BACKEND_PYTEST_XML_SHA>` |
| **backend_test_evidence_json** | `artifacts/rcp01/backend_test_evidence.json` | `generate_backend_test_evidence.py` | **PASS** (0 failures) | `<BACKEND_TEST_EV_SHA>` |
| **frontend_test_summary_json** | `artifacts/rcp01/frontend_test_summary.json` | `vitest v4.1.11` | **PASS** (620 passed) | `<FRONTEND_TEST_EV_SHA>` |
| **secret_scan_report_json** | `artifacts/rcp01/secret_scan_report.json` | `scan_secrets.py` | **PASS** (0 findings) | `<SECRET_SCAN_SHA>` |
| **backend_cyclonedx_sbom** | `artifacts/rcp01/collectflow-backend-sbom.json` | `generate_sbom.py (backend)` | **PASS** (transitive graph) | `<BACKEND_SBOM_SHA>` |
| **frontend_cyclonedx_sbom** | `artifacts/rcp01/collectflow-frontend-sbom.json` | `generate_sbom.py (frontend)` | **PASS** (transitive graph) | `<FRONTEND_SBOM_SHA>` |
| **backend_image_metadata** | `artifacts/rcp01/backend_image_meta.json` | `build_and_scan_image.sh` | **PASS** (no-publish, 0 CVEs) | `<BACKEND_IMAGE_META_SHA>` |
| **frontend_image_metadata** | `artifacts/rcp01/frontend_image_meta.json` | `build_and_scan_image.sh` | **PASS** (no-publish, 0 CVEs) | `<FRONTEND_IMAGE_META_SHA>` |
| **cross_repo_pairing_report** | `artifacts/rcp01/cross_repo_report.json` | `verify_cross_repo_release.py` | **PASS** (verified pair) | `<CROSS_REPO_REPORT_SHA>` |
| **release_candidate_manifest** | `artifacts/rcp01/release-manifest.json` | `generate_release_manifest.py` | **PASS** (RC_VALIDATED) | `<RELEASE_MANIFEST_SHA>` |
| **financial_readiness_manifest**| `artifacts/rcp01/financial/financial_readiness_manifest.json` | `verify_financial_readiness.py` | **PASS** (₹0.00 delta, 5 sources) | `<FINANCIAL_MANIFEST_SHA>` |
| **backend_audit_report** | `artifacts/rcp01/backend_audit_report.json` | `pip-audit v2.7.3` | **PASS** (0 vulnerabilities) | `<BACKEND_AUDIT_SHA>` |
| **frontend_audit_report** | `artifacts/rcp01/frontend_audit_report.json` | `npm audit v10` | **PASS** (0 high/crit) | `<FRONTEND_AUDIT_SHA>` |
| **sast_lint_report** | `artifacts/rcp01/sast_lint_report.json` | `bandit + ruff` | **PASS** (0 security issues) | `<SAST_LINT_SHA>` |
| **backend_trivy_report** | `artifacts/rcp01/backend_trivy_report.json` | `trivy v0.76.0` | **PASS** (0 CVEs) | `<BACKEND_TRIVY_SHA>` |
| **frontend_trivy_report** | `artifacts/rcp01/frontend_trivy_report.json` | `trivy v0.76.0` | **PASS** (0 CVEs) | `<FRONTEND_TRIVY_SHA>` |

---

## 4. Verification and Reproduction Procedures

An independent reviewer can reproduce and verify all evidence using the following commands in sequence:

### 4.1 Master Manifest Verification
Verify the master manifest against disk artifacts and expected Git revisions:
```bash
python3 scripts/release/run_rcp01_preflight.py \
  --verify-manifest artifacts/rcp01/rcp01_release_evidence_manifest.json \
  --expected-backend-sha <BACKEND_FROZEN_SHA> \
  --expected-frontend-sha <FRONTEND_FROZEN_SHA>
```

### 4.2 Full Unit and Negative Test Suite
Execute the 14 unit and negative verification tests proving anti-forgery, checksum enforcement, dirty worktree rejection, anti-fabrication, and governance guardrails:
```bash
venv/bin/python -m pytest tests/test_rcp01_preflight_and_evidence.py -v
```

### 4.3 Clean Source Tree Assertion
Confirm that no files were mutated, untracked, or committed during evidence generation:
```bash
git -C /home/anishb/projects/personal/collectflow status --porcelain
git -C /home/anishb/projects/personal/cash-flow-buddy-101 status --porcelain
```
Both commands must produce zero output.
