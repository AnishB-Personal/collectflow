# CollectFlow Post-Phase 4C Release Readiness Plan

**Document Reference:** `COLLECTFLOW_POST_4C_RELEASE_READINESS_PLAN.md`  
**Stage:** Post-Phase 4C Release Readiness Preflight  
**Date:** October 18, 2026 (Revised per Product Owner Cloud-Agnostic Direction)  
**Status:** DRAFT — AUTHORIZED FOR PLANNING AND ARCHITECTURE REVIEW  
**Recommendation Status:** `NEEDS SECURITY / ARCHITECTURE DECISIONS`  

---

## 1. Executive Summary & Verified Baselines

Phase 4C (Stages 4C.1 through 4C.6) concluded with an independent verification verdict of **ACCEPTANCE WITH LIMITATIONS**, establishing full PostgreSQL 16 relational parity, a 39-scenario Playwright LIVE UAT suite, and zero monetary delta ($\Delta = ₹0.00$) five-source financial reconciliation across direct database models, REST APIs, DOM-rendered figures, and 17-column Excel/CSV exports.

### Product Owner Strategic Mandate: Cloud-Agnostic & On-Premises Deployable
Per Product Owner directive, **CollectFlow must be 100% cloud-agnostic and deployable on-premises, in private clouds, or across any major public cloud (AWS, Azure, GCP)** without modifying application source code or business logic:
- The previous Cloudflare Workers environment was a development/UAT staging adapter, **not** the permanent or mandatory production architecture.
- The Cloudflare Tunnel + AWS ECS/RDS pattern is retained strictly as an **optional reference implementation**, not a mandatory dependency.
- The core application packaging must be based on standard **OCI container images (Docker)**, standard **PostgreSQL 16+**, and generic **HTTP/S reverse proxy ingress** (Nginx, Traefik, or Kubernetes Ingress).
- **Zero proprietary cloud SDKs** (e.g., AWS Boto3, Cloudflare SDKs) are permitted within application business logic. All configuration and secret delivery must use provider-neutral POSIX environment variables.

### Authoritative Git Baselines & Planning Branches
- **Backend Repository:** [`collectflow`](file:///home/anishb/projects/personal/collectflow)
  - Accepted Baseline Commit: `1eb94b96321780d42527721585551d35f774fa3b`
  - Planning Branch: `docs/phase4-release-readiness-preflight`
  - Verified Remote Planning Checkpoint: `e5f50a99df294ccdff48ff7dcac20a4376490067`
- **Frontend Repository:** [`cash-flow-buddy-101`](file:///home/anishb/projects/personal/cash-flow-buddy-101)
  - Accepted Baseline Commit: `2085c29e51d28cd5a2466883415442dd861734b7`
  - Planning Branch: `docs/phase4-release-readiness-preflight`
  - Verified Remote Planning Checkpoint: `f65696f2e5f6ccf3f9678be73560befe09ec965b`

---

## 2. Superseded vs. Authoritative Architecture Decisions

| Domain | Historical Proposal (Stage 4A/4B) | Accepted Authoritative Architecture (Stage 4C.6 / Revised) | Status & Justification |
|---|---|---|---|
| **Hosting Target** | Cloudflare Workers edge required for production | **Cloud-Agnostic OCI Containers** with generic Ingress. Cloudflare and AWS ECS become optional reference adapters. | **AUTHORITATIVE DIRECTION**. Eliminates vendor lock-in; enables on-premises enterprise deployment. |
| **Frontend Packaging** | Cloudflare-only Nitro bundle (`cloudflare-module`) | **Dual-Target Nitro Packaging**: Portable Node.js SSR container (`node-server` preset) as default; Cloudflare as optional adapter. | **PORTABLE REVISION**. Allows deployment on generic Linux, Docker, or Kubernetes. |
| **Persistence Engine** | SQLite WAL-mode with `BEGIN IMMEDIATE` locks | Dedicated PostgreSQL 16+ (Self-hosted or Managed) with SQLAlchemy 2.0 ORM, psycopg3 driver, and Alembic migrations. | **AUTHORITATIVE** (Stage 4C.1). Multi-tenant scalability, ACID transactions, and strict typing. |
| **Payment Mutation** | Physical deletion of erroneous payments allowed | Strict physical deletion block (`DELETE /api/payments/{id}` returns HTTP 400); all adjustments execute via non-destructive compensating `REVERSAL` events. | **AUTHORITATIVE** (Stage 4C.4). Statutory compliance under Indian accounting standards. |
| **Export Security** | Direct bearer token accepted in download query | Single-use cryptographic tickets issued via `POST /api/export/token` with 5-minute TTL, consumed atomically under PostgreSQL `SELECT FOR UPDATE`. | **AUTHORITATIVE** (Stage 4C.5). Eliminates token leakage in access logs and prevents link sharing. |
| **Reporting Aggregation** | Client-side aggregation of `GET /api/invoices?limit=1000` | Authoritative server-side aggregation endpoints (`/api/reporting/summary`, `/api/reporting/ar-aging`, `/api/payments`) with server pagination. | **AUTHORITATIVE** (Stage 4C.5). Prevents 100-invoice truncation and browser memory overflow. |
| **Auth Session Storage** | Browser `sessionStorage` proposed in Stage 4A | Same-origin `HttpOnly; Secure; SameSite=Lax` cookie session independent of ingress provider. In-memory state preserved in SPA. | **EVOLVED**. Solves page-reload session loss while preventing XSS token exfiltration. |

---

## 3. Core Application Packaging & Frontend Runtime Portability

### 3.1 Containerized OCI Application Packaging
CollectFlow is packaged into two portable OCI-compliant container images:

1. **Backend Container (`collectflow-api`):**
   - **Base Image:** `python:3.12-slim-bookworm`
   - **Runtime:** Uvicorn ASGI server running FastAPI application.
   - **Dependencies:** Pinned production manifest (`SQLAlchemy 2.0+`, `psycopg 3.3+`, `alembic`, `argon2-cffi`, `pydantic 2.0+`).
   - **Port:** Exposes internal HTTP port `8000`.
   - **Process Isolation:** Runs as unprivileged `appuser` (UID 10001).

2. **Frontend Container (`collectflow-web`):**
   - **Base Image:** `node:22-bookworm-slim`
   - **Runtime:** Node.js HTTP server running the TanStack Start SSR application.
   - **Port:** Exposes internal HTTP port `3000`.
   - **Process Isolation:** Runs as unprivileged `node` user.

3. **Database Tier:**
   - **Requirement:** Standard PostgreSQL 16+ with UTF-8 encoding.
   - **Deployment Agnostic:** Compatible with self-hosted PostgreSQL (bare metal or container) or managed cloud databases (AWS RDS, GCP Cloud SQL, Azure Database for PostgreSQL).

4. **Ingress / Reverse Proxy Tier:**
   - **Requirement:** Standard HTTP/S reverse proxy terminating TLS 1.3 and unifying routing under a single origin:
     - `/*` $\rightarrow$ Frontend container (`port 3000`).
     - `/api/*` $\rightarrow$ Backend container (`port 8000`).
   - **Implementations:** Nginx, Traefik, Caddy, Kubernetes Ingress Controller, or Cloudflare Gateway.

```mermaid
flowchart TD
    subgraph Client ["Client / Browser"]
        User["End User Browser"]
    end

    subgraph Ingress ["Generic Ingress / Reverse Proxy (Nginx / Ingress Controller / Caddy)"]
        TLS["TLS 1.3 Termination (Certbot / Custom CA / Enterprise Cert)"]
        Router{"Path-Based Reverse Proxy"}
    end

    subgraph Containers ["Portable OCI Container Workloads"]
        WebContainer["collectflow-web (Node.js 22 / TanStack Start SSR:3000)"]
        APIContainer["collectflow-api (Python 3.12 / FastAPI Uvicorn:8000)"]
    end

    subgraph Storage ["PostgreSQL 16+ Persistence Tier"]
        DB[("PostgreSQL 16 Database (Self-Hosted / Managed RDS / Cloud SQL)")]
    end

    User -->|HTTPS 443| TLS
    TLS --> Router
    Router -->|/* (SSR & Static Assets)| WebContainer
    Router -->|/api/* (REST API Calls)| APIContainer
    APIContainer -->|psycopg3 TCP/SSL Connection Pool| DB
```

### 3.2 Frontend Runtime Portability Assessment (TanStack Start / Nitro)
The frontend repository ([`cash-flow-buddy-101`](file:///home/anishb/projects/personal/cash-flow-buddy-101)) currently uses `@lovable.dev/vite-tanstack-config` which defaults Nitro to Cloudflare output (`cloudflare-module`).

#### Assessment & Portable Target Specification:
- **Nitro Multi-Preset Capability:** Nitro (UnJS) natively supports multiple deployment presets via the `NITRO_PRESET` environment variable.
- **Primary Portable Target:** `node-server`
  - Running `NITRO_PRESET=node-server npm run build` compiles the application into a standalone Node.js server located in `.output/server/index.mjs`.
  - The resulting output runs via standard `node .output/server/index.mjs` listening on `PORT=3000`.
  - Fully compatible with standard Docker, Kubernetes, Linux VMs, and on-premises servers.
- **Optional Edge Adapter:** `cloudflare-module`
  - Retained as a build option (`NITRO_PRESET=cloudflare-module`) for teams electing to use Cloudflare Workers.
- **Build-Time vs. Runtime Configuration Separation:**
  - *Build-Time (Static):* React route tree, Tailwind CSS compilation, client asset hashing.
  - *Runtime (Dynamic):* Injected via standard environment variables:
    - `PORT`: HTTP port to bind (default `3000`).
    - `HOST`: IP to bind (default `0.0.0.0`).
    - `VITE_API_MODE`: `"live"`.
    - `VITE_API_BASE_URL`: Left empty (empty string) to leverage same-origin `/api` routing via the reverse proxy, eliminating CORS.

---

## 4. Supported Deployment Profiles

To fulfill the Product Owner's portability mandate, three deployment profiles are formally supported:

### Profile 1: On-Premises / Generic Linux Docker Compose (Reference Profile)
Ideal for on-premises enterprise data centers, private clouds, air-gapped installations, or small-scale self-hosted environments.

```mermaid
flowchart TD
    subgraph Host ["Single Linux Host / VM (Ubuntu / RHEL / Debian)"]
        subgraph DockerNetwork ["Internal Docker Bridge Network (collectflow-net)"]
            Nginx["nginx:alpine (Ports 80/443 Public)"]
            Web["collectflow-web:v1.0.0 (Port 3000 Internal)"]
            API["collectflow-api:v1.0.0 (Port 8000 Internal)"]
            PG[("postgres:16-alpine (Port 5432 Internal)")]
            Volume[("Named Docker Volume: pgdata")]
        end
    end

    Internet["Corporate LAN / Internet"] -->|HTTPS 443| Nginx
    Nginx -->|/*| Web
    Nginx -->|/api/*| API
    API -->|TCP 5432| PG
    PG --- Volume
```

#### Docker Compose Specification (`docker-compose.prod.yml`):
```yaml
version: "3.8"

networks:
  collectflow-net:
    driver: bridge

volumes:
  pgdata:
    driver: local

services:
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    networks:
      - collectflow-net
    environment:
      POSTGRES_DB: ${COLLECTFLOW_DB_NAME:-collectflow}
      POSTGRES_USER: ${COLLECTFLOW_DB_USER:-collectflow_user}
      POSTGRES_PASSWORD_FILE: /run/secrets/db_password
    secrets:
      - db_password
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${COLLECTFLOW_DB_USER:-collectflow_user}"]
      interval: 10s
      timeout: 5s
      retries: 5

  backend:
    image: collectflow-api:v1.0.0
    restart: unless-stopped
    networks:
      - collectflow-net
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      COLLECTFLOW_ENV: production
      COLLECTFLOW_DATABASE_URL: postgresql+psycopg://${COLLECTFLOW_DB_USER}:${DB_PASSWORD}@postgres:5432/${COLLECTFLOW_DB_NAME}
      COLLECTFLOW_SECRET_KEY_FILE: /run/secrets/app_secret
    secrets:
      - db_password
      - app_secret
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/ready"]
      interval: 10s
      timeout: 5s
      retries: 3

  frontend:
    image: collectflow-web:v1.0.0
    restart: unless-stopped
    networks:
      - collectflow-net
    environment:
      NODE_ENV: production
      PORT: 3000
      HOST: 0.0.0.0
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3000/"]
      interval: 15s
      timeout: 5s
      retries: 3

  nginx:
    image: nginx:alpine
    restart: unless-stopped
    networks:
      - collectflow-net
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./nginx/ssl:/etc/nginx/ssl:ro
    depends_on:
      - backend
      - frontend

secrets:
  db_password:
    file: ./secrets/db_password.txt
  app_secret:
    file: ./secrets/app_secret.txt
```

---

### Profile 2: Cloud-Native Kubernetes / Helm (Enterprise Cluster Profile)
Ideal for enterprise organizations running Kubernetes on bare metal, OpenShift, or managed public cloud clusters (AWS EKS, GCP GKE, Azure AKS).

- **Packaging:** Standard Helm Chart (`collectflow/`).
- **Workloads:**
  - `Deployment`: `collectflow-web` (replicas: 2+, horizontal pod autoscaling).
  - `Deployment`: `collectflow-api` (replicas: 2+, horizontal pod autoscaling).
  - `Ingress`: `ingress-nginx` or Traefik with automated cert-manager TLS certificates.
  - `Service`: ClusterIP services routing internal traffic.
  - `Secrets`: Standard Kubernetes `Secret` resources or External Secrets Operator integration (HashiCorp Vault, AWS Secrets Manager, Azure Key Vault).
  - `Database`: External high-availability PostgreSQL cluster or Cloud-native Operator (e.g. CloudNativePG, Crunchy Data PostgreSQL).

---

### Profile 3: Optional Managed Cloud Reference Implementations
Preserved as optional cloud-specific deployment patterns for organizations with existing cloud provider commitments:
- **AWS Reference Architecture:** AWS ECS Fargate (API & Web) + AWS RDS PostgreSQL 16 Multi-AZ + Application Load Balancer (or Cloudflare Tunnel for edge protection).
- **GCP Reference Architecture:** GCP Cloud Run (API & Web) + GCP Cloud SQL PostgreSQL 16 + Google Cloud Load Balancing.
- **Azure Reference Architecture:** Azure Container Apps + Azure Database for PostgreSQL Flexible Server.

---

## 5. Portable Infrastructure & Provider-Neutral Operations

### 5.1 Infrastructure as Code (IaC) Decoupling
To enforce provider neutrality:
- Application source repositories (`collectflow`, `cash-flow-buddy-101`) contain **zero provider-specific Terraform code**.
- Infrastructure automation is maintained in separate, modular repositories:
  - `collectflow-deploy/compose/`: Portable Docker Compose deployment templates.
  - `collectflow-deploy/helm/`: Universal Kubernetes Helm chart.
  - `collectflow-deploy/terraform/aws/`: Optional AWS ECS/RDS module.
  - `collectflow-deploy/terraform/gcp/`: Optional GCP Cloud Run/Cloud SQL module.

### 5.2 Provider-Neutral Security & Authentication
1. **Same-Origin Cookie Authentication:**
   - Functions identically across all deployment profiles. The reverse proxy (Nginx, Traefik, or Cloudflare) unifies the origin under `app.yourdomain.com`.
   - Cookie directive:
     ```http
     Set-Cookie: collectflow_session=<token_secret>; Path=/api; HttpOnly; Secure; SameSite=Lax; Max-Age=604800
     ```
   - Ingress-agnostic CSRF protection via required custom header: `X-Requested-With: XMLHttpRequest`.
2. **Provider-Neutral Secret Delivery:**
   - The application expects secrets via standard POSIX environment variables (e.g., `COLLECTFLOW_DATABASE_URL`, `COLLECTFLOW_SECRET_KEY`) or file mounts (`*_FILE`).
   - Secret injection is decoupled from the runtime: Docker Compose uses Docker secrets; Kubernetes uses K8s Secret volumes; Cloud environments use native task injection.
   - Initial bootstrap administrator password is delivered securely via container environment injection or secure file volume, and is **never printed to stdout, console, or build logs**.
3. **Database Schema Prerequisite:**
   - Additive Alembic migration adding `must_change_password` to `users` table is required before forced password rotation can be enforced.

### 5.3 Portable PostgreSQL Backup & Restore Strategy
Applicable equally to on-premises Linux servers, Docker volumes, Kubernetes persistent storage, and managed cloud databases:
1. **Standardized Logical Backups:**
   - Automated scheduled `pg_dump` job:
     ```bash
     pg_dump -Fc -v -d "${COLLECTFLOW_DATABASE_URL}" -f "/backups/collectflow_$(date +%Y%m%d_%H%M%S).dump"
     ```
   - Compatible with local storage, NFS mounts, AWS S3, Azure Blob, or Google Cloud Storage.
2. **Physical WAL Archiving & Point-In-Time Recovery (PITR):**
   - *Self-Hosted / On-Premises:* Configured via `pgBackRest` or `Barman` pushing WAL archives to local/remote storage.
   - *Managed Cloud:* Automated provider backup archiving (RDS / Cloud SQL).
3. **Restoration Governance:**
   - Restoring a snapshot or dump resets data state; transactions between the snapshot and failure point must be reconciled externally.
   - **Mandatory Policy:** Database restores require **joint written authorization from the Head of Finance and Product Owner**.

---

## 6. Portability & Release Acceptance Matrix

The release candidate must be validated against the following multi-profile portability matrix:

| Deployment Profile | Target Platform | Ingress Technology | Database Implementation | Portability Status |
|---|---|---|---|---|
| **Profile 1 (On-Premises)** | Bare-metal Linux (Ubuntu/RHEL) | Nginx Reverse Proxy | Self-hosted PostgreSQL 16 (Container/Host) | **PRIMARY PORTABLE GATE** |
| **Profile 1 (Private Cloud)** | Enterprise VMware / Nutanix VM | Traefik Reverse Proxy | Dedicated PostgreSQL 16 VM | **PRIMARY PORTABLE GATE** |
| **Profile 2 (Kubernetes)** | Vanilla K8s / K3s / OpenShift | `ingress-nginx` Controller | CloudNativePG Operator / External PG | **ENTERPRISE CLUSTER GATE** |
| **Profile 3 (AWS Reference)** | AWS ECS Fargate | Internal ALB + Cloudflare Tunnel | AWS RDS PostgreSQL 16 Multi-AZ | **OPTIONAL CLOUD ADAPTER** |
| **Profile 3 (GCP Reference)** | GCP Cloud Run | Google Cloud HTTPS Load Balancer | GCP Cloud SQL PostgreSQL 16 | **OPTIONAL CLOUD ADAPTER** |
| **Air-Gapped Offline** | Isolated LAN (No Internet) | Nginx + Local SSL Certs | Local PostgreSQL 16 | **AIR-GAPPED COMPLIANT** |

---

## 7. Comparative Monthly Cost Breakdown Across Profiles

*The following comparative estimates illustrate infrastructure economics across supported deployment profiles:*

| Cost Component | Profile 1: On-Premises / Single VM | Profile 2: Managed Kubernetes (EKS/GKE) | Profile 3: AWS ECS + RDS + Cloudflare |
|---|---|---|---|
| **Compute** | $40 – $80 / mo (1 Dedicated 4-vCPU VM) | $90 – $140 / mo (2 Worker Nodes) | $48 – $64 / mo (ECS Fargate Tasks) |
| **Ingress / Load Balancer** | $0 (Included in Nginx container) | $20 – $30 / mo (Cloud Load Balancer) | $22 – $28 / mo (Internal ALB) |
| **Database** | $0 (Self-hosted on VM with EBS/disk) | $70 – $120 / mo (Managed DB or Stateful) | $135 – $155 / mo (RDS Multi-AZ) |
| **Edge / WAF** | $0 (Corporate firewall / local WAF) | $0 – $25 / mo (Ingress WAF plugin) | $20 – $30 / mo (Cloudflare Workers/WAF) |
| **Backups & Networking** | $10 – $20 / mo (S3 / Local NAS backup) | $25 – $40 / mo (Storage + Egress) | $80 – $105 / mo (NAT Gateways + S3) |
| **Total Estimated Run-Rate** | **$50 – $100 / month** | **$205 – $355 / month** | **$305 – $382 / month** |

---

## 8. Release-Readiness Sequencing & Reconciled Work Packages

The release readiness sequencing prioritizes provider-neutral packaging and core application hardening before any cloud-specific tooling:

```mermaid
flowchart TD
    subgraph Phase_A ["Phase A: Core Portability & Hardening (Mandatory)"]
        WP1[WP-01: Dependency & Isolation Hardening] --> WP2[WP-02: Portable OCI Container Packaging]
        WP2 --> WP3[WP-03: User Model Migration & Production Initializer]
        WP3 --> WP4[WP-04: Provider-Neutral Cookie Auth & CSRF]
    end

    subgraph Phase_B ["Phase B: Portability Verification & Governance"]
        WP4 --> WP5[WP-05: Docker Compose & K8s Portability Suite]
        WP4 --> WP6[WP-06: Business Policy Technical Gates]
        WP6 --> BPD[Formal Sign-Off on Decision Register]
    end

    subgraph Phase_C ["Phase C: Optional Cloud Adapters (On-Demand)"]
        BPD --> WP7[WP-07 Optional: AWS / GCP / Azure Terraform Modules]
    end
```

### Work Package Definitions (WP-01 to WP-07):
- **WP-01: Dependency & Environment Isolation Hardening (Backend)**
  - Update `requirements.txt` with pinned versions of SQLAlchemy 2.0, psycopg3, Alembic, and Argon2-cffi.
  - Refactor `app/core/database.py` to support `COLLECTFLOW_ENV=production` with standard TCP/SSL connections to any PostgreSQL 16 host (local, container, or remote).
- **WP-02: Portable OCI Container Packaging (Frontend & Backend)**
  - Author multi-stage Dockerfile for Backend (`python:3.12-slim`).
  - Configure frontend Nitro preset to compile with `NITRO_PRESET=node-server` and author multi-stage Dockerfile (`node:22-slim`).
  - Author `docker-compose.prod.yml` with Nginx reverse proxy.
- **WP-03: User Model Migration & Production Initializer**
  - Create Alembic migration adding `must_change_password` column to `users`.
  - Author `scripts/init_production_db.py` to seed clean statutory reference data and bootstrap one primary administrator.
  - Ingest bootstrap credentials via secure environment variable / Secrets volume (zero stdout printing).
  - Add hard safety abort to `scripts/seed_postgres_fixtures.py` preventing execution outside `COLLECTFLOW_ENV=test`.
- **WP-04: Provider-Neutral Same-Origin Cookie Authentication & CSRF Hardening**
  - Implement `HttpOnly; Secure; SameSite=Lax` cookie issuance and renewal in FastAPI.
  - Add Origin / Referer validation and custom CSRF header check (`X-Requested-With: XMLHttpRequest`).
  - Update frontend `ApiClient` to leverage cookie sessions while preserving in-memory user profile state.
  - Implement negative test battery validating CSRF and origin rejection.
- **WP-05: Portability Verification Suite & CI Automation**
  - Implement `.github/workflows/ci.yml` (lint, typecheck, pytest, vitest).
  - Implement automated Docker Compose startup and healthcheck validation in CI.
  - Author Helm chart for Kubernetes deployment testing.
- **WP-06: Business Policy Technical Gates & Domestic INR Scoping**
  - Implement INR-only validation gate on purchase orders and invoices (`currency == "INR"`, `exchange_rate == 1.000000`).
  - Update Customer Directory and Reports Hub to display inactive customer exposure badges.
- **WP-07 (Optional): Cloud-Specific Reference Modules**
  - Author Terraform modules for AWS ECS/RDS and Cloudflare Tunnel only if AWS deployment is explicitly selected.
