import os
import pytest
from fastapi.testclient import TestClient


def test_cors_origin_allowlist(client):
    # Test allowed origin
    res = client.options(
        "/api/meta",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization,Content-Type",
        },
    )
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "http://localhost:5173"

    # Test rejected origin (e.g. untrusted attacker origin or public cloudflare worker)
    res_rejected = client.options(
        "/api/meta",
        headers={
            "Origin": "https://malicious-site.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in res_rejected.headers

    # Verify cloudflare staging worker is NOT in the live allowlist by default
    res_cf = client.options(
        "/api/meta",
        headers={
            "Origin": "https://anishb-personal-cash-flow-buddy-101.aanishb.workers.dev",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in res_cf.headers


def test_sandbox_database_path_isolation():
    from app.database import get_db_path

    # In integration mode without COLLECTFLOW_DB_PATH, startup must abort
    os.environ["COLLECTFLOW_ENV"] = "integration"
    old_path = os.environ.pop("COLLECTFLOW_DB_PATH", None)
    try:
        with pytest.raises(RuntimeError, match="CRITICAL ISOLATION ERROR"):
            get_db_path()

        # If pointing to default prod database filename without integration/sandbox/test, must abort
        os.environ["COLLECTFLOW_DB_PATH"] = "/var/data/collectflow.db"
        with pytest.raises(RuntimeError, match="CRITICAL ISOLATION ERROR"):
            get_db_path()

        # If pointing into production path using relative traversal ../
        os.environ["COLLECTFLOW_DB_PATH"] = "/var/data/sandbox/../collectflow.db"
        with pytest.raises(RuntimeError, match="CRITICAL ISOLATION ERROR"):
            get_db_path()

        # Valid sandbox path passes
        os.environ["COLLECTFLOW_DB_PATH"] = "/var/data/collectflow_sandbox.db"
        resolved = get_db_path()
        assert "sandbox" in resolved

        # Test symlink to production path is strictly rejected
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_prod = os.path.join(tmpdir, "collectflow.db")
            with open(fake_prod, "w") as f:
                f.write("")
            symlink_path = os.path.join(tmpdir, "sandbox_symlink.db")
            os.symlink(fake_prod, symlink_path)

            os.environ["COLLECTFLOW_DB_PATH"] = symlink_path
            with pytest.raises(RuntimeError, match="CRITICAL ISOLATION ERROR"):
                get_db_path()
    finally:
        os.environ.pop("COLLECTFLOW_ENV", None)
        if old_path:
            os.environ["COLLECTFLOW_DB_PATH"] = old_path
