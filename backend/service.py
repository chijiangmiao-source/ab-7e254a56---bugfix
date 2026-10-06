"""Upgrade orchestration and power-loss recovery adjudication.

The service enforces three safety rules:

1. **Write integrity** -- a slot only advances past ``CANDIDATE`` once the
   digest *of the bytes actually written this time* equals the manifest
   digest. Every submission re-measures the persisted blob: a verdict cached
   from an earlier candidate (even one that claimed the same digest) can never
   substitute for measuring the current write. A mismatch leaves the slot
   ``REJECTED`` with append-only evidence; it can never boot.
2. **Generation qualification** -- at any generation exactly one submitting
   request may stage a candidate. Competing submissions get a stable ``409``
   and touch nothing (SQLite ``BEGIN IMMEDIATE`` serialises the decision).
3. **Unique-confirmed-slot boot with re-verification** -- recovery re-hashes
   the persisted content of every CONFIRMED/VERIFIED slot against its manifest
   before selecting the unique bootable slot. A confirmed slot whose bytes
   contradict its claimed digest is quarantined (``REJECTED``, evidence kept)
   and never booted. When no slot survives, the device converges safely to a
   non-booting state: it never guesses and never rolls back to a
   ``SUPERSEDED`` slot.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Optional

from .models import (
    Device,
    Diagnosis,
    RecoveryReport,
    Slot,
    SlotStatus,
)
from .store import Store
from .versioning import is_higher, parse_version


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str, extra: dict | None = None):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra or {}


@dataclass
class CandidateRequest:
    version: str
    content: bytes
    digest: Optional[str] = None
    request_id: Optional[str] = None
    fault_point: Optional[str] = None  # candidate_write | digest_check


@dataclass
class ConfirmRequest:
    fault_point: Optional[str] = None  # confirm_switch


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class UpgradeService:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------ #
    def create_device(
        self,
        device_id: str,
        version: str,
        content: bytes,
        digest: Optional[str] = None,
    ) -> Device:
        parse_version(version)  # validate
        digest = digest or sha256_hex(content)
        slot_a = Slot(
            name="A",
            status=SlotStatus.CONFIRMED,
            version=version,
            digest=digest,
            actual_digest=digest,
            size=len(content),
            written=len(content),
            confirmed_generation=1,
        )
        slot_b = Slot(name="B")
        dev = Device(
            device_id=device_id,
            slots={"A": slot_a, "B": slot_b},
            active_slot="A",
            generation=1,
        )
        with self.store.transaction() as conn:
            if self.store.load_raw(device_id) is not None:
                raise ApiError(409, "device_exists", f"设备 {device_id} 已存在")
            try:
                self.store.insert_device(conn, dev, powered_on=True)
            except Exception as exc:  # sqlite3.IntegrityError
                raise ApiError(409, "device_exists", f"设备 {device_id} 已存在") from exc
            self.store.write_blob(conn, device_id, "A", content)
        report = RecoveryReport(
            active_slot="A",
            generation=1,
            powered_from_off=False,
            eligible=["A"],
            diagnoses=[
                Diagnosis("B", "empty_slot", "B 槽为空，无镜像清单，不可引导")
            ],
            rationale=[
                "初始上电：仅 A 槽具有完整清单且状态为 CONFIRMED",
                "唯一符合条件槽位即活动槽位，无需冲突裁决",
            ],
        )
        with self.store.transaction() as conn:
            dev = self.store.load(device_id)
            dev.last_recovery = report
            dev.recovery_history.append(report)
            self.store.save(conn, dev)
        return self.store.load(device_id)

    def get_device(self, device_id: str) -> Device:
        try:
            return self.store.load(device_id)
        except KeyError:
            raise ApiError(404, "not_found", f"设备 {device_id} 不存在")

    def list_devices(self) -> list[Device]:
        return self.store.load_all()

    # ------------------------------------------------------------------ #
    def submit_candidate(self, device_id: str, req: CandidateRequest) -> dict:
        """Stage a higher-version candidate into the inactive slot.

        Returns an outcome dict. A power cut mid-stage is *not* an HTTP error:
        the device is persisted in the powered-off state and the caller opens
        the device view (power-on recovery) to see the adjudication.
        """
        parse_version(req.version)
        request_id = req.request_id or f"req-{uuid.uuid4().hex[:12]}"
        claimed = (req.digest or sha256_hex(req.content)).lower()
        if req.fault_point not in (None, "candidate_write", "digest_check"):
            raise ApiError(400, "bad_fault_point", "未知故障点")

        with self.store.transaction() as conn:
            raw = self.store.load_raw(device_id)
            if raw is None:
                raise ApiError(404, "not_found", f"设备 {device_id} 不存在")
            if not raw.pop("_powered_on"):
                raise ApiError(409, "powered_off", "设备处于断电状态，请先上电恢复")
            dev = Device.from_dict(raw)

            if dev.active_slot is None:
                raise ApiError(
                    409,
                    "no_bootable_slot",
                    "设备处于安全收敛态（无内容实测一致且已确认的槽位），"
                    "已拒绝引导，需人工维修后再升级",
                )
            active = dev.slots[dev.active_slot]
            try:
                higher = is_higher(req.version, active.version or "0")
            except ValueError as exc:
                raise ApiError(422, "bad_version", str(exc))
            if not higher:
                raise ApiError(
                    422,
                    "version_not_higher",
                    f"候选版本 {req.version} 必须高于当前活动版本 {active.version}",
                )

            # --- generation qualification (single winner per generation) ---
            if (
                dev.qualified_generation == dev.generation
                and dev.qualified_request
                and dev.qualified_request != request_id
            ):
                raise ApiError(
                    409,
                    "upgrade_conflict",
                    "当前代次的升级资格已被另一请求取得",
                    extra={
                        "generation": dev.generation,
                        "holder_request": dev.qualified_request,
                        "active_slot": dev.active_slot,
                        "active_version": active.version,
                    },
                )

            target_name = dev.inactive_slot_name()
            target = dev.slots[target_name]
            target.status = SlotStatus.CANDIDATE
            target.version = req.version
            target.digest = claimed
            target.actual_digest = None
            target.size = len(req.content)
            target.written = 0
            target.confirmed_generation = None
            dev.qualified_generation = dev.generation
            dev.qualified_request = request_id
            dev.qualified_slot = target_name
            dev.add_evidence(
                target_name,
                "candidate_staged",
                f"请求 {request_id} 在代次 {dev.generation} 取得升级资格，"
                f"候选版本 {req.version}，清单摘要 {claimed[:16]}…，"
                f"镜像长度 {len(req.content)} 字节",
            )
            self.store.write_blob(conn, device_id, target_name, b"")
            self.store.save(conn, dev)

        # --- streaming write (each chunk is durable, like flash pages) ----
        total = len(req.content)
        cutoff = total
        if req.fault_point == "candidate_write":
            cutoff = total // 2 if total > 1 else 0
        pos = 0
        chunk = max(1, (total + 3) // 4) if total else 1
        while pos < cutoff:
            part = req.content[pos : pos + chunk]
            with self.store.transaction() as conn:
                dev = self.store.load(device_id)
                n = self.store.append_blob(conn, device_id, target_name, part)
                t = dev.slots[target_name]
                t.written = n
                self.store.save(conn, dev)
            pos += len(part)

        if req.fault_point == "candidate_write":
            return self._power_cut(
                device_id,
                target_name,
                "candidate_write",
                request_id,
                detail=f"写入中断：仅 {cutoff}/{total} 字节落盘，清单不完整",
            )

        # --- digest verification -----------------------------------------
        if req.fault_point == "digest_check":
            # Bytes are fully on flash, but the verification verdict was never
            # committed before power loss: slot stays an unproven CANDIDATE.
            return self._power_cut(
                device_id,
                target_name,
                "digest_check",
                request_id,
                detail=f"摘要校验中断：{total}/{total} 字节已写入，但校验结论未提交",
            )

        with self.store.transaction() as conn:
            dev = self.store.load(device_id)
            t = dev.slots[target_name]
            # Measure the bytes persisted by *this* submission. Never consult
            # a verdict carried over from an earlier candidate: two writes may
            # claim the same manifest digest while holding different bytes, and
            # the digest that matters is the digest of what is on flash now.
            actual = self._measure_blob(conn, device_id, target_name)
            t.actual_digest = actual
            if actual != claimed:
                t.status = SlotStatus.REJECTED
                # A failed attempt releases the qualification so a corrected
                # candidate can be submitted at the same generation.
                dev.qualified_generation = None
                dev.qualified_request = None
                dev.qualified_slot = None
                dev.add_evidence(
                    target_name,
                    "digest_mismatch",
                    f"候选 {t.version} 摘要不符：清单 {claimed}，实测 {actual}；"
                    f"损坏候选已保留，禁止引导",
                )
                self.store.save(conn, dev)
                return {
                    "outcome": "verification_failed",
                    "request_id": request_id,
                    "target_slot": target_name,
                    "claimed_digest": claimed,
                    "actual_digest": actual,
                    "device": self.store.load(device_id).to_dict(),
                }
            t.status = SlotStatus.VERIFIED
            dev.add_evidence(
                target_name,
                "digest_verified",
                f"候选 {t.version} 摘要一致（{actual[:16]}…），等待人工确认切换",
            )
            self.store.save(conn, dev)

        return {
            "outcome": "staged",
            "request_id": request_id,
            "target_slot": target_name,
            "claimed_digest": claimed,
            "actual_digest": claimed,
            "device": self.store.load(device_id).to_dict(),
        }

    # ------------------------------------------------------------------ #
    def confirm_switch(self, device_id: str, req: ConfirmRequest) -> dict:
        if req.fault_point not in (None, "confirm_switch"):
            raise ApiError(400, "bad_fault_point", "未知故障点")
        with self.store.transaction() as conn:
            raw = self.store.load_raw(device_id)
            if raw is None:
                raise ApiError(404, "not_found", f"设备 {device_id} 不存在")
            if not raw.pop("_powered_on"):
                raise ApiError(409, "powered_off", "设备处于断电状态，请先上电恢复")
            dev = Device.from_dict(raw)

            target_name = dev.qualified_slot or dev.inactive_slot_name()
            target = dev.slots[target_name]
            if target.status is not SlotStatus.VERIFIED:
                raise ApiError(
                    409,
                    "no_verified_candidate",
                    f"槽位 {target_name} 状态为 {target.status.value}，"
                    "不存在已验证待确认的候选",
                )
            # Re-measure the persisted bytes immediately before commit: never
            # confirm on trust, and never reuse an earlier verification.
            actual = self._measure_blob(conn, device_id, target_name)
            if actual != target.digest or target.digest is None:
                target.status = SlotStatus.REJECTED
                target.actual_digest = actual
                dev.add_evidence(
                    target_name,
                    "digest_mismatch",
                    f"确认前复检失败：清单 {target.digest}，实测 {actual}；禁止切换",
                )
                # A failed verification releases the qualification so a
                # corrected candidate can be submitted at the same generation.
                dev.qualified_generation = None
                dev.qualified_request = None
                dev.qualified_slot = None
                self.store.save(conn, dev)
                raise ApiError(
                    422,
                    "digest_mismatch",
                    "确认前摘要复检失败，已阻止切换并保留损坏证据",
                    extra={"actual_digest": actual},
                )
            target.actual_digest = actual
            baseline_version = (
                dev.slots[dev.active_slot].version
                if dev.active_slot is not None
                else "0"
            )
            if not is_higher(target.version or "0", baseline_version or "0"):
                raise ApiError(422, "version_not_higher", "候选版本不再高于活动版本")

            if req.fault_point == "confirm_switch":
                # Power loss before the atomic switch record commits: old slot
                # stays the unique confirmed slot; candidate remains unconfirmed.
                self.store.set_powered(conn, device_id, False)
                dev.add_evidence(
                    target_name,
                    "power_cut_confirm",
                    f"确认切换提交前断电：候选 {target.version} 尚未确认，"
                    f"代次仍为 {dev.generation}，继续引导旧版本",
                )
                self.store.save(conn, dev)
                return {
                    "outcome": "power_cut",
                    "fault_point": "confirm_switch",
                    "target_slot": target_name,
                    "device": self.store.load(device_id).to_dict(),
                }

            old_name = dev.active_slot
            if old_name is not None:
                old = dev.slots[old_name]
                old.status = SlotStatus.SUPERSEDED
            target.status = SlotStatus.CONFIRMED
            dev.generation += 1
            target.confirmed_generation = dev.generation
            dev.active_slot = target_name
            dev.qualified_generation = None
            dev.qualified_request = None
            dev.qualified_slot = None
            dev.add_evidence(
                target_name,
                "switch_committed",
                f"代次 {dev.generation} 已提交：活动槽位 {old_name or '无（安全收敛态）'} → {target_name}，"
                f"版本 {target.version} 生效；旧槽位标记 SUPERSEDED，禁止回退",
            )
            self.store.save(conn, dev)

        return {
            "outcome": "switched",
            "generation": dev.generation,
            "active_slot": target_name,
            "device": self.store.load(device_id).to_dict(),
        }

    # ------------------------------------------------------------------ #
    def power_off(self, device_id: str) -> dict:
        with self.store.transaction() as conn:
            if self.store.load_raw(device_id) is None:
                raise ApiError(404, "not_found", f"设备 {device_id} 不存在")
            self.store.set_powered(conn, device_id, False)
        return {"outcome": "powered_off", "device_id": device_id}

    def power_on(self, device_id: str) -> dict:
        """Re-open the device: run the recovery adjudication and boot."""
        with self.store.transaction() as conn:
            raw = self.store.load_raw(device_id)
            if raw is None:
                raise ApiError(404, "not_found", f"设备 {device_id} 不存在")
            was_off = not raw.pop("_powered_on")
            dev = Device.from_dict(raw)
            # Re-verify the persisted bytes of every proven/confirmed slot
            # *before* adjudication: a previously committed verdict can never
            # be trusted on its own when the device reopens.
            self._requarify_persisted_slots(conn, device_id, dev)
            report = self._adjudicate(dev)
            report.powered_from_off = was_off
            dev.last_recovery = report
            dev.recovery_history.append(report)

            if report.active_slot is not None:
                # Boot the unique confirmed slot; revive only if a healthy slot
                # exists. Stale upgrade qualification dies with the interrupted
                # attempt unless a VERIFIED candidate is still pending.
                dev.active_slot = report.active_slot
                qslot = dev.qualified_slot
                if qslot and dev.slots[qslot].status is not SlotStatus.VERIFIED:
                    dev.qualified_generation = None
                    dev.qualified_request = None
                    dev.qualified_slot = None
            else:
                # Safe convergence: nothing confirmed survived the content
                # re-check. Clear the stale boot pointer. A *healthy*
                # VERIFIED candidate (bytes re-measured matching) keeps its
                # qualification so the operator may still commit it as the new
                # boot; otherwise drop the stale qualification. Never fall
                # back to a SUPERSEDED slot and never guess between survivors.
                dev.active_slot = None
                qslot = dev.qualified_slot
                if not (qslot and dev.slots[qslot].status is SlotStatus.VERIFIED):
                    dev.qualified_generation = None
                    dev.qualified_request = None
                    dev.qualified_slot = None
            self.store.set_powered(conn, device_id, True)
            self.store.save(conn, dev)

        return {
            "outcome": "recovered" if report.active_slot else "unbootable",
            "recovery": report.to_dict(),
            "device": self.store.load(device_id).to_dict(),
        }

    # ------------------------------------------------------------------ #
    def _measure_blob(self, conn, device_id: str, slot: str) -> str:
        """Hash the bytes currently persisted for a slot -- the only digest
        a verification verdict may ever be based on."""
        row = conn.execute(
            "SELECT content FROM blobs WHERE device_id=? AND slot=?",
            (device_id, slot),
        ).fetchone()
        return sha256_hex(row["content"] if row else b"")

    def _requarify_persisted_slots(self, conn, device_id: str, dev: Device) -> None:
        """Re-hash persisted content of every VERIFIED/CONFIRMED slot on open.

        A slot only keeps its trusted status while its bytes hash to its
        manifest digest. A mismatch proves the persisted image contradicts the
        claimed digest (e.g. a verdict copied from an earlier candidate); the
        slot is quarantined as REJECTED with append-only evidence and can
        neither boot nor be confirmed. This is what makes devices already
        affected by a bad confirmation converge safely on the next reopen.
        """
        for name, slot in dev.slots.items():
            if slot.status not in (SlotStatus.VERIFIED, SlotStatus.CONFIRMED):
                continue
            if not slot.manifest_complete() or not slot.digest:
                continue
            measured = self._measure_blob(conn, device_id, name)
            if measured != slot.digest:
                prior = slot.status.value
                slot.status = SlotStatus.REJECTED
                slot.actual_digest = measured
                slot.confirmed_generation = None
                dev.add_evidence(
                    name,
                    "digest_mismatch_on_reopen",
                    f"重开复核发现槽位内容与清单摘要不符（此前状态 {prior}，"
                    f"版本 {slot.version}）：清单 {slot.digest}，实测 {measured}；"
                    "已隔离为 REJECTED 并保留证据，禁止引导、禁止确认",
                )

    # ------------------------------------------------------------------ #
    def _adjudicate(self, dev: Device) -> RecoveryReport:
        """Pick the unique bootable slot; explain every other slot's fate."""
        report = RecoveryReport(
            active_slot=None,
            generation=dev.generation,
            powered_from_off=True,
        )
        eligible: list[str] = []
        for name in sorted(dev.slots):
            slot = dev.slots[name]
            ok, reason, detail = self._slot_verdict(slot)
            if ok:
                eligible.append(name)
            else:
                report.diagnoses.append(Diagnosis(name, reason, detail))

        report.rationale.append(
            "恢复规则：仅从【清单完整 且 状态为 CONFIRMED】的槽位中选定唯一活动槽位"
        )
        report.rationale.append(f"当前确认代次：{dev.generation}")

        if len(eligible) == 1:
            chosen = eligible[0]
            report.active_slot = chosen
            report.eligible = eligible
            slot = dev.slots[chosen]
            report.rationale.append(
                f"裁决：{chosen} 槽是唯一合格槽位（版本 {slot.version}，"
                f"摘要 {slot.digest[:16] if slot.digest else '—'}…，"
                f"确认代次 {slot.confirmed_generation}），从该槽位引导"
            )
            supersede = [
                n
                for n, s in dev.slots.items()
                if s.status is SlotStatus.SUPERSEDED
            ]
            if supersede:
                report.rationale.append(
                    f"防回退：{', '.join(supersede)} 槽已被更新代次取代"
                    "（SUPERSEDED），新版本生效后永不回退"
                )
        elif len(eligible) == 0:
            superseded = [
                n for n, s in dev.slots.items()
                if s.status is SlotStatus.SUPERSEDED
            ]
            report.critical = (
                "不存在任何清单完整、内容实测一致且已确认的槽位，设备安全收敛、"
                "拒绝引导；所有未确认/损坏候选均保留为诊断证据且未被选择"
            )
            report.rationale.append(
                "裁决：零合格槽位，保持关机/维修状态，不做猜测性引导"
            )
            if superseded:
                report.rationale.append(
                    f"防回退：{', '.join(superseded)} 槽虽保留但已被更新代次取代"
                    "（SUPERSEDED），即使无槽可引导也永不回退到旧版本"
                )
        else:
            report.critical = (
                f"合格槽位不唯一（{eligible}），拒绝猜测性引导，等待人工裁决"
            )
            report.eligible = eligible
            report.rationale.append("裁决：多候选冲突，拒绝引导")
        return report

    @staticmethod
    def _slot_verdict(slot: Slot) -> tuple[bool, str, str]:
        if slot.status is SlotStatus.CONFIRMED and slot.manifest_complete():
            return True, "eligible", "清单完整且已确认，具备引导资格"
        if slot.status is SlotStatus.EMPTY:
            return False, "empty_slot", "空槽位，无镜像清单"
        if slot.status is SlotStatus.CANDIDATE:
            if slot.size is not None and slot.written < slot.size:
                return (
                    False,
                    "incomplete_write",
                    f"候选写入中断：仅 {slot.written}/{slot.size} 字节落盘，"
                    "清单不完整且未经确认，禁止引导",
                )
            return (
                False,
                "unverified_candidate",
                f"候选 {slot.version or ''} 已写入但摘要校验未完成/未提交，"
                "状态为 CANDIDATE，未经确认，禁止引导",
            )
        if slot.status is SlotStatus.VERIFIED:
            return (
                False,
                "unconfirmed_candidate",
                f"候选 {slot.version or ''} 摘要虽已验证一致，但未经人工确认，"
                "不具备引导资格",
            )
        if slot.status is SlotStatus.REJECTED:
            return (
                False,
                "digest_mismatch",
                f"候选 {slot.version or ''} 摘要不符（清单 {slot.digest}，"
                f"实测 {slot.actual_digest}），损坏证据已保留，禁止引导",
            )
        if slot.status is SlotStatus.SUPERSEDED:
            return (
                False,
                "superseded_no_rollback",
                f"版本 {slot.version or ''} 已在代次 {slot.confirmed_generation} "
                "被更新版本取代，按防回退规则永不重新选择",
            )
        return False, "unknown", f"未知状态 {slot.status.value}"

    def _power_cut(
        self, device_id: str, slot: str, fault_point: str, request_id: str, detail: str
    ) -> dict:
        with self.store.transaction() as conn:
            dev = self.store.load(device_id)
            self.store.set_powered(conn, device_id, False)
            dev.add_evidence(slot, "power_cut", f"故障点 {fault_point}：{detail}")
            self.store.save(conn, dev)
        return {
            "outcome": "power_cut",
            "fault_point": fault_point,
            "request_id": request_id,
            "target_slot": slot,
            "detail": detail,
            "device": self.store.load(device_id).to_dict(),
        }
