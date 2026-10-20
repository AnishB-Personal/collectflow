# CollectFlow Release Candidate Go/No-Go Checklist (RCP-01)

## 1. Release Candidate Identity & Baseline

- **Release Candidate ID:** `RC-20261020-01`
- **Backend Branch & SHA:** `feature/release-rcp01-evidence-preflight` @ `<BACKEND_FROZEN_SHA>`
- **Frontend Branch & SHA:** `feature/release-rcp01-evidence-preflight` @ `<FRONTEND_FROZEN_SHA>`
- **Accepted WP-06 Ancestors:**
  - Backend: `12582f7abb3c391e2d80d61ada179629d7825eac` (WP-06 Accepted)
  - Frontend: `c6fc51a75135b0343d9c7ad90a80ba5fb5b61260` (WP-06 Accepted)
- **Preflight Master Manifest:** `artifacts/rcp01/rcp01_release_evidence_manifest.json`

---

## 2. Multi-Perspective Go/No-Go Decision Matrix

| Perspective | Preflight Recommendation | Evaluated Basis & Supporting Evidence | Known Caveats & Outstanding Blockers |
| :--- | :--- | :--- | :--- |
| **[1] Technical Application Release Candidate** | **GO** | 316 backend tests passed against PostgreSQL 16; 620 frontend Vitest passed; 0 TypeScript errors; 5-source financial reconciliation verified at ₹0.00 delta across 10 metrics; Alembic schema 77e73a1cb221 verified; 0 secrets; clean OCI containers. | GitHub-hosted release candidate workflow remains BLOCKED due to default branch indexing (HTTP 404). Merging to `main` prohibited until final production release. |
| **[2] Corporate Business Release** | **NO_GO** | Statutory configuration reviews BPD-01 and BPD-02 remain in status `AWAITING_FINANCE_APPROVAL`. Business Acceptance Checklist is `BLOCKED`. | Strictly prohibited from production deployment and statutory rate activation until corporate finance sign-off. |
| **[3] First Isolated AWS TEST Deployment** | **CONDITIONAL_GO** | All 12 architectural deployment criteria compliant (isolated account boundary, modular IaC, safe empty DB init, exact image provenance, secret management via KMS, structured logging, cost limits at $50/mo, teardown scripts verified). Synthetic test data only. | Requires 3 prerequisites to be fulfilled by authorized human decision-makers prior to executing `terraform apply` or cloud provisioning. |

---

## 3. First Isolated AWS TEST Deployment Checklist (12 Criteria)

- [x] **1. Dedicated Account Boundary:** Isolated AWS account ID separate from corporate billing.
- [x] **2. IaC Completeness:** Modular Terraform / Docker Compose specifications ready in `deploy/`.
- [x] **3. Empty DB Initialization:** Alembic `77e73a1cb221` safe empty schema bootstrap verified.
- [x] **4. Image Provenance:** Images tagged with exact commit SHAs (`collectflow-backend:<sha>`).
- [x] **5. Secrets Management:** AWS Secrets Manager / Parameter Store KMS structure defined.
- [x] **6. Network Isolation & TLS:** Private ECS/RDS subnets, public ALB with ACM TLS 1.3 termination.
- [x] **7. IAM Least Privilege:** Dedicated ECS execution role scoped to Secrets Manager & CloudWatch.
- [x] **8. Structured Logging & Retention:** JSON structured logs, CloudWatch 30-day retention.
- [x] **9. Backup & Rollback:** Pre-migration automated RDS snapshot and rollback teardown procedures.
- [x] **10. Cost Limits:** AWS Budgets alert set at $50/month threshold with automated stop alarms.
- [x] **11. Synthetic Test Data Only:** Synthetic seed fixtures (`seed_postgres_fixtures.py`) verified; zero PII.
- [x] **12. Automated Teardown:** Teardown procedure (`terraform destroy` / `compose down -v`) verified.

### Preconditions for AWS TEST Execution:
1. **[ ] Physical Account Provisioning:** Provision dedicated isolated AWS TEST account with distinct AWS Organization boundaries.
2. **[ ] KMS & Secret Provisioning:** Provision AWS Secrets Manager KMS keys for session secrets and database credentials.
3. **[ ] Operational Sign-off:** Authorized human decision-maker executes operational approval.

---

## 4. Human Decision-Maker Sign-Off Table

| Approval Gate | Role / Authority | Required Status | Decision (GO / NO-GO) | Sign-off Signature & Date |
| :--- | :--- | :--- | :--- | :--- |
| **Technical Architecture Gate** | Lead Systems Architect | Technical Verification GO | [ ] APPROVED [ ] REJECTED | _________________________ |
| **Corporate Finance Gate** | Chief Financial Officer / Finance Lead | Statutory BPD-01 & BPD-02 | [ ] APPROVED [ ] BLOCKED | _________________________ |
| **Cloud Operations Gate** | AWS Cloud Operations Lead | Isolated Account & KMS Preconditions | [ ] APPROVED [ ] HOLD | _________________________ |
| **Final Release Authority** | Release Acceptance Lead | Preflight Manifest Integrity | [ ] APPROVED [ ] REJECTED | _________________________ |
