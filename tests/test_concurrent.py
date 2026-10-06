"""Concurrent candidate adjudication and version qualification rules."""
from __future__ import annotations

import threading

from tests.conftest import make_device


def test_two_concurrent_requests_single_winner(client):
    make_device(client)
    results = []
    errors = []

    def submit(version, request_id):
        try:
            r = client.post("/api/devices/dev-1/candidate", json={
                "version": version, "request_id": request_id,
            })
            results.append((request_id, r.status_code, r.json()))
        except Exception as exc:  # pragma: no cover - surface thread errors
            errors.append(exc)

    t1 = threading.Thread(target=submit, args=("2.0.0", "page-alpha"))
    t2 = threading.Thread(target=submit, args=("2.1.0", "page-beta"))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors

    statuses = sorted(code for _, code, _ in results)
    assert statuses == [200, 409], results
    winner = next((item for item in results if item[1] == 200), None)
    loser = next((item for item in results if item[1] == 409), None)
    assert winner and loser
    assert loser[2]["error"]["code"] == "upgrade_conflict"
    assert loser[2]["error"]["holder_request"] == winner[0]

    # The active version/slot must not have been touched by the loser.
    dev = client.get("/api/devices/dev-1").json()["device"]
    assert dev["active_slot"] == "A"
    assert dev["slots"]["A"]["version"] == "1.0.0"
    assert dev["qualified_request"] == winner[0]
    assert dev["generation"] == 1


def test_conflict_is_stable_across_repeated_attempts(client):
    make_device(client)
    first = client.post("/api/devices/dev-1/candidate", json={
        "version": "2.0.0", "request_id": "alpha",
    })
    assert first.status_code == 200
    for _ in range(3):
        again = client.post("/api/devices/dev-1/candidate", json={
            "version": "2.0.0", "request_id": "alpha",
        })
        # Same request id is a stable retry of the winner, not a new conflict.
        assert again.status_code == 200
        other = client.post("/api/devices/dev-1/candidate", json={
            "version": "2.0.0", "request_id": "beta",
        })
        assert other.status_code == 409
        assert other.json()["error"]["code"] == "upgrade_conflict"


def test_lower_version_is_rejected(client):
    make_device(client, version="3.0.0")
    r = client.post("/api/devices/dev-1/candidate", json={
        "version": "2.9.9", "request_id": "r1",
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "version_not_higher"
    dev = client.get("/api/devices/dev-1").json()["device"]
    assert dev["slots"]["B"]["status"] == "EMPTY"
    assert dev["qualified_request"] is None


def test_second_upgrade_after_switch_bumps_generation(client):
    make_device(client)
    client.post("/api/devices/dev-1/candidate", json={"version": "2.0.0", "request_id": "r1"})
    sw = client.post("/api/devices/dev-1/confirm", json={}).json()
    assert sw["generation"] == 2

    # A fresh generation: qualification token resets, a new candidate may win.
    r = client.post("/api/devices/dev-1/candidate", json={
        "version": "3.0.0", "request_id": "r2",
    })
    assert r.status_code == 200
    assert r.json()["target_slot"] == "A"
    sw2 = client.post("/api/devices/dev-1/confirm", json={}).json()
    assert sw2["active_slot"] == "A"
    assert sw2["generation"] == 3

    # Reopen: newest version active; both older slots SUPERSEDED, no rollback.
    client.post("/api/devices/dev-1/power-off")
    rec = client.post("/api/devices/dev-1/power-on").json()["recovery"]
    assert rec["active_slot"] == "A"
    assert rec["generation"] == 3
    dev = client.get("/api/devices/dev-1").json()["device"]
    assert dev["slots"]["A"]["version"] == "3.0.0"
    assert dev["slots"]["B"]["status"] == "SUPERSEDED"
