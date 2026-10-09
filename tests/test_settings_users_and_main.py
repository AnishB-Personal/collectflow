def test_settings_endpoints_crud(client, admin_token, sales_token):
    # Taxes
    taxes = client.get("/api/settings/taxes", headers=admin_token).json()
    assert len(taxes["gst_rates"]) >= 1

    # GST create
    create_tax = client.post(
        "/api/settings/taxes/gst", json={"name": "GST 5%", "rate": 0.05, "tax_type": "GST"}, headers=admin_token
    )
    assert create_tax.status_code == 200
    new_tax_id = create_tax.json()["id"]

    # Sales cannot delete tax rate
    assert client.delete(f"/api/settings/taxes/gst/{new_tax_id}", headers=sales_token).status_code == 403
    # Admin can delete
    assert client.delete(f"/api/settings/taxes/gst/{new_tax_id}", headers=admin_token).status_code == 200

    # TDS create
    create_tds = client.post(
        "/api/settings/taxes/tds",
        json={"section_code": "194H", "description": "Commission", "rate": 0.05},
        headers=admin_token,
    )
    assert create_tds.status_code == 200
    new_tds_id = create_tds.json()["id"]
    assert client.delete(f"/api/settings/taxes/tds/{new_tds_id}", headers=admin_token).status_code == 200

    # FY
    fys = client.get("/api/settings/financial-years", headers=admin_token).json()
    assert isinstance(fys, list)


def test_users_targets_and_password_reset(client, admin_token, finance_token):
    # Create user
    res = client.post(
        "/api/users",
        json={"username": "target_user_test", "password": "password123", "role": "sales"},
        headers=admin_token,
    )
    user_id = res.json()["user_id"]

    # Save targets
    target_payload = {
        "fy_code": "FY 2026-27",
        "yearly_target": 1200000.0,
        "half_yearly_target": 600000.0,
        "quarterly_target": 300000.0,
        "monthly_target": 100000.0,
    }
    save_tgt = client.post(f"/api/users/{user_id}/targets", json=target_payload, headers=finance_token)
    assert save_tgt.status_code == 200

    # Get targets
    tgt_res = client.get(f"/api/users/{user_id}/targets?fy=FY 2026-27", headers=finance_token)
    assert tgt_res.status_code == 200
    assert tgt_res.json()["targets"]["yearly_target"] == 1200000.0

    # Reset password
    reset_res = client.post(
        f"/api/users/{user_id}/reset-password", json={"new_password": "new_secure_pwd_123"}, headers=admin_token
    )
    assert reset_res.status_code == 200

    # Login with new password
    login_new = client.post("/api/auth/login", json={"username": "target_user_test", "password": "new_secure_pwd_123"})
    assert login_new.status_code == 200


def test_main_health_readiness_and_meta(client, admin_token):
    # Health check
    h = client.get("/health")
    assert h.status_code == 200
    assert h.json()["status"] == "healthy"

    # Readiness check
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"

    # Index HTML
    idx = client.get("/")
    assert idx.status_code == 200
    assert "CollectFlow" in idx.text

    # Meta
    meta = client.get("/api/meta", headers=admin_token)
    assert meta.status_code == 200
    assert "financial_years" in meta.json()
