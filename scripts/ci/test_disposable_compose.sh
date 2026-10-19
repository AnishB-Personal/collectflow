#!/usr/bin/env bash
set -euo pipefail

# CollectFlow Disposable Docker Compose Integration Runner (WP05-08)
# Spins up an isolated, uniquely named Compose stack on loopback interface,
# applies migrations, bootstraps administrator, verifies same-origin proxy,
# verifies authenticated API & CSRF mutation behavior, and guarantees clean teardown via trap.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
COMPOSE_DIR="${ROOT_DIR}/deploy/compose"

RUN_ID="ci-$$-${RANDOM}"
PROJECT_NAME="cf-test-${RUN_ID}"
TMP_DIR="$(mktemp -d "${ROOT_DIR}/.tmp-disposable-XXXXXX")"

# Find an ephemeral free loopback port
FREE_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
export APP_PORT="${FREE_PORT}"
export APP_BIND_IP="127.0.0.1"
export POSTGRES_DB="collectflow_integration"
export POSTGRES_USER="collectflow_user"
export COLLECTFLOW_ENV="integration"

echo "================================================================="
echo "CollectFlow Disposable Compose Integration Test"
echo "Project Name: ${PROJECT_NAME}"
echo "Loopback Port: ${APP_PORT}"
echo "Temporary Dir: ${TMP_DIR}"
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

# 1. Prepare temporary synthetic secrets
mkdir -p "${TMP_DIR}/secrets"
echo -n "disposable_test_secret_32_bytes_ok!" > "${TMP_DIR}/secrets/db_password.txt"
echo -n "AdminBootstrap2026!#" > "${TMP_DIR}/secrets/admin_password.txt"
echo -n "disposable_csrf_secret_32_bytes_ok!" > "${TMP_DIR}/secrets/csrf_secret.txt"
echo -n "disposable_session_secret_32_bytes_ok!" > "${TMP_DIR}/secrets/session_secret.txt"
chmod 644 "${TMP_DIR}"/secrets/*.txt

# 2. Write an override file removing fixed container names to avoid any host-level name collisions
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

# 7. Verification checks
echo "--> Running integration verifications against ${BASE_URL}..."

# A. Same-origin routing checks
echo "1. Testing Nginx proxy liveness probe..."
PROXY_HEALTH="$(curl -s "${BASE_URL}/nginx-health")"
if [[ "${PROXY_HEALTH}" != *"healthy"* ]]; then
    echo "FAILED: Proxy health check returned: ${PROXY_HEALTH}"
    exit 1
fi
echo "   [PASS] /nginx-health -> healthy"

echo "2. Testing backend health check via proxy..."
API_HEALTH="$(curl -s "${BASE_URL}/api/health")"
if [[ "${API_HEALTH}" != *"healthy"* ]]; then
    echo "FAILED: Backend health check via proxy returned: ${API_HEALTH}"
    exit 1
fi
echo "   [PASS] /api/health -> healthy"

echo "3. Testing frontend SSR route..."
FRONTEND_HTML="$(curl -s "${BASE_URL}/")"
if [[ -z "${FRONTEND_HTML}" ]]; then
    echo "FAILED: Frontend root route returned empty response."
    exit 1
fi
echo "   [PASS] / -> SSR response received"

# B. Authentication check: login with bootstrapped credentials
echo "4. Testing authentication via same-origin /api/auth/login..."
COOKIE_JAR="${TMP_DIR}/cookies.txt"
LOGIN_PAYLOAD='{"username":"admin","password":"AdminBootstrap2026!#"}'

LOGIN_RESP="$(curl -s -c "${COOKIE_JAR}" -X POST "${BASE_URL}/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "${LOGIN_PAYLOAD}")"

if [[ "${LOGIN_RESP}" != *"admin"* ]]; then
    echo "FAILED: Login request failed. Response: ${LOGIN_RESP}"
    exit 1
fi

CSRF_TOKEN="$(echo "${LOGIN_RESP}" | python3 -c 'import sys, json; print(json.load(sys.stdin).get("csrf_token", ""))')"
echo "   [PASS] Successfully authenticated admin session (CSRF token acquired)"

# C. Representative read-only call
echo "5. Testing authenticated read-only endpoint (/api/auth/me)..."
USER_ME="$(curl -s -b "${COOKIE_JAR}" "${BASE_URL}/api/auth/me")"
if [[ "${USER_ME}" != *"admin"* ]]; then
    echo "FAILED: Authenticated /api/auth/me failed. Response: ${USER_ME}"
    exit 1
fi
echo "   [PASS] /api/auth/me -> role: admin"

# D. Verify mandatory password rotation blocks state-modifying requests before password change
echo "6. Testing mandatory password rotation enforcement on state-modifying requests..."
ROTATION_BLOCKED_RESP="$(curl -s -w "\nHTTP_STATUS:%{http_code}" -b "${COOKIE_JAR}" -X POST "${BASE_URL}/api/customers" \
    -H "Content-Type: application/json" \
    -H "X-CSRF-Token: ${CSRF_TOKEN}" \
    -d '{"billing_name":"Blocked Customer","state":"Maharashtra"}')"

if [[ "${ROTATION_BLOCKED_RESP}" != *"PASSWORD_CHANGE_REQUIRED"* ]]; then
    echo "FAILED: Expected PASSWORD_CHANGE_REQUIRED error before password rotation. Response: ${ROTATION_BLOCKED_RESP}"
    exit 1
fi
echo "   [PASS] Mutation correctly blocked pending mandatory password rotation"

# E. Execute password rotation via /api/auth/change-password
echo "7. Executing mandatory password rotation (/api/auth/change-password)..."
CHANGE_PW_RESP="$(curl -s -b "${COOKIE_JAR}" -X POST "${BASE_URL}/api/auth/change-password" \
    -H "Content-Type: application/json" \
    -H "X-CSRF-Token: ${CSRF_TOKEN}" \
    -d '{"current_password":"AdminBootstrap2026!#","new_password":"RotatedAdminPass2026!#"}')"

if [[ "${CHANGE_PW_RESP}" != *"Password changed successfully"* ]]; then
    echo "FAILED: Password rotation failed. Response: ${CHANGE_PW_RESP}"
    exit 1
fi
echo "   [PASS] Password rotated successfully and previous sessions revoked"

# F. Log in with new rotated password
echo "8. Logging in with new rotated credentials..."
rm -f "${COOKIE_JAR}"
NEW_LOGIN_PAYLOAD='{"username":"admin","password":"RotatedAdminPass2026!#"}'

NEW_LOGIN_RESP="$(curl -s -c "${COOKIE_JAR}" -X POST "${BASE_URL}/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "${NEW_LOGIN_PAYLOAD}")"

if [[ "${NEW_LOGIN_RESP}" != *"admin"* ]]; then
    echo "FAILED: Re-login with new password failed. Response: ${NEW_LOGIN_RESP}"
    exit 1
fi

NEW_CSRF_TOKEN="$(echo "${NEW_LOGIN_RESP}" | python3 -c 'import sys, json; print(json.load(sys.stdin).get("csrf_token", ""))')"
echo "   [PASS] Successfully re-authenticated with rotated credentials"

# G. CSRF enforcement test: reject mutation without CSRF token
echo "9. Testing CSRF rejection on state-modifying mutation without token..."
NO_CSRF_RESP="$(curl -s -o /dev/null -w "%{http_code}" -b "${COOKIE_JAR}" -X POST "${BASE_URL}/api/customers" \
    -H "Content-Type: application/json" \
    -d '{"billing_name":"Integration Test Customer","state":"Maharashtra"}')"

if [[ "${NO_CSRF_RESP}" != "403" ]]; then
    echo "FAILED: Expected 403 Forbidden without CSRF token, got: ${NO_CSRF_RESP}"
    exit 1
fi
echo "   [PASS] Mutation without CSRF token strictly rejected with 403"

# H. CSRF-protected mutation check: accept with valid CSRF token
echo "10. Testing valid CSRF token acceptance on unblocked account..."
VALID_CSRF_RAW="$(curl -s -w "\nHTTP_STATUS:%{http_code}" -b "${COOKIE_JAR}" -X POST "${BASE_URL}/api/customers" \
    -H "Content-Type: application/json" \
    -H "X-CSRF-Token: ${NEW_CSRF_TOKEN}" \
    -d '{"billing_name":"Integration Test Customer","state":"Maharashtra"}')"

VALID_CSRF_STATUS="$(echo "${VALID_CSRF_RAW}" | grep "HTTP_STATUS:" | cut -d':' -f2)"

if [[ "${VALID_CSRF_STATUS}" == "403" || "${VALID_CSRF_STATUS}" == "500" ]]; then
    echo "FAILED: Mutation with valid CSRF token failed. Response: ${VALID_CSRF_RAW}"
    exit 1
fi
echo "   [PASS] Mutation with valid CSRF token accepted (HTTP status: ${VALID_CSRF_STATUS})"

echo "================================================================="
echo "ALL DISPOSABLE COMPOSE INTEGRATION CHECKS PASSED!"
echo "================================================================="
