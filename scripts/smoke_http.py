#!/usr/bin/env python3
"""HTTP smoke test for power-loss recovery and concurrent adjudication.

Talks to a *live* uvicorn server (plain stdlib only) and exits non-zero on the
first failed expectation.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import threading
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
checks = 0


def synth_image(version: bytes, salt: bytes) -> bytes:
    head = b"img::" + version + b"::" + salt + b"::"
    return (head + bytes((i * 13 + len(head)) & 0xFF for i in range(256 - len(head))))[:256]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def call(method: str, path: str, body=None, expect=None):
    global checks
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status, payload = resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, json.loads(exc.read().decode())
    if expect is not None:
        checks += 1
        assert status == expect, f"{method} {path}: expected {expect}, got {status} {payload}"
    return status, payload


def assert_that(cond, text):
    global checks
    checks += 1
    assert cond, text


def power_cycle(dev, expected_slot, expected_gen):
    call("POST", f"/api/devices/{dev}/power-off", {}, 200)
    _, body = call("POST", f"/api/devices/{dev}/power-on", {}, 200)
    rec, d = body["recovery"], body["device"]
    assert_that(rec["active_slot"] == expected_slot,
                f"{dev}: recovery selected {rec['active_slot']}, want {expected_slot}")
    assert_that(rec["generation"] == expected_gen,
                f"{dev}: generation {rec['generation']}, want {expected_gen}")
    return rec, d


def main() -> int:
    # --- health + built page ------------------------------------------------
    _, health = call("GET", "/api/health", expect=200)
    assert_that(health["status"] == "ok", "health status not ok")
    with urllib.request.urlopen(BASE + "/", timeout=10) as resp:
        html = resp.read().decode()
    assert_that("轨道载荷" in html and "/src/main.js" not in html,
                "served page is not the built bundle")

    # --- power-loss scenarios on device s1 ----------------------------------
    call("POST", "/api/devices", {"device_id": "s1", "version": "1.0.0"}, 201)

    # fault 1: cut during candidate write -> incomplete, old slot boots
    _, b = call("POST", "/api/devices/s1/candidate",
                {"version": "2.0.0", "request_id": "w1", "fault_point": "candidate_write"}, 200)
    assert_that(b["outcome"] == "power_cut" and b["fault_point"] == "candidate_write",
                "candidate_write fault not injected")
    rec, d = power_cycle("s1", "A", 1)
    diag = {x["slot"]: x for x in rec["diagnoses"]}
    assert_that(diag["B"]["reason"] == "incomplete_write", "want incomplete_write diagnosis")
    assert_that(d["slots"]["B"]["written"] < d["slots"]["B"]["size"], "partial write expected")

    # fault 2: cut during digest check -> fully written but unproven
    _, b = call("POST", "/api/devices/s1/candidate",
                {"version": "2.0.0", "request_id": "w2", "fault_point": "digest_check"}, 200)
    assert_that(b["outcome"] == "power_cut", "digest_check fault not injected")
    rec, d = power_cycle("s1", "A", 1)
    diag = {x["slot"]: x for x in rec["diagnoses"]}
    assert_that(diag["B"]["reason"] == "unverified_candidate", "want unverified_candidate")
    assert_that(d["slots"]["B"]["actual_digest"] is None, "no verdict may be committed")

    # corrupt candidate -> REJECTED, evidence kept, never boots
    _, b = call("POST", "/api/devices/s1/candidate",
                {"version": "2.0.0", "request_id": "w3", "corrupt": True}, 200)
    assert_that(b["outcome"] == "verification_failed", "corrupt candidate must fail verification")
    assert_that(b["claimed_digest"] != b["actual_digest"], "digests must differ")
    rec, d = power_cycle("s1", "A", 1)
    assert_that(d["slots"]["B"]["status"] == "REJECTED", "corrupt slot must stay REJECTED")

    # normal stage, fault 3: cut at confirm -> old version, candidate unconfirmed
    call("POST", "/api/devices/s1/candidate",
         {"version": "2.0.0", "request_id": "w4"}, 200)
    _, b = call("POST", "/api/devices/s1/confirm", {"fault_point": "confirm_switch"}, 200)
    assert_that(b["outcome"] == "power_cut", "confirm_switch fault not injected")
    rec, d = power_cycle("s1", "A", 1)
    assert_that(d["slots"]["B"]["status"] == "VERIFIED", "candidate remains VERIFIED")

    # commit for real -> B active, generation 2, no rollback after reopen
    _, b = call("POST", "/api/devices/s1/confirm", {}, 200)
    assert_that(b["outcome"] == "switched" and b["generation"] == 2, "switch not committed")
    digest_b = b["device"]["slots"]["B"]["digest"]
    rec, d = power_cycle("s1", "B", 2)
    assert_that(d["slots"]["B"]["version"] == "2.0.0", "reopen version mismatch")
    assert_that(d["slots"]["B"]["digest"] == digest_b, "reopen digest mismatch")
    assert_that(d["slots"]["A"]["status"] == "SUPERSEDED", "old slot must be SUPERSEDED")
    assert_that(any("防回退" in x for x in rec["rationale"]), "no-rollback rationale missing")

    # --- concurrent different candidates on device s2 -----------------------
    call("POST", "/api/devices", {"device_id": "s2", "version": "1.0.0"}, 201)
    barrier = threading.Barrier(2)
    outcomes = []

    def concurrent_submit(version, request_id):
        barrier.wait()
        outcomes.append(call("POST", "/api/devices/s2/candidate",
                             {"version": version, "request_id": request_id}))

    t1 = threading.Thread(target=concurrent_submit, args=("2.0.0", "page-one"))
    t2 = threading.Thread(target=concurrent_submit, args=("2.1.0", "page-two"))
    t1.start(); t2.start(); t1.join(); t2.join()
    statuses = sorted(code for code, _ in outcomes)
    assert_that(statuses == [200, 409], f"concurrent statuses {statuses}")
    winner = next((p for code, p in outcomes if code == 200), None)
    loser = next((p for code, p in outcomes if code == 409), None)
    assert_that(loser["error"]["code"] == "upgrade_conflict", "loser must be upgrade_conflict")
    assert_that(loser["error"]["holder_request"] == winner["request_id"], "holder mismatch")
    _, d = call("GET", "/api/devices/s2", expect=200)
    d = d["device"]
    assert_that(d["active_slot"] == "A" and d["slots"]["A"]["version"] == "1.0.0",
                "loser must not rewrite active version")

    # winner confirms; reopen shows identical slot/version/digest/generation
    _, b = call("POST", "/api/devices/s2/confirm", {}, 200)
    assert_that(b["generation"] == 2 and b["active_slot"] == "B", "winner switch failed")
    before = b["device"]
    rec, after = power_cycle("s2", "B", 2)
    for field in ("status", "version", "digest", "confirmed_generation"):
        assert_that(after["slots"]["B"][field] == before["slots"]["B"][field],
                    f"field {field} changed across reopen")
    assert_that(after["generation"] == before["generation"], "generation changed across reopen")

    # --- replayed-digest attack on device s3: two real upgrades -------------
    # First upgrade is fully legitimate: v1 -> v2, content matches digest.
    img1, img2 = synth_image(b"1", b"a"), synth_image(b"2", b"b")
    dg1, dg2 = sha(img1), sha(img2)
    call("POST", "/api/devices", {
        "device_id": "s3", "version": "1.0.0",
        "content_b64": b64(img1), "digest": dg1}, 201)
    _, b = call("POST", "/api/devices/s3/candidate", {
        "version": "2.0.0", "request_id": "g1",
        "content_b64": b64(img2), "digest": dg2}, 200)
    assert_that(b["outcome"] == "staged", "legitimate v2 must stage")
    _, b = call("POST", "/api/devices/s3/confirm", {}, 200)
    assert_that(b["outcome"] == "switched" and b["generation"] == 2, "v2 switch must commit")
    rec, d = power_cycle("s3", "B", 2)
    assert_that(d["slots"]["B"]["version"] == "2.0.0" and d["slots"]["B"]["digest"] == dg2,
                "first upgrade must persist v2 with its own digest")

    # Second upgrade: higher version, clearly different bytes, but the manifest
    # reuses the PREVIOUS image digest. It must be re-measured and rejected.
    img3 = synth_image(b"3", b"different-bytes")
    dg3 = sha(img3)
    assert_that(dg3 != dg2, "attack payload must hash differently from reused digest")
    _, b = call("POST", "/api/devices/s3/candidate", {
        "version": "3.0.0", "request_id": "g-attack",
        "content_b64": b64(img3), "digest": dg2}, 200)
    assert_that(b["outcome"] == "verification_failed", "replayed-digest candidate must fail")
    assert_that(b["claimed_digest"] == dg2 and b["actual_digest"] == dg3,
                "verdict must come from the bytes written this time")

    # No generation advance, no slot switch; confirm is refused.
    _, d = call("GET", "/api/devices/s3", expect=200)
    d = d["device"]
    assert_that(d["generation"] == 2 and d["active_slot"] == "B", "reject must not advance/switch")
    assert_that(d["slots"]["A"]["status"] == "REJECTED", "bad candidate slot must be REJECTED")
    assert_that(d["slots"]["A"]["version"] == "3.0.0", "rejected slot keeps claimed version")
    assert_that(d["slots"]["A"]["digest"] == dg2 and d["slots"]["A"]["actual_digest"] == dg3,
                "rejected slot keeps both claimed and measured digests as evidence")
    code, body = call("POST", "/api/devices/s3/confirm", {})
    assert_that(code == 409 and body["error"]["code"] == "no_verified_candidate",
                "confirm of the mismatched candidate must be refused")

    # Power cycle: keeps booting genuine v2; bad slot diagnosed, evidence kept.
    rec, d = power_cycle("s3", "B", 2)
    diag = {x["slot"]: x for x in rec["diagnoses"]}
    assert_that(diag["A"]["reason"] == "digest_mismatch", "want digest_mismatch diagnosis on A")
    assert_that(d["slots"]["B"]["version"] == "2.0.0" and d["slots"]["B"]["digest"] == dg2,
                "reopen must keep the genuinely confirmed v2")
    _, ev = call("GET", "/api/devices/s3/evidence", expect=200)
    assert_that(any(e["reason"] == "digest_mismatch" and e["slot"] == "A" for e in ev["evidence"]),
                "digest_mismatch evidence must be retained")

    # A corrected higher candidate at the same generation can still proceed.
    img3ok = synth_image(b"3", b"good-bytes")
    _, b = call("POST", "/api/devices/s3/candidate", {
        "version": "3.0.0", "request_id": "g-fixed",
        "content_b64": b64(img3ok), "digest": sha(img3ok)}, 200)
    assert_that(b["outcome"] == "staged", "corrected candidate must stage")
    _, b = call("POST", "/api/devices/s3/confirm", {}, 200)
    assert_that(b["outcome"] == "switched" and b["generation"] == 3 and b["active_slot"] == "A",
                "corrected upgrade must commit at generation 3")

    print(f"SMOKE OK: {checks} HTTP assertions passed "
          f"(power-loss x3, corrupt candidate, concurrent 409, reopen consistency, "
          f"replayed-digest reject + recovery)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"SMOKE FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
