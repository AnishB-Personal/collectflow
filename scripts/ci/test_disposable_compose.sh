#!/usr/bin/env bash
set -euo pipefail

# CollectFlow Disposable Docker Compose Integration Runner
# WP05-05: Strengthened assertions, failure-injection trap verification,
# strict HTTP status checks, DB mutation confirmations, stale session validation,
# restricted secret permissions, and sanitized credential logging.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
COMPOSE_DIR="${ROOT_DIR}/deploy/compose"

RUN_ID="ci-$$-${RANDOM}"
PROJECT_NAME="cf-test-${RUN_ID}"
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

# Handle failure-injection trap test mode (WP05-05)
if [[ "${1:-}" == "--test-failure-trap" ]]; then
    echo "=== Executing Failure-Injection Trap Verification Test ==="
    TEST_TMP_DIR="$(mktemp -d "${ROOT_DIR}/.tmp-disposable-XXXXXX")"
    chmod 700 "${TEST_TMP_DIR}"
    TEST_PROJECT="cf-trap-test-${RUN_ID}"
    TEST_COMPOSE="${TEST_TMP_DIR}/docker-compose.yml"
    
    cat <<EOF > "${TEST_COMPOSE}"
services:
  trap_worker:
    image: alpine:latest
    command: ["sleep", "120"]
EOF

    # Run subshell that spins up container, simulates failure, and executes trap cleanup
    (
        test_cleanup() {
            local code=$?
            echo "Subshell trap handler caught exit code: ${code}"
            docker compose -p "${TEST_PROJECT}" -f "${TEST_COMPOSE}" down -v --remove-orphans 2>/dev/null || true
            rm -rf "${TEST_TMP_DIR}"
            exit ${code}
        }
        trap test_cleanup EXIT INT TERM ERR
        
        docker compose -p "${TEST_PROJECT}" -f "${TEST_COMPOSE}" up -d trap_worker
        if [[ -z "$(docker compose -p "${TEST_PROJECT}" -f "${TEST_COMPOSE}" ps -q trap_worker 2>/dev/null)" ]]; then
            echo "ERROR: Failed to start test container for trap verification."
            exit 1
        fi
        echo "Container trap_worker started. Simulating failure injection..."
        # Simulate fatal failure
        exit 42
    ) || local_subshell_exit=$?

    # Verify that the trap cleaned up all containers and the temp directory
    REMAINING_CONTAINERS="$(docker compose -p "${TEST_PROJECT}" -f "${TEST_COMPOSE}" ps -q 2>/dev/null || true)"
    if [[ -n "${REMAINING_CONTAINERS}" ]]; then
        echo "FAILED: Cleanup trap left running containers: ${REMAINING_CONTAINERS}"
        docker compose -p "${TEST_PROJECT}" -f "${TEST_COMPOSE}" down -v --remove-orphans 2>/dev/null || true
        exit 1
    fi

    if [[ -d "${TEST_TMP_DIR}" ]]; then
        echo "FAILED: Cleanup trap left temporary directory on disk: ${TEST_TMP_DIR}"
        rm -rf "${TEST_TMP_DIR}"
        exit 1
    fi

    echo "=== [TRAP-VERIFICATION] PASSED: Cleanup trap successfully tore down containers and removed temporary directory upon injected failure ==="
    exit 0
fi

TMP_DIR="$(mktemp -d "${ROOT_DIR}/.tmp-disposable-XXXXXX")"
chmod 700 "${TMP_DIR}"

# Find an ephemeral free loopback port
FREE_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
export APP_PORT="${FREE_PORT}"
export APP_BIND_IP="127.0.0.1"
export POSTGRES_DB="collectflow_integration"
export POSTGRES_USER="collectflow_user"
export COLLECTFLOW_ENV="integration"

echo "================================================================="
echo "CollectFlow Disposable Compose Integration Test"
echo "Project Name:   ${PROJECT_NAME}"
echo "Loopback Port:  ${APP_PORT}"
echo "Temporary Dir:  ${TMP_DIR}"
echo "================================================================="

# Trap cleanup for guaranteed teardown
cleanup() {
    local exit_code=$?
    echo "=== Initiating Disposable Stack Teardown (${PROJECT_NAME}) ==="
    if [[ -f "${TMP_DIR}/docker-compose.override.yml" ]]; then
        docker compose \
            -p "${PROJECT_NAME}" \
            -f "${COMPOSE_DIR}/docker-compose.yml" \
            -f "${TMP_DIR}/docker-compose.override.yml" \
            down -v --remove-orphans 2>/dev/null || true
    fi
    rm -rf "${TMP_DIR}"
    echo "=== Teardown Complete (Exit code: ${exit_code}) ==="
    exit ${exit_code}
}
trap cleanup EXIT INT TERM ERR

# 1. Prepare temporary secrets with restricted permissions (WP05-05)
mkdir -p "${TMP_DIR}/secrets"
chmod 700 "${TMP_DIR}/secrets"

# Write secrets without printing values to logs
echo -n "disposable_test_secret_32_bytes_ok!" > "${TMP_DIR}/secrets/db_password.txt"
echo -n "AdminBootstrap2026!#" > "${TMP_DIR}/secrets/admin_password.txt"
echo -n "disposable_csrf_secret_32_bytes_ok!" > "${TMP_DIR}/secrets/csrf_secret.txt"
echo -n "disposable_session_secret_32_bytes_ok!" > "${TMP_DIR}/secrets/session_secret.txt"
chmod 600 "${TMP_DIR}"/secrets/*.txt
echo "Secret files generated with restricted 0600 permissions."

# 2. Write an override file removing fixed container names and running backend as host UID for permission isolation
cat <<EOF > "${TMP_DIR}/docker-compose.override.yml"
services:
  postgres:
    container_name: null
    environment:
      POSTGRES_DB: "${POSTGRES_DB}"
      POSTGRES_USER: "${POSTGRES_USER}"
      POSTGRES_PASSWORD_FILE: "/run/secrets/db_password"
    secrets:
      - db_password
  backend:
    container_name: null
    user: "${HOST_UID}:${HOST_GID}"
    environment:
      COLLECTFLOW_ENV: "integration"
      COLLECTFLOW_DB_HOST: "postgres"
      COLLECTFLOW_DB_PORT: "5432"
      COLLECTFLOW_DB_NAME: "${POSTGRES_DB}"
      COLLECTFLOW_DB_USER: "${POSTGRES_USER}"
      COLLECTFLOW_DB_PASSWORD_FILE: "/run/secrets/db_password"
      COLLECTFLOW_ADMIN_PASSWORD_FILE: "/run/secrets/admin_password"
      COLLECTFLOW_CSRF_SECRET_FILE: "/run/secrets/csrf_secret"
      COLLECTFLOW_SESSION_SECRET_FILE: "/run/secrets/session_secret"
    secrets:
      - db_password
      - admin_password
      - csrf_secret
      - session_secret
  frontend:
    container_name: null
  proxy:
    container_name: null
    ports:
      - "127.0.0.1:${APP_PORT}:80"

secrets:
  db_password:
    file: ${TMP_DIR}/secrets/db_password.txt
  admin_password:
    file: ${TMP_DIR}/secrets/admin_password.txt
  csrf_secret:
    file: ${TMP_DIR}/secrets/csrf_secret.txt
  session_secret:
    file: ${TMP_DIR}/secrets/session_secret.txt
EOF

COMPOSE_CMD="docker compose -p ${PROJECT_NAME} -f ${COMPOSE_DIR}/docker-compose.yml -f ${TMP_DIR}/docker-compose.override.yml"

# 3. Start PostgreSQL service
echo "--> Starting disposable PostgreSQL 16..."
${COMPOSE_CMD} up -d postgres

echo "--> Waiting for PostgreSQL readiness..."
for i in {1..30}; do
    if ${COMPOSE_CMD} exec -T postgres pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" &>/dev/null; then
        echo "PostgreSQL is ready."
        break
    fi
    sleep 1
    if [[ $i -eq 30 ]]; then
        echo "ERROR: Timed out waiting for PostgreSQL container."
        exit 1
    fi
done

# 4. Run database migrations
echo "--> Applying Alembic migrations inside disposable backend container..."
${COMPOSE_CMD} run --rm backend alembic upgrade head

# 5. Execute controlled WP-03 administrator bootstrap
echo "--> Bootstrapping initial administrator (WP-03)..."
${COMPOSE_CMD} run --rm \
    -e COLLECTFLOW_BOOTSTRAP_ADMIN_PASSWORD_FILE=/run/secrets/admin_password \
    backend python3 scripts/init_production_db.py

# 6. Start full application stack (backend, frontend, proxy)
echo "--> Starting backend, frontend and local proxy..."
${COMPOSE_CMD} up -d backend frontend proxy

echo "--> Waiting for HTTP services on 127.0.0.1:${APP_PORT}..."
BASE_URL="http://127.0.0.1:${APP_PORT}"

for i in {1..40}; do
    if curl -s -f "${BASE_URL}/nginx-health" &>/dev/null && curl -s -f "${BASE_URL}/api/health" &>/dev/null; then
        echo "Services are responding."
        break
    fi
    sleep 1
    if [[ $i -eq 40 ]]; then
        echo "ERROR: Timed out waiting for application stack."
        ${COMPOSE_CMD} logs
        exit 1
    fi
done

# 7. Verification checks with strict HTTP status code assertions (WP05-05)
echo "--> Running integration verifications against ${BASE_URL}..."

# Helper function to check exact status code
assert_status() {
    local expected="$1"
    local actual="$2"
    local desc="$3"
    if [[ "${actual}" != "${expected}" ]]; then
        echo "FAILED: ${desc} - expected HTTP ${expected}, got HTTP ${actual}"
        exit 1
    fi
    echo "   [PASS] ${desc} (HTTP ${actual})"
}

# A. Same-origin routing checks
PROXY_STATUS="$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/nginx-health")"
assert_status "200" "${PROXY_STATUS}" "Proxy health check (/nginx-health)"

API_STATUS="$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/api/health")"
assert_status "200" "${API_STATUS}" "Backend health check (/api/health)"

FRONTEND_STATUS="$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/")"
assert_status "200" "${FRONTEND_STATUS}" "Frontend SSR root route (/)"

# B. Unauthenticated check (WP05-05: verify failed authorization)
UNAUTH_STATUS="$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/api/auth/me")"
assert_status "401" "${UNAUTH_STATUS}" "Unauthenticated request rejection on /api/auth/me"

# C. Authentication: login with bootstrapped credentials
COOKIE_JAR="${TMP_DIR}/cookies.txt"
OLD_COOKIE_JAR="${TMP_DIR}/old_cookies.txt"
LOGIN_PAYLOAD_FILE="${TMP_DIR}/login_payload.json"
echo '{"username":"admin","password":"AdminBootstrap2026!#"}' > "${LOGIN_PAYLOAD_FILE}"
chmod 600 "${LOGIN_PAYLOAD_FILE}"

LOGIN_RESP="$(curl -s -w "\nHTTP_STATUS:%{http_code}" -c "${COOKIE_JAR}" -X POST "${BASE_URL}/api/auth/login" \
    -H "Content-Type: application/json" \
    -d @"${LOGIN_PAYLOAD_FILE}")"

LOGIN_STATUS="$(echo "${LOGIN_RESP}" | grep "HTTP_STATUS:" | cut -d':' -f2)"
assert_status "200" "${LOGIN_STATUS}" "Admin authentication (/api/auth/login)"

CSRF_TOKEN="$(echo "${LOGIN_RESP}" | grep -v "HTTP_STATUS:" | python3 -c 'import sys, json; print(json.load(sys.stdin).get("csrf_token", ""))')"
if [[ -z "${CSRF_TOKEN}" ]]; then
    echo "FAILED: Did not receive CSRF token from login response."
    exit 1
fi
echo "   [PASS] CSRF token successfully acquired."

# D. Authenticated read-only call
ME_STATUS="$(curl -s -o /dev/null -w "%{http_code}" -b "${COOKIE_JAR}" "${BASE_URL}/api/auth/me")"
assert_status "200" "${ME_STATUS}" "Authenticated /api/auth/me call"

# E. Verify mandatory password rotation blocks state-modifying requests before password change
ROTATION_BLOCKED_STATUS="$(curl -s -o /dev/null -w "%{http_code}" -b "${COOKIE_JAR}" -X POST "${BASE_URL}/api/customers" \
    -H "Content-Type: application/json" \
    -H "X-CSRF-Token: ${CSRF_TOKEN}" \
    -d '{"billing_name":"Blocked Customer","state":"Maharashtra"}')"
assert_status "403" "${ROTATION_BLOCKED_STATUS}" "Mandatory password rotation blocks mutation before change"

# Save old session cookie for stale-session revocation check later
cp "${COOKIE_JAR}" "${OLD_COOKIE_JAR}"

# F. Execute password rotation via /api/auth/change-password
ROTATE_PAYLOAD_FILE="${TMP_DIR}/rotate_payload.json"
echo '{"current_password":"AdminBootstrap2026!#","new_password":"RotatedAdminPass2026!#"}' > "${ROTATE_PAYLOAD_FILE}"
chmod 600 "${ROTATE_PAYLOAD_FILE}"

ROTATE_STATUS="$(curl -s -o /dev/null -w "%{http_code}" -b "${COOKIE_JAR}" -X POST "${BASE_URL}/api/auth/change-password" \
    -H "Content-Type: application/json" \
    -H "X-CSRF-Token: ${CSRF_TOKEN}" \
    -d @"${ROTATE_PAYLOAD_FILE}")"
assert_status "200" "${ROTATE_STATUS}" "Mandatory password rotation (/api/auth/change-password)"

# G. Stale-session check (WP05-05: confirm revoked pre-rotation session cannot be used)
STALE_STATUS="$(curl -s -o /dev/null -w "%{http_code}" -b "${OLD_COOKIE_JAR}" "${BASE_URL}/api/auth/me")"
assert_status "401" "${STALE_STATUS}" "Revocation of pre-rotation session on /api/auth/me (stale session rejected)"

# H. Log in with new rotated password
rm -f "${COOKIE_JAR}"
NEW_LOGIN_PAYLOAD_FILE="${TMP_DIR}/new_login_payload.json"
echo '{"username":"admin","password":"RotatedAdminPass2026!#"}' > "${NEW_LOGIN_PAYLOAD_FILE}"
chmod 600 "${NEW_LOGIN_PAYLOAD_FILE}"

NEW_LOGIN_RESP="$(curl -s -w "\nHTTP_STATUS:%{http_code}" -c "${COOKIE_JAR}" -X POST "${BASE_URL}/api/auth/login" \
    -H "Content-Type: application/json" \
    -d @"${NEW_LOGIN_PAYLOAD_FILE}")"

NEW_LOGIN_STATUS="$(echo "${NEW_LOGIN_RESP}" | grep "HTTP_STATUS:" | cut -d':' -f2)"
assert_status "200" "${NEW_LOGIN_STATUS}" "Re-authentication with rotated password"

NEW_CSRF_TOKEN="$(echo "${NEW_LOGIN_RESP}" | grep -v "HTTP_STATUS:" | python3 -c 'import sys, json; print(json.load(sys.stdin).get("csrf_token", ""))')"
if [[ -z "${NEW_CSRF_TOKEN}" ]]; then
    echo "FAILED: Did not receive new CSRF token after rotated login."
    exit 1
fi

# I. Data verification before no-CSRF mutation (WP05-05)
CUSTOMERS_BEFORE="$(curl -s -b "${COOKIE_JAR}" "${BASE_URL}/api/customers")"
INITIAL_COUNT="$(echo "${CUSTOMERS_BEFORE}" | python3 -c 'import sys, json; print(len(json.load(sys.stdin)))' 2>/dev/null || echo "0")"

# J. CSRF enforcement test: reject mutation without CSRF token
NO_CSRF_STATUS="$(curl -s -o /dev/null -w "%{http_code}" -b "${COOKIE_JAR}" -X POST "${BASE_URL}/api/customers" \
    -H "Content-Type: application/json" \
    -d '{"billing_name":"Unauthorized Customer Without CSRF","state":"Maharashtra"}')"
assert_status "403" "${NO_CSRF_STATUS}" "State-modifying mutation without CSRF token rejected with 403"

# K. Confirm the no-CSRF mutation did NOT modify database (WP05-05)
CUSTOMERS_AFTER_BLOCKED="$(curl -s -b "${COOKIE_JAR}" "${BASE_URL}/api/customers")"
BLOCKED_COUNT="$(echo "${CUSTOMERS_AFTER_BLOCKED}" | python3 -c 'import sys, json; print(len(json.load(sys.stdin)))' 2>/dev/null || echo "0")"
if [[ "${INITIAL_COUNT}" != "${BLOCKED_COUNT}" ]]; then
    echo "FAILED: Customer count changed despite 403 rejection! Initial: ${INITIAL_COUNT}, After: ${BLOCKED_COUNT}"
    exit 1
fi
if echo "${CUSTOMERS_AFTER_BLOCKED}" | grep -q "Unauthorized Customer Without CSRF"; then
    echo "FAILED: Database record was inserted despite missing CSRF token!"
    exit 1
fi
echo "   [PASS] Confirmed no-CSRF mutation did NOT modify database (customer count preserved: ${BLOCKED_COUNT})"

# L. CSRF-protected mutation check: accept with valid CSRF token & verify record creation in DB (WP05-05)
SYNTHETIC_NAME="Verified Integration Customer ${RUN_ID}"
CREATE_RESP="$(curl -s -w "\nHTTP_STATUS:%{http_code}" -b "${COOKIE_JAR}" -X POST "${BASE_URL}/api/customers" \
    -H "Content-Type: application/json" \
    -H "X-CSRF-Token: ${NEW_CSRF_TOKEN}" \
    -d "{\"billing_name\":\"${SYNTHETIC_NAME}\",\"state\":\"Maharashtra\"}")"

CREATE_STATUS="$(echo "${CREATE_RESP}" | grep "HTTP_STATUS:" | cut -d':' -f2)"
assert_status "200" "${CREATE_STATUS}" "State-modifying mutation with valid CSRF token accepted"

NEW_CUST_ID="$(echo "${CREATE_RESP}" | grep -v "HTTP_STATUS:" | python3 -c 'import sys, json; print(json.load(sys.stdin).get("id", ""))')"
if [[ -z "${NEW_CUST_ID}" || "${NEW_CUST_ID}" == "None" ]]; then
    echo "FAILED: Customer creation response did not return new record ID."
    exit 1
fi

# Confirm the record actually exists in the database
VERIFY_CUST_STATUS="$(curl -s -w "\nHTTP_STATUS:%{http_code}" -b "${COOKIE_JAR}" "${BASE_URL}/api/customers/${NEW_CUST_ID}")"
GET_CUST_CODE="$(echo "${VERIFY_CUST_STATUS}" | grep "HTTP_STATUS:" | cut -d':' -f2)"
assert_status "200" "${GET_CUST_CODE}" "Querying created synthetic customer by ID (/api/customers/${NEW_CUST_ID})"

if ! echo "${VERIFY_CUST_STATUS}" | grep -q "${SYNTHETIC_NAME}"; then
    echo "FAILED: Query response did not contain expected synthetic customer billing name!"
    exit 1
fi
echo "   [PASS] Verified synthetic customer record was genuinely persisted in PostgreSQL database (ID: ${NEW_CUST_ID})."

echo "================================================================="
echo "ALL DISPOSABLE COMPOSE INTEGRATION AND DATABASE CHECKS PASSED!"
echo "================================================================="
