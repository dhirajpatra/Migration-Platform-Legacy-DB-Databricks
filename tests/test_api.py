import time
from fastapi.testclient import TestClient
from fake_connector import SeedConnector
from migcore.orchestrator.api import create_app


def test_api_run_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    cfg = {"graph": {"enabled": False}, "sources": [{"id": "mssql_demo", "engine": "sqlserver"}]}
    c = TestClient(create_app(cfg, lambda s: SeedConnector(s["id"], s)))
    rid = c.post("/runs").json()["run_id"]
    for _ in range(100):
        body = c.get(f"/runs/{rid}").json() if c.get(f"/runs/{rid}").status_code == 200 else {}
        if body.get("status") == "awaiting_g1":
            break
        time.sleep(0.1)
    assert body["status"] == "awaiting_g1" and body["error"] is None
    assert "Assessment Report" in c.get(f"/runs/{rid}/assessment").text
    assert c.post(f"/runs/{rid}/approve", json={"approved": True, "approver": "me"}).status_code == 200
    for _ in range(100):
        body = c.get(f"/runs/{rid}").json()
        if body["status"] == "g1_approved":
            break
        time.sleep(0.1)
    assert body["status"] == "g1_approved"
    assert c.post(f"/runs/{rid}/approve", json={"approved": True, "approver": "me"}).status_code == 409
