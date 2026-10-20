# CollectFlow Release Candidate Go/No-Go Checklist (RCP-01)

## 1. Release Candidate Identity & Baseline

- **Release Candidate ID:** `RC-20261020-01`
- **Backend Branch & SHA:** `feature/rcp01-security-evidence-remediation` @ `<BACKEND_FROZEN_SHA>`
- **Frontend Branch & SHA:** `feature/rcp01-security-evidence-remediation` @ `<FRONTEND_FROZEN_SHA>`
- **Accepted WP-06 Ancestors:**
  - Backend: `12582f7abb3c391e2d80d61ada179629d7825eac` (WP-06 Accepted)
  - Frontend: `c6fc51a75135b0343d9c7ad90a80ba5fb5b61260` (WP-06 Accepted)
- **Preflight Master Manifest:** `artifacts/rcp01/rcp01_release_evidence_manifest.json`

---

## 2. Multi-Perspective Go/No-Go Decision Matrix

| Perspective | Preflight Recommendation | Evaluated Basis & Supporting Evidence | Known Caveats & Outstanding Blockers |
| :--- | :--- | :--- | :--- |
| **[1] Technical Application Release Candidate** | **GO** | 387 backend tests evaluated (316 passed, 71 skipped) against PostgreSQL 16; 620 frontend Vitest passed; 0 TypeScript errors; 5-source financial reconciliation verified at ₹0.00 delta across 10 metrics; Alembic schema 77e73a1cb221 verified; 0 secrets; clean OCI containers with 0 CRITICAL / 0 HIGH CVEs. | GitHub-hosted release candidate workflow remains BLOCKED due to default branch indexing (HTTP 404). Merging to `main` prohibited until final production release. |
| **[2] Corporate Business Release** | **NO_GO** | Statutory configuration reviews BPD-01 and BPD-02 remain in status `AWAITING_FINANCE_APPROVAL`. Business Acceptance Checklist is `BLOCKED`. | Strictly prohibited from production deployment and statutory rate activation until corporate finance sign-off. |
| **[3] First Isolated AWS TEST Deployment** | **NO_GO** | Preflight evaluated deploy/ ground truth: only 2/12 criteria verified (`empty_db_init`, `image_provenance`), 4 partial, and 6 not implemented. Zero Terraform modules exist in `deploy/`. Dedicated test account boundary unprovisioned. | Must author Terraform IaC in `deploy/terraform/` and provision dedicated isolated AWS TEST account before deployment can be considered. |

---

## 3. First Isolated AWS TEST Deployment Checklist (12 Criteria)

- [ ] **1. Dedicated Account Boundary:** `NOT_IMPLEMENTED` (Dedicated AWS Organization test account ID unprovisioned).
- [ ] **2. IaC Completeness:** `PARTIAL` (`deploy/compose/` has compose; 0 Terraform `.tf` modules exist in `deploy/`).
- [x] **3. Empty DB Initialization:** `VERIFIED` (Alembic `77e73a1cb221` safe empty schema bootstrap verified).
- [x] **4. Image Provenance:** `VERIFIED` (Images tagged with exact commit SHAs, digests recorded, zero-publish policy).
- [ ] **5. Secrets Management:** `NOT_IMPLEMENTED` (AWS Secrets Manager / KMS keys unprovisioned).
- [ ] **6. Network Isolation & TLS:** `NOT_IMPLEMENTED` (VPC, private subnets, security groups, ALB TLS unprovisioned).
- [ ] **7. IAM Least Privilege:** `NOT_IMPLEMENTED` (ECS task execution roles & policies unprovisioned).
- [ ] **8. Structured Logging & Retention:** `PARTIAL` (JSON structured logs emitted locally; CloudWatch log group unprovisioned).
- [ ] **9. Backup & Rollback:** `PARTIAL` (Local PostgreSQL dump scripts present; RDS automated snapshot unprovisioned).
- [ ] **10. Cost Limits:** `NOT_IMPLEMENTED` (AWS Budgets alert / CloudWatch billing alarms unprovisioned).
- [ ] **11. Synthetic Test Data Only:** `PARTIAL` (Synthetic seed fixtures in `seed_postgres_fixtures.py` verified; AWS RDS seeding unexecuted).
- [ ] **12. Automated Teardown:** `PARTIAL` (Local `compose down -v` verified; Terraform destroy unprovisioned).

### Outstanding Preconditions for AWS TEST Execution:
1. **[ ] Terraform Module Development:** Author modular Terraform specifications under `deploy/terraform/` for VPC, ECS Fargate, RDS PostgreSQL, and ALB.
2. **[ ] Physical Account Provisioning:** Provision dedicated isolated AWS TEST account with distinct AWS Organization boundaries.
3. **[ ] KMS & Secret Provisioning:** Provision AWS Secrets Manager KMS keys for session secrets and database credentials.
4. **[ ] Operational Sign-off:** Authorized human decision-maker executes operational approval.

---

## 4. Human Decision-Maker Sign-Off Table

| Approval Gate | Role / Authority | Required Status | Decision (GO / NO-GO) | Sign-off Signature & Date |
| :--- | :--- | :--- | :--- | :--- |
| **Technical Architecture Gate** | Lead Systems Architect | Technical Verification GO | [ ] APPROVED [ ] REJECTED | _________________________ |
| **Corporate Finance Gate** | Chief Financial Officer / Finance Lead | Statutory BPD-01 & BPD-02 | [ ] APPROVED [ ] BLOCKED | _________________________ |
| **Cloud Operations Gate** | AWS Cloud Operations Lead | Isolated Account & KMS Preconditions | [ ] APPROVED [ ] HOLD | _________________________ |
| **Final Release Authority** | Release Acceptance Lead | Preflight Manifest Integrity | [ ] APPROVED [ ] REJECTED | _________________________ |
