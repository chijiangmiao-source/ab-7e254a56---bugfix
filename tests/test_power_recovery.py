"""Power-loss recovery: candidate write, digest check, confirm switch."""
from __future__ import annotations

from tests.conftest import make_device


def test_power_cut_during_candidate_write_keeps_old_slot(client):
    make_device(client)

    r = client.post("/api/devices/dev-1/candidate", json={
        "version": "2.0.0", "request_id": "r1", "fault_point": "candidate_write",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "power_cut"
    assert body["fault_point"] == "candidate_write"
    assert body["device"]["powered_on"] is False

    # Power on -> recovery must still boot the old confirmed slot A.
    r = client.post("/api/devices/dev-1/power-on")
    assert r.status_code == 200
    rec = r.json()["recovery"]
    assert rec["active_slot"] == "A"
    assert rec["eligible"] == ["A"]
    assert rec["generation"] == 1
    diag_b = next(d for d in rec["diagnoses"] if d["slot"] == "B")
    assert diag_b["reason"] == "incomplete_write"
    assert "禁止引导" in diag_b["detail"]

    dev = r.json()["device"]
    assert dev["active_slot"] == "A"
    assert dev["slots"]["A"]["version"] == "1.0.0"
    assert dev["slots"]["B"]["status"] == "CANDIDATE"
    assert dev["slots"]["B"]["written"] < dev["slots"]["B"]["size"]

    # Evidence was persisted across the power cut.
    ev = client.get("/api/devices/dev-1/evidence").json()["evidence"]
    assert any(e["reason"] == "power_cut" and e["slot"] == "B" for e in ev)


def test_power_cut_during_digest_check_never_promotes_candidate(client):
    make_device(client)
    r = client.post("/api/devices/dev-1/candidate", json={
        "version": "2.0.0", "request_id": "r1", "fault_point": "digest_check",
    })
    assert r.json()["outcome"] == "power_cut"

    r = client.post("/api/devices/dev-1/power-on")
    rec = r.json()["recovery"]
    assert rec["active_slot"] == "A"
    diag_b = next(d for d in rec["diagnoses"] if d["slot"] == "B")
    # Fully written but verdict never committed => unproven, not bootable.
    assert diag_b["reason"] == "unverified_candidate"

    dev = r.json()["device"]
    assert dev["slots"]["B"]["written"] == dev["slots"]["B"]["size"]
    assert dev["slots"]["B"]["status"] == "CANDIDATE"
    assert dev["slots"]["B"]["actual_digest"] is None


def test_power_cut_during_confirm_switch_does_not_commit(client):
    make_device(client)
    r = client.post("/api/devices/dev-1/candidate", json={
        "version": "2.0.0", "request_id": "r1",
    })
    assert r.json()["outcome"] == "staged"

    r = client.post("/api/devices/dev-1/confirm", json={"fault_point": "confirm_switch"})
    assert r.json()["outcome"] == "power_cut"

    r = client.post("/api/devices/dev-1/power-on")
    rec = r.json()["recovery"]
    # Old version keeps booting; candidate stays verified-but-unconfirmed.
    assert rec["active_slot"] == "A"
    assert rec["generation"] == 1
    diag_b = next(d for d in rec["diagnoses"] if d["slot"] == "B")
    assert diag_b["reason"] == "unconfirmed_candidate"
    dev = r.json()["device"]
    assert dev["slots"]["A"]["version"] == "1.0.0"
    assert dev["slots"]["B"]["status"] == "VERIFIED"

    # The staged candidate can still be confirmed after recovery.
    r = client.post("/api/devices/dev-1/confirm", json={})
    assert r.status_code == 200
    assert r.json()["outcome"] == "switched"
    assert r.json()["generation"] == 2


def test_digest_mismatch_rejects_and_never_boots(client):
    make_device(client)
    # Claim the *correct* digest but send corrupt bytes.
    import hashlib
    tag = b"payload-image::2.0.0::"
    good = (tag + bytes((i * 7 + 5) & 0xFF for i in range(256 - len(tag))))[:256]
    good_digest = hashlib.sha256(good).hexdigest()
    bad = bytes([good[0] ^ 0xFF]) + good[1:]
    import base64
    r = client.post("/api/devices/dev-1/candidate", json={
        "version": "2.0.0",
        "request_id": "r-bad",
        "digest": good_digest,
        "content_b64": base64.b64encode(bad).decode(),
    })
    body = r.json()
    assert body["outcome"] == "verification_failed"
    assert body["claimed_digest"] != body["actual_digest"]

    # Even after an explicit power cycle the corrupt slot is rejected.
    client.post("/api/devices/dev-1/power-off")
    r = client.post("/api/devices/dev-1/power-on")
    rec = r.json()["recovery"]
    assert rec["active_slot"] == "A"
    diag_b = next(d for d in rec["diagnoses"] if d["slot"] == "B")
    assert diag_b["reason"] == "digest_mismatch"
    dev = r.json()["device"]
    assert dev["slots"]["B"]["status"] == "REJECTED"
    assert dev["slots"]["B"]["actual_digest"] == body["actual_digest"]


def test_full_upgrade_persists_and_survives_reopen(client):
    make_device(client)
    client.post("/api/devices/dev-1/candidate", json={"version": "2.0.0", "request_id": "r1"})
    sw = client.post("/api/devices/dev-1/confirm", json={}).json()
    assert sw["active_slot"] == "B"
    assert sw["generation"] == 2
    expected_digest = sw["device"]["slots"]["B"]["digest"]

    client.post("/api/devices/dev-1/power-off")
    r = client.post("/api/devices/dev-1/power-on")
    rec = r.json()["recovery"]
    assert rec["active_slot"] == "B"
    assert rec["generation"] == 2
    dev = r.json()["device"]
    assert dev["slots"]["B"]["version"] == "2.0.0"
    assert dev["slots"]["B"]["digest"] == expected_digest
    assert dev["slots"]["B"]["status"] == "CONFIRMED"
    assert dev["slots"]["A"]["status"] == "SUPERSEDED"

    # Rationale must explain the no-rollback decision.
    assert any("防回退" in x for x in rec["rationale"])
