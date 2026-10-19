# CollectFlow Portable CI/CD & Security Gates Runner Guide

This directory contains portable automation scripts for executing CollectFlow's build, security, and release verification gates locally on Linux without requiring GitHub Actions.

## Available Scripts

### 1. Secret Scanning (`scan_secrets.py`)
Scans git-tracked files for high-risk secrets, private keys, API tokens, and credentials with log-safe redaction:
```bash
python3 scripts/ci/scan_secrets.py --root .
```

### 2. Software Bill of Materials (SBOM) Generation (`generate_sbom.py`)
Generates CycloneDX 1.4/1.5 JSON SBOMs and SHA-256 checksums:
```bash
# Backend (Python requirements via pip-audit)
python3 scripts/ci/generate_sbom.py --repo-type backend --root . --output /tmp/backend-sbom.json

# Frontend (Node dependencies via npm sbom)
python3 scripts/ci/generate_sbom.py --repo-type frontend --root ../cash-flow-buddy-101 --output /tmp/frontend-sbom.json
```

### 3. Container Build & Digest Inspection (`build_and_scan_image.sh`)
Builds OCI container images with Git commit SHA labels, non-root users, and extracts image digests. Enforces disabled-by-default publication:
```bash
# Backend
./scripts/ci/build_and_scan_image.sh backend collectflow-backend /tmp/backend-meta.json

# Frontend
./scripts/ci/build_and_scan_image.sh frontend collectflow-frontend /tmp/frontend-meta.json
```

### 4. Cross-Repository Release Verification (`verify_cross_repo_release.py`)
Verifies exact commit SHA pairing between backend and frontend repositories:
```bash
python3 scripts/ci/verify_cross_repo_release.py \
    --initiator backend \
    --backend-path . \
    --frontend-path ../cash-flow-buddy-101 \
    --report /tmp/cross-repo-report.json
```

### 5. Release Candidate Manifest Generation & Validation
Generates machine-readable release candidate metadata and validates all signatures, SHAs, digests, and gates:
```bash
# Generate manifest
python3 scripts/ci/generate_release_manifest.py \
    --rc-id "RC-20261019-01" \
    --backend-dir . \
    --frontend-dir ../cash-flow-buddy-101 \
    --backend-image-meta /tmp/backend-meta.json \
    --frontend-image-meta /tmp/frontend-meta.json \
    --backend-sbom /tmp/backend-sbom.json \
    --frontend-sbom /tmp/frontend-sbom.json \
    --output /tmp/release-manifest.json

# Validate manifest (fails closed if any gate, digest, or SHA is invalid)
python3 scripts/ci/validate_release_manifest.py \
    --manifest /tmp/release-manifest.json \
    --backend-dir . \
    --frontend-dir ../cash-flow-buddy-101
```

### 6. Disposable Docker Compose Integration Runner (`test_disposable_compose.sh`)
Executes an end-to-end integration test against a temporary, uniquely named compose stack bound strictly to `127.0.0.1`.
Applies migrations, bootstraps initial administrator, verifies same-origin proxying, tests mandatory password rotation, and tests CSRF-protected mutations:
```bash
./scripts/ci/test_disposable_compose.sh
```
Guarantees clean teardown of containers, networks, and disposable volumes via shell traps on exit or error.
