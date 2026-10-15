from fastapi.testclient import TestClient

from ic.api.app import create_app


def test_health_reports_falkordb_reachable():
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "falkordb": True}
