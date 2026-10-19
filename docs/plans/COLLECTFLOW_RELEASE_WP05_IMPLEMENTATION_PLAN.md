# CollectFlow Release Preparation WP-05 Implementation Plan
## Portable CI/CD, Automated Security Gates & Release Artifact Management

**Author:** Antigravity Agent
**Date:** 2026-10-19
**Status:** Completed (Review Corrections Hardened & Verified)
**Target Branch:** `feature/release-wp05-portable-cicd` (both repositories)
**Backend Baseline:** `b93b6e2c90dbaac422df606c102d85a8d06eaf20`
**Frontend Baseline:** `93e47e0756a80fa1ebf5afd41e9dc0f78655d7ca`

---

## Review Corrections Addressed (WP05-01 through WP05-06)
1. **WP05-01 (Eliminate Fabricated Evidence):** Removed hardcoded test counts, clean worktree flags, and arbitrary string pass values in `generate_release_manifest.py`. Enforced structured evidence ingestion (file + sha256 checksum) and fail-closed validation in `validate_release_manifest.py`.
2. **WP05-02 (Fail-Closed Container Scans):** Removed scanner suppression in `build_and_scan_image.sh`. Enforced fail-closed behavior on missing Trivy or tool errors, scanned HIGH and CRITICAL vulnerabilities, blocked CRITICAL findings, and recorded scanner version + evidence checksum.
3. **WP05-03 (Cross-Repository Gate Results):** Removed `|| true` from CI workflows. Allowed CI private repo limitations to be marked BLOCKED, while strictly rejecting BLOCKED status during release-manifest validation. Verified exact commit SHAs and repository identities.
4. **WP05-04 (OCI Digest Verification):** Distinguished Image Configuration IDs from registry digests. Verified image `org.opencontainers.image.revision` label against exact git commit SHA.
5. **WP05-05 (Disposable Compose Assertions):** Added strict HTTP status code assertions (200, 401, 403), verified database state after mutations, verified stale-session revocation (401), restricted secret permissions (`0600`), masked credentials in logs, and added executable `--test-failure-trap` integration test.
6. **WP05-06 (CI & Supply-Chain Controls):** Executed portable Playwright mock checks in frontend CI, recorded live browser and HTTPS SSL tests as `NOT_EXECUTED` with environmental justification, pinned third-party actions to immutable commit SHAs, and attached structured reports.

---

## 1. Executive Summary & Objective

The objective of WP-05 is to establish a deterministic, portable build and verification process across both CollectFlow repositories (`collectflow` backend and `cash-flow-buddy-101` frontend) without cloud infrastructure dependencies or manual intervention.

Key deliverables:
1. **WP05-01: Backend CI Pipeline** (`.github/workflows/backend-ci.yml`) targeting Python 3.12 + PostgreSQL 16.
2. **WP05-02: Frontend CI Pipeline** (`.github/workflows/frontend-ci.yml`) targeting Node.js 22, TanStack Start/Nitro SSR, Vite LIVE build, Vitest, and Playwright with removal of hardcoded workstation paths.
3. **WP05-03: Container Build Portability** Multi-stage OCI Dockerfiles, non-root users, deterministic dependency locking, and sanitization of build contexts via `.dockerignore`.
4. **WP05-04: Automated Security Gates** Secret scanning, `pip-audit`, `npm audit`, Ruff linting/static analysis, container image vulnerability scanning, fail-closed policy.
5. **WP05-05: Build & Scan OCI Artifacts** Portable scripts under `scripts/ci/`, commit-based immutable digests, strictly disabled-by-default publication.
6. **WP05-06: SBOM & Release Manifest Generator/Validator** CycloneDX/SPDX SBOM generation, machine-readable release candidate manifest, checksum verification, fail-closed validation rules.
7. **WP05-07: Cross-Repository Integration Gate** Local and CI dual-repository validation linking exact commit SHAs, detecting mismatched pairings, fail-closed access controls.
8. **WP05-08: Disposable Docker Compose Integration** Automated local loopback runner with trap/finally cleanup, disposable PostgreSQL 16, WP-03 bootstrap, same-origin reverse proxy verification.
9. **WP05-09: CI Workflow Security** Least-privilege permissions (`contents: read`), pinned GitHub actions, concurrency cancellation, sanitized outputs.
10. **WP05-10: Quality Gates & Verification** Explicit status reporting (`PASS`, `PASS WITH LIMITATIONS`, `NEEDS ATTENTION`, `BLOCKED`).

---

## 2. Regression Baseline & Invariants

### Accepted Regression Counts
- **Backend (`collectflow`):** `240 passed, 71 skipped, 0 failed` across 311 tests.
  - *Quarantined legacy tests:* 71 tests in `conftest.py:LEGACY_SQLITE_TEST_MODULES` (15 SQLite modules from Phases 1-3 pending migration to PostgreSQL test suite). These must remain quarantined without modification.
- **Frontend (`cash-flow-buddy-101`):** `620 passed across 44 test files, 0 failed`.

### Invariants & Non-Negotiables
- Zero cloud/AWS infrastructure provisioning.
- Zero merges to `main` branch.
- No automatic image publication or remote registry pushes.
- Preserve WP-03 database initialization protections and WP-04 session/CSRF authentication controls.
- Maintain localhost/loopback-only HTTP proxy bindings (`127.0.0.1:${APP_PORT:-8080}`).

---

## 3. Work Package Breakdown & Implementation Tasks

### 3.1. WP05-01: Backend CI Pipeline (`collectflow/.github/workflows/backend-ci.yml`)
- Steps:
  1. Source checkout (`actions/checkout@v4`).
  2. Setup Python 3.12 (`actions/setup-python@v5`) with pip cache.
  3. Install dependencies from `requirements.txt`.
  4. Run syntax/import validation and `ruff check .`.
  5. Run Python dependency vulnerability scanning (`pip-audit`).
  6. Run secret scanning (`detect-secrets` / custom regex scanner).
  7. Start disposable PostgreSQL 16 service container in CI runner.
  8. Run Alembic migrations to `head` (`alembic upgrade head`).
  9. Run complete pytest regression suite (`pytest tests/`).
  10. Run focused security regression tests (`tests/test_wp04_auth_sessions_csrf.py`, etc.).
  11. Build backend OCI container image.
  12. Scan container image vulnerabilities using Trivy.
  13. Produce SBOM and release candidate manifest.

### 3.2. WP05-02: Frontend CI Pipeline (`cash-flow-buddy-101/.github/workflows/frontend-ci.yml`)
- **Playwright Portability Correction (`playwright.config.ts`):**
  - Remove hardcoded `/home/anishb/.local/lib`.
  - Provide portable environment-variable based library discovery (`PLAYWRIGHT_LD_LIBRARY_PATH` or fallback).
  - Separate MOCK (`playwright.mock.config.ts`) and LIVE (`playwright.live.config.ts`) browser test configurations.
  - Fail closed if LIVE mode backend is unreachable (no silent fallback to MOCK).
- **TypeScript & ESLint Corrections:**
  - Fix `exactOptionalPropertyTypes` in `src/lib/api-client.ts` (`csrf_token?: string | undefined`).
  - Fix `CustomerCreateDTO` usages in `src/test/wp04-auth-sessions-csrf.test.tsx`.
  - Fix index signature access in `vite.config.ts` (`process.env['NITRO_PRESET']`).
  - Adjust `eslint.config.js` to avoid prettier conflicts and format discrepancies, ensure `npm run lint` passes with 0 errors.
- **Frontend CI Steps:**
  1. Checkout code.
  2. Setup Node.js 22 with npm cache.
  3. Strict deterministic install via `npm ci`.
  4. TypeScript typecheck (`npx tsc --noEmit`).
  5. ESLint check (`npm run lint`).
  6. `npm audit --audit-level=high`.
  7. Secret scanning.
  8. Vitest full suite (`npm test -- --run`).
  9. Nitro/Vite production SSR build (`npm run build`).
  10. Playwright browser mock smoke tests.
  11. Frontend OCI image build.
  12. Trivy container scan and SBOM generation.

### 3.3. WP05-03: Container Build Portability
- Review and harden `collectflow/Dockerfile` and `cash-flow-buddy-101/Dockerfile`:
  - Multi-stage builds.
  - Base images pinned by tag.
  - Non-root runtime users (`collectflow:collectflow`, `node:node`).
  - Strict lockfile enforcement (`npm ci` fail if missing `package-lock.json`).
  - `.dockerignore` sanitization: exclude `.env*`, `.git`, `secrets/`, `*.pyc`, `venv`, `node_modules`, `.output`, `dist`.
  - Commit SHA label injection (`org.opencontainers.image.revision`).

### 3.4. WP05-04: Automated Security Gates
- Pinned scanner tooling:
  - `pip-audit` for Python dependencies.
  - `npm audit` for Node dependencies.
  - `ruff` for Python SAST.
  - `trivy` for OCI image scanning.
  - Local secret scanner script (`scripts/ci/scan_secrets.py`) checking common API keys, private keys, passwords, and tokens.
- Fail-closed threshold: 0 Critical vulnerabilities, 0 unapproved High vulnerabilities.

### 3.5. WP05-05: Build & Scan OCI Artifacts (`scripts/ci/build_and_scan_image.sh`)
- Portable bash script executable in both CI and local Linux runners.
- Generates commit-tagged images with metadata:
  - Digest (`sha256:...`)
  - Git commit SHA
  - Build timestamp
  - Base image identity
  - Scanner verdict
- External publication remains disabled by default.

### 3.6. WP05-06: CycloneDX/SPDX SBOM & Release Manifest Generator/Validator
- Python scripts:
  - `scripts/ci/generate_sbom.sh` / `generate_sbom.py`
  - `scripts/ci/generate_release_manifest.py`:
    Creates `release-manifest.json` with release candidate ID, Git commit SHAs, image digests, SBOM references with SHA256 checksums, test results, security scan verdicts, approval status (`unapproved`), and deployment authorization state (`false`).
  - `scripts/ci/validate_release_manifest.py`:
    Validates manifest against schema, verifies all referenced files and SHA256 checksums exist, ensures source SHAs match git HEAD, verifies image digests match build outputs, rejects unapproved or failed security gates.

### 3.7. WP05-07: Cross-Repository Integration Gate
- Script `scripts/ci/verify_cross_repo_release.py`:
  - Validates frontend and backend commit pairings.
  - Rejects mismatched, stale, or untracked revisions.
  - Checks if secondary repository is accessible; if in GitHub Actions without cross-repo token, marks integration status as `BLOCKED_PENDING_AUTHORIZATION` with clear instructions, rather than failing insecurely or requesting administrative PATs.

### 3.8. WP05-08: Disposable Docker Compose Integration Runner
- Script `scripts/ci/test_disposable_compose.sh`:
  - Uses a unique project name `cf-ci-${RUN_ID:-$$}`.
  - Generates synthetic test secrets in a temporary directory.
  - Spins up PostgreSQL 16, runs migrations and WP-03 administrator bootstrap.
  - Starts backend, frontend, and localhost Nginx reverse proxy on loopback.
  - Tests health endpoints, same-origin `/api` forwarding, authentication, read endpoint, and CSRF-protected mutation.
  - Uses `trap ... EXIT` to guarantee complete teardown of containers, networks, and disposable volumes.

### 3.9. WP05-09: Secure GitHub Actions Defaults
- `permissions: contents: read` as root default.
- Pinned actions (`actions/checkout@v4`, `actions/setup-python@v5`, `actions/setup-node@v4`, `aquasecurity/trivy-action@0.28.0`).
- Concurrency control with `cancel-in-progress: true`.
- Masking sensitive variables in logs.

### 3.10. WP05-10: Quality Gates, Failure Handling & Focused Regression Tests
- Create `tests/ci/test_wp05_ci_release_gates.py` covering:
  - Release manifest validation success and failure modes.
  - Rejection of wrong source commit SHA.
  - Rejection of digest mismatch.
  - Rejection of missing SBOM or SBOM checksum mismatch.
  - Rejection of failed security gate (Critical/High vulnerabilities).
  - Rejection of incorrect cross-repository pairing.
  - Verification of disabled-by-default publication.
  - Disposable compose trap/teardown validation.
  - Secret-free logging and redaction.

---

## 4. Execution Sequence

1. **Step 1:** Establish plan document (this file).
2. **Step 2:** Frontend corrections:
   - Fix TypeScript errors (`api-client.ts`, `vite.config.ts`, `wp04-auth-sessions-csrf.test.tsx`).
   - Fix ESLint configuration and regex escape errors.
   - Separate Playwright MOCK and LIVE configurations and remove hardcoded paths.
   - Verify `npx tsc --noEmit`, `npm run lint`, and `npm test`.
3. **Step 3:** Backend Ruff & import corrections:
   - Fix imports in `app/main.py`.
   - Ensure `ruff check .` passes cleanly.
4. **Step 4:** Container & Dockerfile hardening:
   - Verify and harden backend & frontend Dockerfiles and `.dockerignore`.
5. **Step 5:** CI/CD scripts & Release tooling:
   - Implement `scripts/ci/scan_secrets.py`.
   - Implement `scripts/ci/generate_sbom.sh` / Python SBOM generator.
   - Implement `scripts/ci/generate_release_manifest.py` and `validate_release_manifest.py`.
   - Implement `scripts/ci/verify_cross_repo_release.py`.
   - Implement `scripts/ci/test_disposable_compose.sh`.
6. **Step 6:** GitHub Actions workflows:
   - Create `collectflow/.github/workflows/backend-ci.yml`.
   - Create `cash-flow-buddy-101/.github/workflows/frontend-ci.yml`.
7. **Step 7:** Focused CI & Release gate tests:
   - Create `tests/ci/test_wp05_ci_release_gates.py` in `collectflow`.
   - Run complete backend test suite and confirm regression baseline (`240 passed, 71 skipped, 0 failed` + new tests passing).
8. **Step 8:** Full local integration verification & disposable compose run:
   - Execute disposable Compose integration.
   - Run manifest generator & validator.
9. **Step 9:** Documentation & Final Report:
   - Produce `collectflow/docs/reviews/COLLECTFLOW_RELEASE_WP05_IMPLEMENTATION_REPORT.md`.
   - Commit and push both branches with exact SHAs.
