"""Misc API behavior: health, validation, evidence, persistence layout."""
from __future__ import annotations

from tests.conftest import make_device


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_and_list_devices(client):
    make_device(client, "d-a", "1.0.0")
    make_device(client, "d-b", "1.4.2")
    ids = [d["device_id"] for d in client.get("/api/devices").json()["devices"]]
    assert ids == ["d-a", "d-b"]


def test_duplicate_device_conflict(client):
    make_device(client)
    r = client.post("/api/devices", json={"device_id": "dev-1", "version": "9.0.0"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "device_exists"
    # Original version is untouched.
    dev = client.get("/api/devices/dev-1").json()["device"]
    assert dev["slots"]["A"]["version"] == "1.0.0"


def test_operations_while_powered_off_are_refused(client):
    make_device(client)
    client.post("/api/devices/dev-1/power-off")
    r = client.post("/api/devices/dev-1/candidate", json={"version": "2.0.0"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "powered_off"


def test_evidence_endpoint_records_lifecycle(client):
    make_device(client)
    client.post("/api/devices/dev-1/candidate", json={"version": "2.0.0", "request_id": "r1"})
    client.post("/api/devices/dev-1/confirm", json={})
    ev = client.get("/api/devices/dev-1/evidence").json()["evidence"]
    reasons = [e["reason"] for e in ev]
    assert "candidate_staged" in reasons
    assert "digest_verified" in reasons
    assert "switch_committed" in reasons
