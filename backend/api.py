"""HTTP API for the dual-slot upgrade lab."""
from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .service import (
    ApiError,
    CandidateRequest,
    ConfirmRequest,
    UpgradeService,
    sha256_hex,
)
from .store import Store

DATA_PATH = os.environ.get("DATA_PATH", str(Path(__file__).resolve().parent.parent / "data" / "upgrade.db"))

store = Store(DATA_PATH)
service = UpgradeService(store)

app = FastAPI(title="轨道载荷 A/B 双槽升级服务", version="1.0.0")


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "detail": exc.detail, **exc.extra}},
    )


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": app.title, "devices": len(store.list_device_ids())}


class CreateDeviceBody(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    version: str
    content_b64: Optional[str] = None
    digest: Optional[str] = None


class CandidateBody(BaseModel):
    version: str
    content_b64: Optional[str] = None
    digest: Optional[str] = None
    request_id: Optional[str] = None
    fault_point: Optional[str] = None
    corrupt: bool = False  # when true, flip a payload byte to force digest mismatch


class ConfirmBody(BaseModel):
    fault_point: Optional[str] = None


def _decode_content(content_b64: Optional[str], version: str, corrupt: bool) -> bytes:
    if content_b64:
        try:
            data = base64.b64decode(content_b64, validate=True)
        except binascii.Error as exc:
            raise ApiError(400, "bad_content", "content_b64 不是合法的 Base64") from exc
    else:
        # Deterministic synthetic payload image: 256 bytes, version tagged.
        tag = f"payload-image::{version}::".encode()
        data = (tag + bytes((i * 7 + len(version)) & 0xFF for i in range(256 - len(tag))))[:256]
    return data


def _corrupt(data: bytes) -> bytes:
    """Flip the first payload byte to simulate flash corruption."""
    if not data:
        data = b"\x00"
    return bytes([data[0] ^ 0xFF]) + data[1:]


def _device_view(device_id: str) -> dict:
    dev = service.get_device(device_id)
    body = dev.to_dict()
    body["powered_on"] = store.is_powered_on(device_id)
    return body


@app.get("/api/devices")
def list_devices() -> dict:
    devices = []
    for did in store.list_device_ids():
        dev = store.load(did).to_dict()
        dev["powered_on"] = store.is_powered_on(did)
        devices.append(dev)
    return {"devices": devices}


@app.post("/api/devices", status_code=201)
def create_device(body: CreateDeviceBody) -> dict:
    content = _decode_content(body.content_b64, body.version, corrupt=False)
    service.create_device(body.device_id, body.version, content, body.digest)
    return {"outcome": "created", "device": _device_view(body.device_id)}


@app.get("/api/devices/{device_id}")
def get_device(device_id: str) -> dict:
    return {"device": _device_view(device_id)}


@app.post("/api/devices/{device_id}/candidate")
def submit_candidate(device_id: str, body: CandidateBody) -> dict:
    content = _decode_content(body.content_b64, body.version, body.corrupt)
    # For a corruption demo the manifest keeps the *clean* digest while the
    # bytes on flash are damaged, so verification is forced to fail.
    claimed = body.digest.lower() if body.digest else None
    if body.corrupt:
        claimed = claimed or sha256_hex(content)
        content = _corrupt(content)
    req = CandidateRequest(
        version=body.version,
        content=content,
        digest=claimed,
        request_id=body.request_id,
        fault_point=body.fault_point,
    )
    result = service.submit_candidate(device_id, req)
    result["device"] = _device_view(device_id)
    return result


@app.post("/api/devices/{device_id}/confirm")
def confirm_switch(device_id: str, body: ConfirmBody) -> dict:
    result = service.confirm_switch(device_id, ConfirmRequest(fault_point=body.fault_point))
    result["device"] = _device_view(device_id)
    return result


@app.post("/api/devices/{device_id}/power-off")
def power_off(device_id: str) -> dict:
    result = service.power_off(device_id)
    result["device"] = _device_view(device_id)
    return result


@app.post("/api/devices/{device_id}/power-on")
def power_on(device_id: str) -> dict:
    result = service.power_on(device_id)
    result["device"] = _device_view(device_id)
    return result


@app.get("/api/devices/{device_id}/evidence")
def evidence(device_id: str) -> dict:
    dev = service.get_device(device_id)
    return {"evidence": dev.evidence, "recovery_history": [r.to_dict() for r in dev.recovery_history]}


def mount_static() -> None:
    dist = Path(__file__).resolve().parent.parent / "web" / "dist"
    if dist.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(dist), html=True), name="static")


mount_static()
