"""Replayed-digest attack: every verification must measure the bytes written
by *this* candidate, and devices already poisoned by a wrong confirmation
must converge safely (never boot, never roll back) on the next reopen.
"""
from __future__ import annotations

import base64
import hashlib

import backend.api as api
from backend.models import SlotStatus

from tests.conftest import make_device


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def image(version: bytes, salt: bytes) -> bytes:
    """Deterministic 256-byte image; distinct salts yield distinct bytes."""
    head = b"img::" + version + b"::" + salt + b"::"
    return (head + bytes((i * 13 + len(head)) & 0xFF for i in range(256 - len(head))))[:256]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def setup_two_generations(client, dev="dev-1"):
    c1, c2 = image(b"1", b"a"), image(b"2", b"b")
    d1, d2 = digest(c1), digest(c2)
    client.post("/api/devices", json={
        "device_id": dev, "version": "1.0.0",
        "content_b64": b64(c1), "digest": d1,
    })
    r = client.post(f"/api/devices/{dev}/candidate", json={
        "version": "2.0.0", "request_id": "r1",
        "content_b64": b64(c2), "digest": d2,
    })
    assert r.json()["outcome"] == "staged"
    sw = client.post(f"/api/devices/{dev}/confirm", json={})
    assert sw.json()["outcome"] == "switched"
    return c1, d1, c2, d2


def test_candidate_reusing_previous_digest_is_measured_and_rejected(client):
    _c1, _d1, _c2, d2 = setup_two_generations(client)
    # v3 bytes are clearly different, yet the manifest claims the v2 digest.
    c3 = image(b"3", b"different")
    assert digest(c3) != d2

    r = client.post("/api/devices/dev-1/candidate", json={
        "version": "3.0.0", "request_id": "r-attack",
        "content_b64": b64(c3), "digest": d2,
    })
    body = r.json()
    assert body["outcome"] == "verification_failed"
    assert body["claimed_digest"] == d2
    # The verdict comes from the bytes actually persisted this time, not from
    # the earlier v2 verification cached under the same digest.
    assert body["actual_digest"] == digest(c3)

    dev = client.get("/api/devices/dev-1").json()["device"]
    slot_a = dev["slots"]["A"]
    assert slot_a["status"] == "REJECTED"
    assert slot_a["version"] == "3.0.0"
    assert slot_a["digest"] == d2                      # claimed (stale)
    assert slot_a["actual_digest"] == digest(c3)       # measured now
    # No generation advance, no active-slot switch.
    assert dev["generation"] == 2
    assert dev["active_slot"] == "B"
    assert dev["slots"]["B"]["version"] == "2.0.0"
    assert dev["qualified_request"] is None            # qualification released

    # Confirmation must be refused: the bad content is not a bootable image.
    r = client.post("/api/devices/dev-1/confirm", json={})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_verified_candidate"
    assert client.get("/api/devices/dev-1").json()["device"]["generation"] == 2

    # Reopen keeps booting the genuinely-confirmed v2 slot, not the bad one.
    client.post("/api/devices/dev-1/power-off")
    on = client.post("/api/devices/dev-1/power-on").json()
    assert on["recovery"]["active_slot"] == "B"
    assert on["recovery"]["generation"] == 2
    d = on["device"]
    assert d["active_slot"] == "B"
    assert d["slots"]["B"]["version"] == "2.0.0"
    assert d["slots"]["B"]["digest"] == d2
    assert d["slots"]["A"]["status"] == "REJECTED"
    diag = next(x for x in on["recovery"]["diagnoses"] if x["slot"] == "A")
    assert diag["reason"] == "digest_mismatch"

    # The rejected bytes remain on flash as reviewable diagnostic evidence.
    with api.store.transaction() as conn:
        row = conn.execute(
            "SELECT content FROM blobs WHERE device_id=? AND slot=?",
            ("dev-1", "A"),
        ).fetchone()
    assert row["content"] == c3
    ev = client.get("/api/devices/dev-1/evidence").json()["evidence"]
    assert any(e["reason"] == "digest_mismatch" and e["slot"] == "A" for e in ev)


def test_correct_higher_candidate_still_recovers_after_rejection(client):
    _c1, _d1, _c2, d2 = setup_two_generations(client)
    c3_bad = image(b"3", b"bad")
    client.post("/api/devices/dev-1/candidate", json={
        "version": "3.0.0", "request_id": "r-attack",
        "content_b64": b64(c3_bad), "digest": d2,
    })
    # A corrected candidate at the same generation re-wins qualification.
    c3 = image(b"3", b"good")
    r = client.post("/api/devices/dev-1/candidate", json={
        "version": "3.0.0", "request_id": "r-fixed",
        "content_b64": b64(c3), "digest": digest(c3),
    })
    assert r.status_code == 200 and r.json()["outcome"] == "staged"
    sw = client.post("/api/devices/dev-1/confirm", json={}).json()
    assert sw["outcome"] == "switched"
    assert sw["generation"] == 3 and sw["active_slot"] == "A"


def test_already_poisoned_confirmed_slot_converges_safely_on_reopen(client):
    """A device persisted by the old (vulnerable) build with a confirmed slot
    whose bytes contradict its claimed digest must not boot it on reopen."""
    _c1, _d1, _c2, d2 = setup_two_generations(client)
    c3 = image(b"3", b"poisoned")
    assert digest(c3) != d2

    # Reproduce the bad on-flash state: A CONFIRMED v3 claiming the v2 digest
    # while actually holding v3 bytes; B SUPERSEDED; generation 3.
    with api.store.transaction() as conn:
        dev = api.store.load("dev-1")
        a = dev.slots["A"]
        a.status = SlotStatus.CONFIRMED
        a.version = "3.0.0"
        a.digest = d2
        a.actual_digest = d2
        a.size = len(c3)
        a.written = len(c3)
        a.confirmed_generation = 3
        dev.slots["B"].status = SlotStatus.SUPERSEDED
        dev.generation = 3
        dev.active_slot = "A"
        api.store.write_blob(conn, "dev-1", "A", c3)
        api.store.save(conn, dev)

    client.post("/api/devices/dev-1/power-off")
    on = client.post("/api/devices/dev-1/power-on").json()

    # Safe convergence: refuses to boot the digest-contradicting confirmed slot.
    assert on["outcome"] == "unbootable"
    rec = on["recovery"]
    assert rec["active_slot"] is None
    assert rec["eligible"] == []
    assert rec["critical"]
    d = on["device"]
    assert d["active_slot"] is None
    assert d["generation"] == 3                       # confirmation generation not re-advanced
    a, b = d["slots"]["A"], d["slots"]["B"]
    assert a["status"] == "REJECTED"                  # quarantined
    assert a["version"] == "3.0.0"
    assert a["digest"] == d2
    assert a["actual_digest"] == digest(c3)
    assert a["confirmed_generation"] is None
    # No rollback: the displaced v2 slot stays SUPERSEDED and is never booted.
    assert b["status"] == "SUPERSEDED"
    assert b["version"] == "2.0.0"
    diag = next(x for x in rec["diagnoses"] if x["slot"] == "A")
    assert diag["reason"] == "digest_mismatch"
    assert any("永不回退" in x for x in rec["rationale"])

    # Reviewable evidence survives, and the bad bytes are retained untouched.
    ev = client.get("/api/devices/dev-1/evidence").json()["evidence"]
    assert any(e["reason"] == "digest_mismatch_on_reopen" for e in ev)
    with api.store.transaction() as conn:
        row = conn.execute(
            "SELECT content FROM blobs WHERE device_id=? AND slot=?",
            ("dev-1", "A"),
        ).fetchone()
    assert row["content"] == c3

    # The verdict is stable across further reopens: still no boot/rollback.
    client.post("/api/devices/dev-1/power-off")
    again = client.post("/api/devices/dev-1/power-on").json()
    assert again["outcome"] == "unbootable"
    assert again["device"]["active_slot"] is None
    assert again["device"]["slots"]["B"]["status"] == "SUPERSEDED"
