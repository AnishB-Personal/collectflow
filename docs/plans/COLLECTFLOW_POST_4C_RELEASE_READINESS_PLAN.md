# CollectFlow Post-Phase 4C Release Readiness Plan

**Document Reference:** `COLLECTFLOW_POST_4C_RELEASE_READINESS_PLAN.md`  
**Stage:** Post-Phase 4C Release Readiness Preflight (Planning & Read-Only Verification)  
**Date:** October 18, 2026  
**Status:** DRAFT — AUTHORIZED FOR PLANNING AND ARCHITECTURE REVIEW  
**Recommendation Status:** `NEEDS SECURITY / ARCHITECTURE DECISIONS`  

---

## 1. Executive Summary & Verified Baselines

Phase 4C (Stages 4C.1 through 4C.6) concluded with an independent verification verdict of **ACCEPTANCE WITH LIMITATIONS**, establishing full PostgreSQL 16 relational parity, a 39-scenario Playwright LIVE UAT suite, and zero monetary delta ($\Delta = ₹0.00$) five-source financial reconciliation across direct database models, REST APIs, DOM-rendered figures, and 17-column Excel/CSV exports.

This preflight document establishes a technical roadmap, risk evaluation, and release governance framework for transitioning the complete PostgreSQL-backed CollectFlow application from a local verified test environment into an enterprise staging and production deployment.

### Authoritative Git Baselines & Planning Branches
- **Backend Repository:** [`collectflow`](file:///home/anishb/projects/personal/collectflow)
  - Accepted Baseline Commit: `1eb94b96321780d42527721585551d35f774fa3b`
  - Dedicated Planning Branch: `docs/phase4-release-readiness-preflight`
- **Frontend Repository:** [`cash-flow-buddy-101`](file:///home/anishb/projects/personal/cash-flow-buddy-101)
  - Accepted Baseline Commit: `2085c29e51d28cd5a2466883415442dd861734b7`
  - Dedicated Planning Branch: `docs/phase4-release-readiness-preflight`

> [!IMPORTANT]
> **Preflight Constraints:** No cloud infrastructure has been provisioned, no code merged to `main`, no database migrations executed in production, and no application behavior modified during this assessment.

---

## 2. Superseded vs. Authoritative Architecture Decisions

A rigorous review of historical design documentation (Stage 4A through Stage 4C.6) identifies several proposals that have been superseded by subsequent stage implementations:

| Domain | Historical Proposal (Stage 4A/4B) | Accepted Authoritative Architecture (Stage 4C.6) | Status & Justification |
|---|---|---|---|
| **Persistence Engine** | SQLite WAL-mode with `BEGIN IMMEDIATE` locks | Dedicated PostgreSQL 16+ with SQLAlchemy 2.0 ORM, psycopg3 driver, and Alembic migrations | **SUPERSEDED** by Stage 4C.1 for multi-tenant scalability and strict typing. |
| **Payment Mutation** | Physical deletion of erroneous payments allowed in development | Strict physical deletion block (`DELETE /api/payments/{id}` returns HTTP 400); all adjustments execute via non-destructive compensating `REVERSAL` events | **AUTHORITATIVE** (Stage 4C.4). Statutory compliance under Indian accounting standards. |
| **Export Security** | Direct bearer token accepted in download query parameter | Single-use cryptographic tickets issued via `POST /api/export/token` with 5-minute TTL, consumed atomically under PostgreSQL `SELECT FOR UPDATE` | **AUTHORITATIVE** (Stage 4C.5). Eliminates token leakage in access logs and prevents download link sharing. |
| **Reporting Aggregation** | Client-side aggregation of `GET /api/invoices?limit=1000` | Authoritative server-side aggregation endpoints (`/api/reporting/summary`, `/api/reporting/ar-aging`, `/api/payments`) with server pagination | **AUTHORITATIVE** (Stage 4C.5). Prevents 100-invoice truncation and browser memory overflow. |
| **Auth Session Storage** | Browser `sessionStorage` proposed in Stage 4A | Ephemeral in-memory React state (`inMemoryToken`) in Stage 4B/4C; `HttpOnly` same-origin cookies proposed for Production | **EVOLVED**. In-memory tokens protect against persistent XSS but require same-origin cookie architecture for production persistence across reloads. |

---

## 3. Production Hosting Architecture Assessment

CollectFlow consists of two decoupled application tiers:
1. **Frontend:** React 19 SPA built via TanStack Router and Vite, packaged for Nitro/Cloudflare runtime.
2. **Backend:** FastAPI application running on Python 3.12, utilizing Uvicorn, SQLAlchemy 2.0, and PostgreSQL 16+.

### Hosting Options Evaluation

```mermaid
flowchart TD
    subgraph Client Tier
        Browser["User Browser / Client"]
    end

    subgraph Edge / Gateway Tier ["Cloudflare Edge & Gateway (app.collectflow.io)"]
        WAF["Cloudflare WAF & DDoS Shield"]
        DNS["Authoritative DNS / TLS 1.3 Termination"]
        Router{"Reverse Proxy & Path Router"}
        EdgeFrontend["Cloudflare Pages / Workers (Frontend Assets & SSR)"]
    end

    subgraph Application Tier ["Private AWS VPC / VPC Peering"]
        ALB["Internal Application Load Balancer"]
        FastAPI1["FastAPI Container 1 (ECS Fargate)"]
        FastAPI2["FastAPI Container 2 (ECS Fargate)"]
    end

    subgraph Data Tier ["Isolated Private Database Subnet"]
        RDSPrimary[("AWS RDS PostgreSQL 16 (Primary Writer)")]
        RDSStandby[("AWS RDS PostgreSQL 16 (Multi-AZ Standby)")]
        S3Backups[("Encrypted S3 Automated Backups & WAL PITR")]
    end

    Browser -->|HTTPS / TLS 1.3| WAF
    WAF --> DNS
    DNS --> Router
    Router -->|/* (Static / SSR Assets)| EdgeFrontend
    Router -->|/api/* (Reverse Proxy with Cookies)| ALB
    ALB --> FastAPI1
    ALB --> FastAPI2
    FastAPI1 -->|psycopg3 Connection Pool| RDSPrimary
    FastAPI2 -->|psycopg3 Connection Pool| RDSPrimary
    RDSPrimary -.->|Synchronous Replication| RDSStandby
    RDSPrimary -.->|Automated Snapshots & WAL Streams| S3Backups
```

#### Option A: Edge Frontend + Managed Serverless Containers & RDS (Recommended)
- **Frontend:** Cloudflare Pages (or Cloudflare Workers) delivering edge-cached static bundles and TanStack SSR.
- **Gateway & Routing:** Cloudflare Custom Routing / Cloudflare Tunnel mapping `app.collectflow.io` to frontend edge, and reverse-proxying `app.collectflow.io/api/*` to the backend origin.
- **Backend:** AWS ECS Fargate (or GCP Cloud Run) containerized FastAPI service deployed in private subnets across two Availability Zones (min 2 instances for high availability, auto-scaling to 6).
- **Database:** AWS RDS PostgreSQL 16 Multi-AZ (`db.t4g.medium`, 100 GB gp3 with storage autoscaling).
- **Secrets Management:** AWS Secrets Manager / Doppler.
- **Evaluation:**
  - *Security:* **Superior**. Same-origin routing eliminates cross-site CORS exposures and permits `HttpOnly; Secure; SameSite=Strict` cookies. Database has zero public internet exposure.
  - *Operational Complexity:* Moderate. Requires container build pipeline and Terraform VPC management.
  - *Rollback:* Fast. Instant container task definition reversion and Cloudflare Pages deployment rollbacks.
  - *Maintainability:* High. Zero host OS patching; fully managed database maintenance.

#### Option B: Unified Single-Cloud PaaS (e.g., Render / DigitalOcean App Platform)
- **Frontend & Backend:** Monolithic PaaS deployment where frontend static site and backend web service run within the same provider project.
- **Database:** Provider-managed PostgreSQL 16.
- **Evaluation:**
  - *Security:* Moderate. Lacks fine-grained AWS IAM roles, VPC transit gateways, and dedicated enterprise WAF inspection.
  - *Operational Complexity:* Low. Single configuration file, push-to-deploy workflow.
  - *Rollback:* Moderate. Dependent on PaaS provider deployment hooks.
  - *Maintainability:* Moderate. Less flexibility for enterprise network isolation and audit retention.

### Architecture Recommendation
**Option A is strongly recommended.** Financial applications handling accounts receivable, statutory tax calculation, and payment transactions require enterprise-grade network segmentation, zero-trust database isolation, and automated point-in-time recovery.

### Estimated Monthly Cost Assumptions (Planning Estimates Only)
*The following figures represent approximate infrastructure estimates and must NOT be interpreted as binding provider quotes:*

| Component | Specification / Provider | Estimated Cost (USD/month) |
|---|---|---|
| **Frontend & Edge Gateway** | Cloudflare Pro Plan + Pages / Workers | $25 – $40 |
| **Backend Compute** | AWS ECS Fargate (2 tasks: 0.5 vCPU, 1 GB RAM, 24/7) | $35 – $55 |
| **Application Load Balancer** | AWS ALB (1 ALB with LCUs) | $22 – $30 |
| **Managed Database** | AWS RDS PostgreSQL 16 Multi-AZ (`db.t4g.medium`, 100 GB gp3) | $130 – $160 |
| **Backup Storage & PITR** | AWS S3 Standard + Glacier (Snapshots + WAL archive) | $10 – $20 |
| **Secrets & Telemetry** | AWS Secrets Manager + CloudWatch Logs / Datadog basic | $20 – $35 |
| **Total Estimated Run-Rate** | **High-Availability Production Environment** | **$242 – $340 / month** |

---

## 4. Authentication & Session Security Architecture

### 4.1 Current Implementation Analysis (Stage 4B / 4C)
- The frontend holds the session token exclusively in memory (`ApiClient.inMemoryToken`).
- While this successfully prevents token theft from persistent web storage (`localStorage` / `sessionStorage`), it has a critical operational flaw for real users: **any page refresh or browser reload immediately wipes the token and forces the user back to `/login`**.
- The backend accepts `Authorization: Bearer <token>` and validates the SHA-256 hash against the `sessions` table.

### 4.2 Production Target: Same-Origin Cookie Authentication
To resolve the page-refresh friction while maintaining zero XSS exposure, the production architecture must adopt **same-origin cookie-based session management**:

1. **Reverse Proxy Unification:**
   - The user visits `https://app.collectflow.io`.
   - The gateway proxies `https://app.collectflow.io/api/*` to the FastAPI backend.
   - Because the frontend and backend share the exact origin, third-party cookie restrictions do not apply.

2. **Session Cookie Directive:**
   - On successful login (`POST /api/auth/login`), the backend responds with a `Set-Cookie` header:
     ```http
     Set-Cookie: collectflow_session=<token_secret>; Path=/api; HttpOnly; Secure; SameSite=Lax; Max-Age=604800
     ```
   - `HttpOnly`: Completely inaccessible to client-side JavaScript (`document.cookie` returns nothing), mitigating XSS exfiltration.
   - `Secure`: Transmitted strictly over TLS 1.3 HTTPS connections.
   - `SameSite=Lax`: Blocks cross-site request transmission on cross-origin POSTs while supporting top-level navigation.

3. **CSRF Mitigation:**
   - All state-mutating requests (`POST`, `PUT`, `DELETE`) require a custom header: `X-Requested-With: XMLHttpRequest` or `X-CollectFlow-CSRF: 1`.
   - Browsers forbid cross-origin forms or images from setting custom headers without a preflight CORS approval. Since the backend CORS configuration will explicitly reject unauthorized origins, cross-site request forgery is structurally impossible.

4. **Session Invalidation:**
   - `POST /api/auth/logout` deletes the database session record and clears the cookie via `Set-Cookie: collectflow_session=; Path=/api; Max-Age=0; HttpOnly; Secure`.

---

## 5. PostgreSQL Release Readiness & Database Strategy

### 5.1 Alembic Migration Assessment
- Single baseline migration exists: [`migrations/versions/e9ee322f8b96_create_collectflow_v1_domain_schema.py`](file:///home/anishb/projects/personal/collectflow/migrations/versions/e9ee322f8b96_create_collectflow_v1_domain_schema.py).
- Creates all 11 domain models, 14 indexes, and statutory check constraints (`chk_settlement_math`, `chk_po_status`, `chk_user_role`, `chk_gst_rate_bounds`).
- Schema consistency is verified against `app/models/domain_models.py`.
- **Finding:** Alembic directory is named `migrations/` rather than default `alembic/`. While `alembic.ini` is correctly configured (`script_location = %(here)s/migrations`), migration execution documentation must explicitly standardize this path.

### 5.2 Production Database Initialization Strategy
The current [`scripts/seed_postgres_fixtures.py`](file:///home/anishb/projects/personal/collectflow/scripts/seed_postgres_fixtures.py) was built for testing and contains hardcoded synthetic users (`sandbox_admin`, `sandbox_finance`, etc.) with known passwords (`AdminPass123!`), as well as test customers and test transactions.

**Production Initialization Rules:**
1. **Never run `seed_postgres_fixtures.py` in Production.** The script must be hardened to abort immediately if `COLLECTFLOW_ENV != "test"`.
2. **Dedicated Production Initializer:** Create `scripts/init_production_db.py` that:
   - Verifies the database is empty.
   - Executes `alembic upgrade head`.
   - Seeds ONLY statutory reference data:
     - Active Financial Year (`FY 2026-27`) and standard historical FY codes.
     - GST Rate Master: 0%, 5%, 12%, 18%, 28%.
     - TDS Section Master: 194C (2%), 194J (10%), 194J(b) (2%).
   - Creates exactly **one primary bootstrap administrator** with a cryptographically secure random password generated via `secrets.token_urlsafe(24)`, outputting the initial credentials strictly to secure stdout/Secrets Manager.
   - Flags the initial administrator with `must_change_password = True` to force immediate password rotation on first login.
   - Inserts **zero** demo customers, **zero** purchase orders, and **zero** financial transactions.

### 5.3 Backup & Disaster Recovery Architecture
- **Automated Physical Backups:** AWS RDS continuous archiving with 7-day Point-In-Time Recovery (PITR) window (1-minute recovery granularity).
- **Daily Logical Backups:** Automated `pg_dump` job running at 01:00 UTC, encrypting output via AES-256 and pushing to an isolated Amazon S3 bucket with Object Lock (WORM compliance) and a 90-day lifecycle retention policy.
- **Recovery Testing:** Quarterly automated restore dry-run into an ephemeral test instance to verify backup integrity and validate recovery time.

---

## 6. CI/CD & Infrastructure Automation Design

To satisfy the automation-first mandate with zero manual cloud portal actions, the release pipeline will be governed by GitHub Actions workflows and Terraform infrastructure-as-code:

```mermaid
flowchart LR
    subgraph Pull Request Gate
        PR[Developer PR] --> Lint[Lint & TypeCheck]
        Lint --> Pytest[Backend Pytest (115 tests)]
        Lint --> Vitest[Frontend Vitest (596 tests)]
        Pytest --> SchemaCheck[Alembic Dry-Run]
    end

    subgraph Staging & UAT Gate
        SchemaCheck --> MergeRelease[Merge to release/*]
        MergeRelease --> EphemeralDB[Spin up Ephemeral PG 16]
        EphemeralDB --> PlaywrightUAT[Playwright LIVE UAT (39 tests)]
        PlaywrightUAT --> Reconcile[5-Source Financial Reconcile]
    end

    subgraph Artifact & Deploy Gate
        Reconcile --> BuildDocker[Build Signed Docker Images]
        BuildDocker --> StagingDeploy[Deploy to Staging Environment]
        StagingDeploy --> SmokeStaging[Staging Smoke & Health Check]
        SmokeStaging --> ManualApproval{Lead & Finance Sign-Off}
        ManualApproval -->|Approved| ProdDeploy[Canary Production Rollout]
        ManualApproval -->|Rejected| Abort[Release Aborted]
    end
```

### Automation Specifications
1. **GitHub Branch Protection:**
   - `main` and `release/*` branches protected.
   - Mandatory linear history, zero force pushes.
   - Minimum 2 code reviews including security owner approval.
   - Status checks must pass: Lint, Typecheck, Pytest, Vitest, Playwright UAT, Reconciliation Verifier.
2. **Infrastructure as Code (IaC):**
   - Terraform templates defining VPC, Subnets, Security Groups, RDS PostgreSQL, ECS Fargate cluster, Cloudflare DNS, and WAF rules.
   - State stored in encrypted remote backend (S3 + DynamoDB state locking).
3. **Automated Rollback Criteria:**
   - Post-deployment health probe `/ready` fails 3 consecutive attempts.
   - Elevated error rate (HTTP 5xx > 0.5% over 5-minute rolling window).
   - In the event of rollback, ECS Fargate reverts to previous task definition revision in < 90 seconds.

---

## 7. Observability, Telemetry & Operations

### 7.1 Health & Readiness Probes
- **Liveness Probe (`GET /health`):** Validates FastAPI process execution; returns HTTP 200 `{"status": "healthy"}`.
- **Readiness Probe (`GET /ready`):** Validates PostgreSQL database connectivity and connection pool health (`SELECT 1`); returns HTTP 200 `{"status": "ready"}` or HTTP 503 `{"status": "not_ready"}` without leaking internal connection exception details.

### 7.2 Structured Logging & Correlation
- Standardized JSON logging across all backend routers:
  ```json
  {
    "timestamp": "2026-10-18T15:00:00.123Z",
    "level": "INFO",
    "req_id": "a1b2c3d4e5f6",
    "user_id": 4,
    "role": "finance",
    "method": "POST",
    "path": "/api/payments",
    "status": 200,
    "duration_ms": 42.5
  }
  ```
- `X-Request-ID` correlation header tracked across frontend, reverse proxy, FastAPI middleware, and PostgreSQL transaction query comments.
- Zero credentials, passwords, session tokens, or export ticket secrets written to logs.

### 7.3 Operational Metrics & Proposed Service Level Objectives (SLOs)
*The following targets are proposed baselines for operational alignment:*

| Metric | Target Objective | Alert Threshold | PagerDuty Severity |
|---|---|---|---|
| **API Availability** | $\ge 99.90\%$ uptime monthly | Availability $< 99.50\%$ over 5 min | P1 (Critical) |
| **Transaction Latency (OLTP)** | $p95 < 250\text{ ms}$ | $p95 > 500\text{ ms}$ over 10 min | P2 (High) |
| **Reporting Latency** | $p95 < 800\text{ ms}$ | $p95 > 2000\text{ ms}$ over 15 min | P3 (Medium) |
| **Financial Settlement Errors** | $0\text{ unhandled 5xx exceptions}$ | $\ge 1\text{ error}$ in `/api/payments` | P1 (Critical) |
| **Authentication Velocity** | Normal human login distribution | $> 15\text{ failed attempts}$ in 5 min | P2 (High) |
| **Database Pool Saturation** | $\le 70\%$ pool utilization | Active connections $> 85\%$ of pool | P2 (High) |

---

## 8. Release Lifecycle & Acceptance Gates

Transition from accepted local source code to a live production deployment follows a strict seven-stage gating process:

```
[Gate 1: Accepted Source] ──> [Gate 2: Release Candidate] ──> [Gate 3: Isolated Staging]
                                                                      │
[Gate 6: Production Launch] <── [Gate 5: Operational Drill] <── [Gate 4: Security Verification]
         │
[Gate 7: 48h Hypercare]
```

### Gate Definitions

#### Gate 1: Accepted Source Code Checkpoint
- **Prerequisites:** Successful completion of Stage 4C.6 UAT and zero-variance reconciliation.
- **Evidence:** Verified Git commit SHAs (`collectflow`: `1eb94b96321780d42527721585551d35f774fa3b`, `cash-flow-buddy-101`: `2085c29e51d28cd5a2466883415442dd861734b7`).
- **Approver:** Independent Acceptance Reviewer.

#### Gate 2: Release Candidate (RC) Packaging
- **Prerequisites:** Dependency manifests updated (`requirements.txt` corrected), container images built via GitHub Actions, SLSA provenance attestations attached.
- **Evidence:** Clean container vulnerability scan (0 critical/high CVEs via Trivy/pip-audit).
- **Approver:** DevOps Lead.

#### Gate 3: Isolated Staging Deployment
- **Prerequisites:** Staging infrastructure provisioned via Terraform, Alembic migrations applied to fresh staging PostgreSQL database.
- **Evidence:** Successful execution of 39 Playwright scenarios against staging endpoints; 5-source reconciliation report attached.
- **Approver:** QA Lead & Finance SME.

#### Gate 4: Security Verification & Audit Gate
- **Prerequisites:** Penetration testing and automated DAST scan against staging environment.
- **Evidence:** Verification of RBAC boundary enforcement, single-use export ticket consumption, brute-force rate-limiting, and zero session token leakage.
- **Approver:** Application Security Lead.

#### Gate 5: Operational & Disaster Recovery Verification
- **Prerequisites:** Simulated database failover (Multi-AZ switchover test) and automated backup restoration dry-run.
- **Evidence:** Confirmed RTO $\le 60$ minutes and RPO $\le 15$ minutes with verified data integrity.
- **Approver:** Site Reliability Engineer (SRE).

#### Gate 6: Controlled Production Release
- **Prerequisites:** Written sign-off on the Four Business Policy Decisions (Closed-period accounting, Export permissions, Customer deactivation, Forex scope); production database initialized with clean statutory master data (zero synthetic records).
- **Evidence:** Production `/health` and `/ready` probes green; initial admin password rotation verified; canary traffic ramped to 100%.
- **Approver:** Product Owner, Head of Engineering, and Head of Finance.

#### Gate 7: Hypercare & Post-Release Reconciliation
- **Prerequisites:** 48-hour continuous monitoring window following launch.
- **Evidence:** Zero unhandled financial exceptions in `/api/payments`; export operations verified; zero balance discrepancies.
- **Approver:** Head of Finance & Product Owner.

---

## 9. Next Steps & Release Work Packages

To transition from planning into release execution, the following technical work packages must be scheduled:

1. **WP-01: Dependency & Isolation Hardening (Backend):**
   - Correct `requirements.txt` to include `SQLAlchemy`, `psycopg[binary]`, `alembic`, and `argon2-cffi`.
   - Extend `app/core/database.py` with an authorized `COLLECTFLOW_ENV=production` configuration supporting managed remote PostgreSQL connections and strict SSL requirements.
2. **WP-02: Production Database Initializer:**
   - Author `scripts/init_production_db.py` to seed clean statutory reference data and bootstrap a single primary admin with forced rotation.
   - Add hard safety abort to `seed_postgres_fixtures.py` preventing execution outside `COLLECTFLOW_ENV=test`.
3. **WP-03: Same-Origin Cookie Authentication Implementation:**
   - Implement `HttpOnly; Secure; SameSite=Lax` cookie issuance and CSRF header verification in FastAPI backend.
   - Update frontend `ApiClient` to leverage cookie sessions, resolving page-refresh session loss.
4. **WP-04: CI/CD Pipeline Automation:**
   - Create `.github/workflows/ci.yml` and `.github/workflows/release.yml` with automated container builds and Playwright UAT execution.
5. **WP-05: Infrastructure as Code (Terraform):**
   - Create Terraform modules for AWS ECS Fargate, RDS PostgreSQL Multi-AZ, and Cloudflare Gateway routing.
