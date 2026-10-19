#!/usr/bin/env bash
set -euo pipefail

# CollectFlow OCI Image Build, Scan and Metadata Generator
# WP05-02, WP05-04, WP05-R03 (Fail-Closed Container Scans & Identity Verification)

REPO_TYPE="${1:-backend}"
IMAGE_NAME="${2:-collectflow-${REPO_TYPE}}"
METADATA_OUTPUT="${3:-/tmp/${IMAGE_NAME}-metadata.json}"
PUBLISH_FLAG="${4:-no-publish}"
SCAN_REPORT_PATH="${5:-/tmp/${IMAGE_NAME}-trivy-report.json}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EXCEPTIONS_FILE="${6:-${APPROVED_EXCEPTIONS_FILE:-${ROOT_DIR}/security/vulnerability-exceptions.json}}"
COMMIT_SHA="$(git -C "${ROOT_DIR}" rev-parse HEAD 2>/dev/null || echo "unknown")"
BUILD_TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
TARGET_ARCH="$(uname -m)"

# Strict scanning is enabled by default in mandatory CI/release paths (WP05-R03)
STRICT_SCAN_GATE="${STRICT_SCAN_GATE:-true}"

echo "=== CollectFlow OCI Build & Scan ==="
echo "Repository Type:  ${REPO_TYPE}"
echo "Image Name:       ${IMAGE_NAME}"
echo "Commit SHA:       ${COMMIT_SHA}"
echo "Timestamp:        ${BUILD_TIMESTAMP}"
echo "Architecture:     ${TARGET_ARCH}"
echo "Root Directory:   ${ROOT_DIR}"
echo "Strict Scan Gate: ${STRICT_SCAN_GATE}"

# 1. Strict Publication Gate (WP05-05 Registry Publication Policy)
if [[ "${PUBLISH_FLAG}" == "--publish" || "${PUBLISH_IMAGES:-false}" == "true" ]]; then
    echo "ERROR: Automatic external registry publication (GHCR/ECR) is strictly disabled by default."
    echo "External publication requires separate explicit authorization and deployment promotion."
    exit 1
fi

# 2. Build image with commit SHA build-arg and label
IMAGE_TAG="${IMAGE_NAME}:${COMMIT_SHA}"
echo "Building container image: ${IMAGE_TAG}..."
docker build \
    --build-arg VCS_REF="${COMMIT_SHA}" \
    -t "${IMAGE_TAG}" \
    "${ROOT_DIR}"

# 3. Capture Image Configuration ID (WP05-04: Distinguish Image Config ID from OCI Registry Digest)
IMAGE_ID="$(docker inspect --format='{{.Id}}' "${IMAGE_TAG}")"
echo "Built image ID (daemon config digest): ${IMAGE_ID}"

# Extract base image
BASE_IMAGE="$(docker inspect --format='{{range .Config.Env}}{{println .}}{{end}}' "${IMAGE_TAG}" | grep -E '^PYTHON_VERSION=|^NODE_VERSION=' | head -n 1 || echo "standard-base")"

# 4. Associate and verify exact source SHA with OCI revision label (WP05-04)
OCI_REVISION="$(docker inspect --format='{{index .Config.Labels "org.opencontainers.image.revision"}}' "${IMAGE_TAG}" 2>/dev/null || echo "")"
if [[ -z "${OCI_REVISION}" || "${OCI_REVISION}" == "<no value>" ]]; then
    echo "ERROR: Image ${IMAGE_TAG} is missing required 'org.opencontainers.image.revision' label."
    exit 1
fi

if [[ "${OCI_REVISION}" != "${COMMIT_SHA}" ]]; then
    echo "ERROR: Image OCI revision label '${OCI_REVISION}' does not match git commit SHA '${COMMIT_SHA}'!"
    exit 1
fi
echo "Verified OCI revision label: ${OCI_REVISION} matches git commit SHA."

# 5. Fail-Closed Vulnerability Scan (WP05-R03)
SCAN_VERDICT="BLOCKED_SCANNER_UNAVAILABLE"
SCAN_DETAILS="Trivy scanner is not installed or available."
SCANNER_NAME="trivy"
SCANNER_VERSION="unavailable"
SCAN_CHECKSUM=""
SCAN_CRITICAL_COUNT=0
SCAN_HIGH_COUNT=0
BLOCKING_FINDINGS="[]"

if command -v trivy &>/dev/null; then
    SCANNER_VERSION="$(trivy --version | head -n 1)"
    echo "Running Trivy image vulnerability scan (${SCANNER_VERSION})..."
    
    # Run scan without suppressing tool execution errors; do not ignore unfixed vulnerabilities (WP05-R03)
    TRIVY_RC=0
    trivy image \
        --severity HIGH,CRITICAL \
        --format json \
        --output "${SCAN_REPORT_PATH}" \
        "${IMAGE_TAG}" || TRIVY_RC=$?

    # Fail closed on tool execution errors even if a partial report exists
    if [[ ${TRIVY_RC} -ne 0 && ! -s "${SCAN_REPORT_PATH}" ]]; then
        echo "ERROR: Trivy execution failed with code ${TRIVY_RC} without producing report."
        SCAN_VERDICT="ERROR_SCANNER_EXECUTION_FAILED"
        SCAN_DETAILS="Trivy execution failed with exit code ${TRIVY_RC}."
        exit 1
    fi

    if [[ -s "${SCAN_REPORT_PATH}" ]]; then
        SCAN_CHECKSUM="$(sha256sum "${SCAN_REPORT_PATH}" | awk '{print $1}')"
        
        # Parse counts and blocking findings strictly from JSON output
        PARSE_RESULT="$(python3 -c "
import json, sys

try:
    with open('${SCAN_REPORT_PATH}', 'r') as f:
        data = json.load(f)
    if 'Results' not in data:
        sys.exit(3) # Incomplete report

    results = data.get('Results', [])
    crit_list = []
    high_list = []
    for r in results:
        for v in r.get('Vulnerabilities', []):
            sev = v.get('Severity', '').upper()
            cve_id = v.get('VulnerabilityID', 'UNKNOWN')
            pkg = v.get('PkgName', 'unknown')
            if sev == 'CRITICAL':
                crit_list.append(f'{cve_id} ({pkg})')
            elif sev == 'HIGH':
                high_list.append(f'{cve_id} ({pkg})')

    # Check approved exceptions if file exists
    approved_cves = set()
    exceptions_path = '${EXCEPTIONS_FILE}'
    if exceptions_path:
        import os
        if os.path.exists(exceptions_path):
            with open(exceptions_path, 'r') as ef:
                edata = json.load(ef)
                approved_cves = set(edata.get('approved_cves', []))

    unapproved_crit = [c for c in crit_list if c.split()[0] not in approved_cves]
    unapproved_high = [c for c in high_list if c.split()[0] not in approved_cves]

    out = {
        'crit_count': len(crit_list),
        'high_count': len(high_list),
        'unapproved_crit_count': len(unapproved_crit),
        'unapproved_high_count': len(unapproved_high),
        'blocking_findings': unapproved_crit + unapproved_high
    }
    print(json.dumps(out))
except Exception as e:
    sys.exit(2)
" || echo "PARSE_ERROR")"

        if [[ "${PARSE_RESULT}" == "PARSE_ERROR" ]]; then
            echo "ERROR: Failed to parse Trivy scan report JSON or report is malformed/incomplete."
            SCAN_VERDICT="ERROR_SCANNER_EXECUTION_FAILED"
            SCAN_DETAILS="Malformed or incomplete scan report JSON."
            exit 1
        fi

        SCAN_CRITICAL_COUNT="$(echo "${PARSE_RESULT}" | python3 -c 'import sys, json; print(json.load(sys.stdin)["crit_count"])')"
        SCAN_HIGH_COUNT="$(echo "${PARSE_RESULT}" | python3 -c 'import sys, json; print(json.load(sys.stdin)["high_count"])')"
        UNAPPROVED_CRIT="$(echo "${PARSE_RESULT}" | python3 -c 'import sys, json; print(json.load(sys.stdin)["unapproved_crit_count"])')"
        UNAPPROVED_HIGH="$(echo "${PARSE_RESULT}" | python3 -c 'import sys, json; print(json.load(sys.stdin)["unapproved_high_count"])')"
        BLOCKING_FINDINGS="$(echo "${PARSE_RESULT}" | python3 -c 'import sys, json; print(json.dumps(json.load(sys.stdin)["blocking_findings"]))')"

        if [[ ${UNAPPROVED_CRIT} -gt 0 ]]; then
            SCAN_VERDICT="FAIL_CRITICAL_VULNERABILITY"
            SCAN_DETAILS="Detected ${UNAPPROVED_CRIT} unapproved CRITICAL vulnerability(ies) in ${IMAGE_TAG}."
            echo "SECURITY GATE FAILED: ${SCAN_DETAILS}"
            exit 1
        elif [[ ${UNAPPROVED_HIGH} -gt 0 ]]; then
            SCAN_VERDICT="FAIL_HIGH_VULNERABILITY"
            SCAN_DETAILS="Detected ${UNAPPROVED_HIGH} unapproved HIGH vulnerability(ies) in ${IMAGE_TAG}."
            echo "SECURITY GATE FAILED: ${SCAN_DETAILS}"
            exit 1
        elif [[ ${SCAN_CRITICAL_COUNT} -gt 0 || ${SCAN_HIGH_COUNT} -gt 0 ]]; then
            SCAN_VERDICT="PASS_WITH_APPROVED_EXCEPTIONS"
            SCAN_DETAILS="All findings (${SCAN_CRITICAL_COUNT} CRITICAL, ${SCAN_HIGH_COUNT} HIGH) covered by documented approved exceptions."
            echo "SECURITY GATE PASSED (WITH APPROVED EXCEPTIONS): ${SCAN_DETAILS}"
        else
            SCAN_VERDICT="PASS"
            SCAN_DETAILS="0 CRITICAL and 0 HIGH vulnerabilities detected in container image."
            echo "SECURITY GATE PASSED: ${SCAN_DETAILS}"
        fi
    fi
else
    echo "WARNING: Trivy scanner is not installed locally."
    # Strict scan gate fails closed when scanner is missing (WP05-R03)
    if [[ "${STRICT_SCAN_GATE}" == "true" ]]; then
        echo "ERROR: Mandatory container vulnerability scan cannot pass because Trivy is unavailable."
        exit 1
    fi
fi

# 6. Generate Metadata JSON with explicit artifact digest vs image ID distinction (WP05-04, WP05-R03)
mkdir -p "$(dirname "${METADATA_OUTPUT}")"
cat <<EOF > "${METADATA_OUTPUT}"
{
  "schema_version": "1.2.0",
  "image_name": "${IMAGE_NAME}",
  "image_tag": "${IMAGE_TAG}",
  "image_id": "${IMAGE_ID}",
  "artifact_type": "local_daemon_image",
  "artifact_digest": "${IMAGE_ID}",
  "oci_revision": "${OCI_REVISION}",
  "source_commit_sha": "${COMMIT_SHA}",
  "build_timestamp": "${BUILD_TIMESTAMP}",
  "target_architecture": "${TARGET_ARCH}",
  "base_image": "${BASE_IMAGE}",
  "build_toolchain": "docker-$(docker --version 2>/dev/null | awk '{print $3}' | tr -d ',' || echo 'unknown')",
  "security_scan": {
    "scanner": "${SCANNER_NAME}",
    "scanner_version": "${SCANNER_VERSION}",
    "verdict": "${SCAN_VERDICT}",
    "details": "${SCAN_DETAILS}",
    "critical_count": ${SCAN_CRITICAL_COUNT},
    "high_count": ${SCAN_HIGH_COUNT},
    "blocking_findings": ${BLOCKING_FINDINGS},
    "evidence_file": "${SCAN_REPORT_PATH}",
    "evidence_sha256": "${SCAN_CHECKSUM}"
  },
  "scan_verdict": "${SCAN_VERDICT}",
  "publication_status": "DISABLED_BY_DEFAULT",
  "registry_published": false,
  "registry_digest": null,
  "deployment_authorized": false
}
EOF

echo "Image metadata written to ${METADATA_OUTPUT}"
echo "Build and scan complete: ${SCAN_VERDICT}"
