"""Stale-verdict / confirmed-slot poisoning regression tests.

Scenario (the defect this guards against):

1. A device completes a *normal* upgrade (content == claimed digest) and the
   operator confirms the switch.
2. The maintainer submits a *higher-version* candidate into the other slot
   whose image bytes are clearly different but whose claimed digest reuses the
   previous image's digest.

The new candidate must be measured on its own bytes and rejected -- the prior
candidate's verification conclusion must never substitute for this one. No
generation may advance, no active slot may switch, and the mismatching bytes
must never become a bootable image.

A device *already* poisoned in the field (a CONFIRMED slot whose persisted
bytes no longer match its manifest digest) must converge safely on reopen:
the slot is quarantined, evidence is retained, the device refuses to boot
rather than rolling back to a SUPERSEDED version, and the verdict repeats on
every subsequent re-open.
"""
from __future__ import annotations

import base64
import hashlib
import sqlite3

from tests.conftest import make_device

OTHER_BYTES = b"totally-different-image-bytes-for-v3!!" + b"\x00" * 220


def _synthetic_payload(version: str) -> bytes:
    tag = f"payload-image::{version}::".encode()
    return (tag + bytes((i * 7 + len(version)) & 0xFF for i in range(256 - len(tag))))[:256]


def _do_upgrade(client, dev, version, request_id, content=None, digest=None):
    body = {"version": version, "request_id": request_id}
    if content is not None:
        body["content_b64"] = base64.b64encode(content).decode()
    if digest is not None:
        body["digest"] = digest
    staged = client.post(f"/api/devices/{dev}/candidate", json=body)
    assert staged.status_code == 200, staged.text
    assert staged.json()["outcome"] == "staged"
    confirmed = client.post(f"/api/devices/{dev}/confirm", json={})
    assert confirmed.status_code == 200, confirmed.text
    return staged.json(), confirmed.json()


def _blob_digest(db_path: str, dev: str, slot: str) -> str:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT content FROM blobs WHERE device_id=? AND slot=?", (dev, slot)
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    return hashlib.sha256(row[0]).hexdigest()


def _blob_bytes(db_path: str, dev: str, slot: str) -> bytes:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT content FROM blobs WHERE device_id=? AND slot=?", (dev, slot)
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    return row[0]


def test_higher_candidate_reusing_prior_digest_is_measured_and_rejected(client):
    make_device(client)

    # --- first, fully normal upgrade v1 -> v2, confirmed and switched -------
    v2 = _synthetic_payload("2.0.0")
    v2_digest = hashlib.sha256(v2).hexdigest()
    staged, switched = _do_upgrade(client, "dev-1", "2.0.0", "r1")
    assert staged["claimed_digest"] == v2_digest
    assert switched["active_slot"] == "B"
    assert switched["generation"] == 2

    # --- second candidate: higher version, different bytes, STALE digest ----
    assert hashlib.sha256(OTHER_BYTES).hexdigest() != v2_digest
    r = client.post("/api/devices/dev-1/candidate", json={
        "version": "3.0.0",
        "request_id": "r2",
        "digest": v2_digest,
        "content_b64": base64.b64encode(OTHER_BYTES).decode(),
    })
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "verification_failed"
    # The verdict is computed from THIS write's bytes, not the old verdict.
    assert body["claimed_digest"] == v2_digest
    assert body["actual_digest"] == hashlib.sha256(OTHER_BYTES).hexdigest()

    dev = body["device"]
    slot_a = dev["slots"]["A"]
    assert slot_a["status"] == "REJECTED"
    assert slot_a["actual_digest"] == hashlib.sha256(OTHER_BYTES).hexdigest()
    # Persisted bytes really are the new image; displayed digest does not match.
    import os
    real = _blob_digest(os.environ["DATA_PATH"], "dev-1", "A")
    assert real == hashlib.sha256(OTHER_BYTES).hexdigest()
    assert real != slot_a["digest"]

    # --- confirmation must be refused: no generation bump / slot switch -----
    r = client.post("/api/devices/dev-1/confirm", json={})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_verified_candidate"

    dev = client.get("/api/devices/dev-1").json()["device"]
    assert dev["active_slot"] == "B"
    assert dev["generation"] == 2
    assert dev["slots"]["B"]["status"] == "CONFIRMED"
    assert dev["slots"]["B"]["version"] == "2.0.0"
    assert dev["qualified_request"] is None

    # Evidence explains the rejection.
    ev = client.get("/api/devices/dev-1/evidence").json()["evidence"]
    mismatch = [e for e in ev if e["slot"] == "A" and e["reason"] == "digest_mismatch"]
    assert mismatch and v2_digest[:16] in mismatch[-1]["detail"]


def test_rejected_stale_candidate_survives_power_cycle_without_booting(client):
    make_device(client)
    v2_digest = hashlib.sha256(_synthetic_payload("2.0.0")).hexdigest()
    _do_upgrade(client, "dev-1", "2.0.0", "r1")

    r = client.post("/api/devices/dev-1/candidate", json={
        "version": "3.0.0", "request_id": "r2",
        "digest": v2_digest,
        "content_b64": base64.b64encode(OTHER_BYTES).decode(),
    })
    assert r.json()["outcome"] == "verification_failed"

    client.post("/api/devices/dev-1/power-off")
    r = client.post("/api/devices/dev-1/power-on")
    rec, dev = r.json()["recovery"], r.json()["device"]
    # Old confirmed version keeps booting; rejected slot is diagnosed, not booted.
    assert rec["active_slot"] == "B"
    assert rec["generation"] == 2
    diag_a = next(d for d in rec["diagnoses"] if d["slot"] == "A")
    assert diag_a["reason"] == "digest_mismatch"
    assert dev["slots"]["A"]["status"] == "REJECTED"
    assert dev["slots"]["B"]["version"] == "2.0.0"


def test_persisted_poisoned_confirmed_slot_is_quarantined_on_reopen(client):
    """Device already affected in the field: CONFIRMED bytes != manifest.

    Simulated via the fault-injection hook (manifest untouched, bytes flipped),
    then adjudicated purely through the public power-on API.
    """
    make_device(client)
    v2_digest = hashlib.sha256(_synthetic_payload("2.0.0")).hexdigest()
    _, sw = _do_upgrade(client, "dev-1", "2.0.0", "r1")
    assert sw["active_slot"] == "B"

    # Tamper the confirmed active slot's persisted bytes (device powers off).
    r = client.post("/api/devices/dev-1/fault/tamper-confirmed", json={"slot": "B"})
    assert r.status_code == 200
    assert r.json()["outcome"] == "tampered"
    assert r.json()["device"]["powered_on"] is False

    r = client.post("/api/devices/dev-1/power-on")
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "unbootable"
    rec, dev = body["recovery"], body["device"]
    assert rec["active_slot"] is None
    assert rec["eligible"] == []
    assert rec["critical"] and "防回退" in rec["critical"]

    diag_b = next(d for d in rec["diagnoses"] if d["slot"] == "B")
    assert diag_b["reason"] == "confirmed_digest_mismatch"
    assert v2_digest[:16] in diag_b["detail"]
    diag_a = next(d for d in rec["diagnoses"] if d["slot"] == "A")
    assert diag_a["reason"] == "superseded_no_rollback"

    # The poisoned slot is quarantined; generation is untouched; device stays off.
    assert dev["slots"]["B"]["status"] == "REJECTED"
    assert dev["slots"]["B"]["quarantined"] is True
    assert dev["slots"]["B"]["quarantine_generation"] == 2
    assert dev["slots"]["B"]["version"] == "2.0.0"
    assert dev["slots"]["B"]["digest"] == v2_digest
    assert dev["slots"]["B"]["actual_digest"] != v2_digest
    assert dev["slots"]["A"]["status"] == "SUPERSEDED"
    assert dev["generation"] == 2
    assert dev["powered_on"] is False

    # Evidence retained for review.
    ev = client.get("/api/devices/dev-1/evidence").json()["evidence"]
    assert any(e["reason"] == "confirmed_digest_mismatch" for e in ev)

    # While the device remains shut down (safe maintenance state) operations
    # are refused as powered_off -- a quarantined slot can never be confirmed.
    r = client.post("/api/devices/dev-1/confirm", json={})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "powered_off"

    # Every subsequent re-open converges to the same safe verdict.
    r = client.post("/api/devices/dev-1/power-on")
    rec2, dev2 = r.json()["recovery"], r.json()["device"]
    assert r.json()["outcome"] == "unbootable"
    assert rec2["active_slot"] is None
    assert dev2["slots"]["B"]["status"] == "REJECTED"
    assert dev2["slots"]["A"]["status"] == "SUPERSEDED"

    # Even if powered and the stale qualification pointed at B, confirming is
    # refused with slot_quarantined (never no_verified_candidate ambiguity).
    import pytest
    from backend.api import service as svc
    from backend.service import ConfirmRequest
    with svc.store.transaction() as conn:
        svc.store.set_powered(conn, "dev-1", True)
        d = svc.store.load("dev-1")
        d.qualified_slot = "B"
        svc.store.save(conn, d)
    with pytest.raises(Exception) as exc:
        svc.confirm_switch("dev-1", ConfirmRequest())
    assert getattr(exc.value, "code", None) == "slot_quarantined"


def test_poisoned_device_crafted_at_storage_level_converges_the_same(client):
    """Hand-craft the exact legacy poisoned state (bypasses the HTTP hook)."""
    import os
    make_device(client)
    v2_digest = hashlib.sha256(_synthetic_payload("2.0.0")).hexdigest()
    _do_upgrade(client, "dev-1", "2.0.0", "r1")

    db_path = os.environ["DATA_PATH"]
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "UPDATE blobs SET content=? WHERE device_id=? AND slot=?",
            (OTHER_BYTES, "dev-1", "B"),
        )
        con.commit()
    finally:
        con.close()
    client.post("/api/devices/dev-1/power-off")

    r = client.post("/api/devices/dev-1/power-on")
    body = r.json()
    assert body["outcome"] == "unbootable"
    rec, dev = body["recovery"], body["device"]
    assert rec["active_slot"] is None
    assert dev["slots"]["B"]["status"] == "REJECTED"
    assert dev["slots"]["B"]["actual_digest"] == hashlib.sha256(OTHER_BYTES).hexdigest()
    assert dev["slots"]["B"]["digest"] == v2_digest
    # Nothing on flash was rewritten away: the mismatching bytes remain as
    # evidence, and the superseded old version is still not selected.
    assert _blob_bytes(db_path, "dev-1", "B") == OTHER_BYTES
    assert dev["slots"]["A"]["status"] == "SUPERSEDED"


def test_healthy_confirmed_slot_still_passes_reopen_audit(client):
    """The new audit must not disturb a normal two-upgrade happy path."""
    make_device(client)
    _do_upgrade(client, "dev-1", "2.0.0", "r1")
    _, sw2 = _do_upgrade(client, "dev-1", "3.0.0", "r2")
    assert sw2["active_slot"] == "A" and sw2["generation"] == 3

    client.post("/api/devices/dev-1/power-off")
    r = client.post("/api/devices/dev-1/power-on")
    rec, dev = r.json()["recovery"], r.json()["device"]
    assert rec["active_slot"] == "A"
    assert rec["generation"] == 3
    assert dev["slots"]["A"]["status"] == "CONFIRMED"
    assert dev["slots"]["A"]["quarantined"] is False
    assert dev["slots"]["B"]["status"] == "SUPERSEDED"
    assert all(dg["reason"] != "confirmed_digest_mismatch" for dg in rec["diagnoses"])
