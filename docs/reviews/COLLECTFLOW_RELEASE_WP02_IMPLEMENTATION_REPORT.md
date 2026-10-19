# CollectFlow — Release Preparation WP-02 Implementation Report

**Document Status:** Complete (Updated Post-Review)  
**Date:** 2026-10-19  
**Developer Recommendation:** **READY FOR INDEPENDENT RE-REVIEW**  
**Target Package:** Portable OCI Containers & Docker Compose Integration (WP-02)

---

## 1. Executive Summary & Verdict

Release Preparation Work Package 02 (WP-02) has been fully implemented, hardened, and verified with all independent review corrections addressed:

1. **Restricted Local Compose Exposure (WP02-01)**:
   - Updated Docker Compose reverse proxy port mapping to bind strictly to loopback by default: `"${APP_BIND_IP:-127.0.0.1}:${APP_PORT:-8080}:80"`.
   - Verified that the default published port does NOT listen on `0.0.0.0` or `[::]`, strictly listening on `127.0.0.1:8080`.
   - Permitted explicit, controlled bind configuration via `APP_BIND_IP`.
   - Explicitly documented that this Nginx configuration is HTTP-only and intended solely for isolated local integration and testing.
   - Explicitly prohibited deploying this unencrypted HTTP proxy configuration as-is to public internet or AWS environments without external TLS termination (e.g., AWS ALB, Cloudflare, or Ingress Controller).

2. **Frontend OCI Container & Client Alignment (`collectflow-frontend`)**:
   - Built on `node:22-alpine` using a multi-stage Dockerfile.
   - Configured TanStack Start / Nitro with the portable `node-server` preset while preserving Cloudflare as an optional adapter.
   - Clarified API client documentation: confirmed that empty or omitted `VITE_API_BASE_URL` in LIVE mode emits clean relative `/api/...` requests (not a literal `"same-origin"` string), enabling dynamic same-origin resolution against `window.location.origin` without image rebuilds.
   - Configured flexible build context (`${FRONTEND_BUILD_CONTEXT:-../../../cash-flow-buddy-101}`) supporting standard sibling-repository layouts or custom paths.
   - Executes under an unprivileged user (`collectflow:collectflow`, UID 10001).

3. **Hardened Backend OCI Container (`collectflow-backend`)**:
   - Built on `python:3.12-slim` using a multi-stage Dockerfile.
   - Executes under an unprivileged user (`collectflow:collectflow`, UID 10001).
   - Features standard Uvicorn entrypoint on internal port 8000 and native Python standard-library healthcheck.
   - Hardened database configuration: added `read_secret_file()` supporting `COLLECTFLOW_DB_PASSWORD_FILE` and `COLLECTFLOW_DATABASE_URL_FILE`.
   - Closed port validation vulnerability (Trap 4): `parse_db_port()` strictly validates integer ranges 1–65535 and fails closed on invalid strings.

4. **Database & Network Isolation**:
   - PostgreSQL 16 (`postgres:16-alpine`) runs exclusively on an internal bridge network (`internal`) with named volume persistence (`pgdata`).
   - PostgreSQL port 5432 is **not published** to the host, ensuring total isolation from host services.
   - Database credentials are fed strictly via mounted Docker secrets (`/run/secrets/db_password`), completely eliminating cleartext passwords from environment variables and Git commits.

5. **Nginx Reverse Proxy & Same-Origin Routing**:
   - Reverse proxy (`nginx:1.27-alpine`) exposes `127.0.0.1:8080` (configurable via `APP_BIND_IP` and `APP_PORT`).
   - Routes `/` to `frontend:3000` and `/api/` to `backend:8000` with standard forwarding headers (`Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`).
   - Provides `/nginx-health` liveness probe.

---

## 2. Git Checkpoints and Working Trees

### Backend Repository: `collectflow`
- **Accepted WP-01 Baseline:** `1d2bfc9b8ded68e1ff06b6a398ecd70f5e7013c2`
- **Implementation Branch:** `feature/release-wp02-portable-containers`
- **Working Tree:** Clean (all temporary test secrets destroyed, no secrets committed).

### Frontend Repository: `cash-flow-buddy-101`
- **Accepted Phase 4C Baseline:** `2085c29e51d28cd5a2466883415442dd861734b7`
- **Implementation Branch:** `feature/release-wp02-portable-containers`
- **Working Tree:** Clean.

---

## 3. Detailed Component Architecture

### 3.1 Frontend (`cash-flow-buddy-101`)
- **`vite.config.ts`**:
  Configured `nitro: { preset: process.env.NITRO_PRESET || "node-server" }`. Default container builds output standard Node.js server bundles (`.output/server/index.mjs`), while `NITRO_PRESET=cloudflare-module` remains available for Cloudflare targets.
- **`src/lib/api-client.ts`**:
  `initFromEnv()` permits empty or omitted `VITE_API_BASE_URL` in LIVE mode without throwing configuration errors. `request()` checks `if (this.baseUrl === "")` and sends relative URLs (`/api/...`), allowing the browser to resolve requests against `window.location.origin`. Accurate inline comments document this verified relative-path behavior.
- **`Dockerfile` & `.dockerignore`**:
  Multi-stage build (`node:22-alpine` builder and runner), non-root user `collectflow` (UID 10001), port 3000 exposed, Alpine `wget` healthcheck on `/`.

### 3.2 Backend (`collectflow`)
- **`app/core/database.py`**:
  - `read_secret_file(file_path: str) -> str`: Safely reads secret files, stripping whitespace and newlines, and rejects empty files.
  - `get_db_password() -> str | None`: Checks `COLLECTFLOW_DB_PASSWORD` then `COLLECTFLOW_DB_PASSWORD_FILE`.
  - `get_custom_database_url() -> str | None`: Checks `COLLECTFLOW_DATABASE_URL` then `COLLECTFLOW_DATABASE_URL_FILE`.
  - `parse_db_port(raw_port: str | int | None) -> int`: Validates 1–65535, rejects invalid port strings ("bad_port", "-1", "70000").
  - `sanitize_credentials()`: Scans secret file password to redact it from all error traces and logs.
  - `validate_database_url()`: Confirms integration mode allows container host `postgres`.
- **`app/main.py`**:
  Added explicit `/api/health` and `/api/ready` route aliases for clean proxy routing.
- **`Dockerfile` & `.dockerignore`**:
  Multi-stage build (`python:3.12-slim` builder and runner), non-root user `collectflow` (UID 10001), `/app/data` created and chowned, port 8000 exposed, python `urllib.request` healthcheck on `/health`.

### 3.3 Compose Deployment (`collectflow/deploy/compose`)
- **`docker-compose.yml`**:
  - Configured proxy port binding to `"${APP_BIND_IP:-127.0.0.1}:${APP_PORT:-8080}:80"`.
  - Configured frontend build context to `${FRONTEND_BUILD_CONTEXT:-../../../cash-flow-buddy-101}`.
  - Added explicit security documentation warnings prohibiting public unencrypted exposure.
- **`nginx.conf`**: Same-origin reverse proxy configuration with explicit security notice and deployment scope documentation.
- **`migrate.sh`**: Single-command database migration script (`docker compose exec -T backend alembic upgrade head`).
- **`.env.example`** & **`secrets/db_password.txt.example`**: Clean configuration templates documenting `APP_BIND_IP`, `APP_PORT`, and `FRONTEND_BUILD_CONTEXT`.
- **`.gitignore`**: Strictly ignores real compose secrets (`secrets/*.txt`) and `.env`.

---

## 4. Verification & Validation Evidence

### 4.1 Unit & Contract Test Suites
- **Frontend Vitest Suite (`cash-flow-buddy-101`)**:
  - Result: **43/43 test files passed, 596/596 tests passed**.
  - Verified relative-path live configuration, reporting, UI flows, and financial models.
- **Backend Pytest Suite (`collectflow`)**:
  - Result: **142 passed, 71 skipped, 0 failed in 29.86s**.
  - Verified all database isolation, hardening, domain contracts, and financial flows.

### 4.2 Restrict Local Compose Exposure Verification (WP02-01)
- Started stack with default settings:
  ```bash
  docker compose -f deploy/compose/docker-compose.yml up -d
  ```
- Inspected container port bindings (`docker compose ps`):
  ```
  collectflow-proxy  ...  Up 8 seconds  127.0.0.1:8080->80/tcp
  ```
- Checked host socket listeners (`ss -tulpn | grep 8080`):
  ```
  tcp  LISTEN  0  4096  127.0.0.1:8080  0.0.0.0:*
  ```
  Confirmed: port 8080 is strictly bound to `127.0.0.1` and does **NOT** listen on `0.0.0.0` or `[::]`.

### 4.3 Same-Origin Reverse Proxy Probing
Probed Nginx on host `http://127.0.0.1:8080`:
- `GET /nginx-health` -> `HTTP/1.1 200 OK`, body: `healthy`
- `GET /api/health` -> `HTTP/1.1 200 OK`, body: `{"status":"healthy","service":"CollectFlow","version":"1.0.0"}`
- `GET /api/ready` -> `HTTP/1.1 200 OK`, body: `{"status":"ready","database":"connected"}` (verified live DB connection)
- `GET /` -> `HTTP/1.1 200 OK`, content-type: `text/html; charset=utf-8`
- `POST /api/auth/login` with dummy credentials -> `HTTP/1.1 401 Unauthorized`, body: `{"detail":"Invalid username or password"}`
- `GET /api/meta` unauthenticated -> `HTTP/1.1 401 Unauthorized`, body: `{"detail":"Authentication required"}`

### 4.4 Standalone Container Security Probing
- **Frontend Container**: Verified user: `uid=10001(collectflow) gid=10001(collectflow)`, HTTP 200 on port 3000.
- **Backend Container**: Verified user: `uid=10001(collectflow) gid=10001(collectflow)`, HTTP 200 on `/health`.

### 4.5 State Persistence & Clean Teardown
- Applied Alembic migrations: `create_collectflow_v1_domain_schema` (`e9ee322f8b96`).
- Verified database state persistence across container stop and start.
- Executed clean teardown (`docker compose down -v`) and confirmed zero temporary secrets or volumes remain.

---

## 5. Scope Boundary Compliance

| Requirement / Boundary | Status | Evidence |
|---|---|---|
| Cloud-agnostic OCI containers | COMPLIANT | Standard Dockerfile in both repositories |
| Same-origin proxy routing | COMPLIANT | Nginx reverse proxy routes `/` and `/api/` |
| Local loopback bind default | COMPLIANT | `APP_BIND_IP:-127.0.0.1` verified via `ss` and `docker compose ps` |
| Public AWS deployment prohibited | COMPLIANT | Explicitly documented in `docker-compose.yml`, `nginx.conf`, `.env.example` |
| Sibling repo layout configurable | COMPLIANT | `FRONTEND_BUILD_CONTEXT` in `docker-compose.yml` and `.env.example` |
| Non-root container execution | COMPLIANT | UID 10001 `collectflow` in both images |
| Docker secret files used | COMPLIANT | `POSTGRES_PASSWORD_FILE` & `COLLECTFLOW_DB_PASSWORD_FILE` |
| Port validation fail-closed | COMPLIANT | `parse_db_port` rejects invalid strings |
| Preserved accepted baseline | COMPLIANT | Both branches cleanly forked from accepted commits |
| No WP-03 / WP-04 / WP-05 work | COMPLIANT | User initialization, cookie auth, AWS scripts omitted |
| No secrets committed to Git | COMPLIANT | Secrets file ignored; only `.example` committed |
