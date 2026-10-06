#!/usr/bin/env python3
"""HTTP smoke test for power-loss recovery and concurrent adjudication.

Talks to a *live* uvicorn server (plain stdlib only) and exits non-zero on the
first failed expectation.
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
checks = 0


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

    print(f"SMOKE OK: {checks} HTTP assertions passed "
          f"(power-loss x3, corrupt candidate, concurrent 409, reopen consistency)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"SMOKE FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
