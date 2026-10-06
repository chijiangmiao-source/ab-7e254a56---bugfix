"""Domain models for A/B dual-slot payload image upgrade.

Slot lifecycle
--------------
A slot manifest goes through these stages:

* ``EMPTY``      -- slot record exists but no image has been written
* ``CANDIDATE``  -- candidate bytes written (or partially), digest not proven
* ``VERIFIED``   -- digest verified against the manifest, awaiting confirmation
* ``CONFIRMED``  -- operator confirmed at a unique confirmation generation;
                    only a CONFIRMED slot with a complete manifest may boot
* ``REJECTED``   -- digest verification failed (at submit, at the pre-confirm
                    re-check, or at the reopen byte audit of a formerly
                    CONFIRMED slot); corrupt candidate evidence is kept and the
                    slot is quarantined -- it can never boot or be confirmed
* ``SUPERSEDED`` -- a formerly CONFIRMED slot replaced by a higher generation;
                    it is retained but can never be selected again (no rollback)

Power loss can interrupt any step. Recovery never trusts an in-flight write or
an unverified/unconfirmed candidate: it selects the *unique* slot whose
manifest is complete AND whose status is CONFIRMED -- and even a CONFIRMED slot
is re-measured against its persisted bytes on every reopen, so a slot whose
flash contents no longer match its manifest digest is quarantined before it can
boot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SlotStatus(str, Enum):
    EMPTY = "EMPTY"
    CANDIDATE = "CANDIDATE"
    VERIFIED = "VERIFIED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"

    @property
    def bootable(self) -> bool:
        return self is SlotStatus.CONFIRMED


# Stages at which a simulated power cut may be injected.
FAULT_POINTS = (
    "candidate_write",  # power cut while candidate bytes are streaming in
    "digest_check",     # power cut while the digest is being verified
    "confirm_switch",   # power cut while the confirmation is being committed
)


@dataclass
class Slot:
    name: str
    status: SlotStatus = SlotStatus.EMPTY
    version: Optional[str] = None
    digest: Optional[str] = None          # digest claimed by the manifest
    actual_digest: Optional[str] = None   # digest measured after a full write
    size: Optional[int] = None            # None => factory-provisioned slot
    written: int = 0
    confirmed_generation: Optional[int] = None
    # Set when a formerly CONFIRMED slot fails the reopen-time byte audit: it is
    # quarantined to REJECTED and can never be selected or confirmed again.
    quarantined: bool = False
    quarantine_generation: Optional[int] = None

    def manifest_complete(self) -> bool:
        """True iff the write finished and a manifest digest is present.

        Note: completeness alone is not enough to boot -- CONFIRMED status is
        also required. REJECTED/SUPERSEDED slots may carry complete manifests
        but must never be selected.
        """
        if self.status in (SlotStatus.EMPTY, SlotStatus.CANDIDATE):
            return False
        if not self.digest:
            return False
        if self.size is None:
            return True  # factory slot provisioned directly from a manifest
        return self.written == self.size

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "version": self.version,
            "digest": self.digest,
            "actual_digest": self.actual_digest,
            "size": self.size,
            "written": self.written,
            "confirmed_generation": self.confirmed_generation,
            "quarantined": self.quarantined,
            "quarantine_generation": self.quarantine_generation,
            "manifest_complete": self.manifest_complete(),
            "bootable": self.status.bootable and self.manifest_complete(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Slot":
        return cls(
            name=data["name"],
            status=SlotStatus(data["status"]),
            version=data.get("version"),
            digest=data.get("digest"),
            actual_digest=data.get("actual_digest"),
            size=data.get("size"),
            written=data.get("written", 0),
            confirmed_generation=data.get("confirmed_generation"),
            quarantined=data.get("quarantined", False),
            quarantine_generation=data.get("quarantine_generation"),
        )


@dataclass
class Diagnosis:
    """Evidence explaining why a slot was (not) chosen during recovery."""

    slot: str
    reason: str
    detail: str

    def to_dict(self) -> dict:
        return {"slot": self.slot, "reason": self.reason, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: dict) -> "Diagnosis":
        return cls(slot=data["slot"], reason=data["reason"], detail=data["detail"])


@dataclass
class RecoveryReport:
    active_slot: Optional[str]
    generation: int
    powered_from_off: bool
    eligible: list[str] = field(default_factory=list)
    diagnoses: list[Diagnosis] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)
    critical: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "active_slot": self.active_slot,
            "generation": self.generation,
            "powered_from_off": self.powered_from_off,
            "eligible": list(self.eligible),
            "diagnoses": [d.to_dict() for d in self.diagnoses],
            "rationale": list(self.rationale),
            "critical": self.critical,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RecoveryReport":
        return cls(
            active_slot=data.get("active_slot"),
            generation=data["generation"],
            powered_from_off=data.get("powered_from_off", False),
            eligible=list(data.get("eligible", [])),
            diagnoses=[Diagnosis.from_dict(d) for d in data.get("diagnoses", [])],
            rationale=list(data.get("rationale", [])),
            critical=data.get("critical"),
        )


@dataclass
class Device:
    device_id: str
    slots: dict[str, Slot]
    active_slot: str
    # Monotonic confirmation generation; bumped only by a committed confirm.
    generation: int = 0
    # Qualification for the upgrade at the *current* generation. Exactly one
    # submitting request may hold it; every other submit gets a stable 409.
    qualified_generation: Optional[int] = None
    qualified_request: Optional[str] = None
    qualified_slot: Optional[str] = None
    last_recovery: Optional[RecoveryReport] = None
    recovery_history: list[RecoveryReport] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    evidence_seq: int = 0

    def slot(self, name: str) -> Slot:
        return self.slots[name]

    def inactive_slot_name(self) -> str:
        return "B" if self.active_slot == "A" else "A"

    def add_evidence(self, slot: str, reason: str, detail: str) -> None:
        self.evidence_seq += 1
        self.evidence.append(
            {
                "seq": self.evidence_seq,
                "slot": slot,
                "reason": reason,
                "detail": detail,
            }
        )

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "active_slot": self.active_slot,
            "generation": self.generation,
            "qualified_generation": self.qualified_generation,
            "qualified_request": self.qualified_request,
            "qualified_slot": self.qualified_slot,
            "slots": {name: s.to_dict() for name, s in self.slots.items()},
            "last_recovery": self.last_recovery.to_dict()
            if self.last_recovery
            else None,
            "recovery_history": [r.to_dict() for r in self.recovery_history],
            "evidence": list(self.evidence),
            "evidence_seq": self.evidence_seq,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Device":
        dev = cls(
            device_id=data["device_id"],
            slots={name: Slot.from_dict(s) for name, s in data["slots"].items()},
            active_slot=data["active_slot"],
            generation=data.get("generation", 0),
            qualified_generation=data.get("qualified_generation"),
            qualified_request=data.get("qualified_request"),
            qualified_slot=data.get("qualified_slot"),
            evidence=list(data.get("evidence", [])),
            evidence_seq=data.get("evidence_seq", 0),
        )
        if data.get("last_recovery"):
            dev.last_recovery = RecoveryReport.from_dict(data["last_recovery"])
        dev.recovery_history = [
            RecoveryReport.from_dict(r) for r in data.get("recovery_history", [])
        ]
        return dev
