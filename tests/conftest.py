"""Shared pytest fixtures: isolated DB + FastAPI TestClient."""
from __future__ import annotations

import importlib
import sys

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "test.db"
    import os
    os.environ["DATA_PATH"] = str(db_path)

    import backend.api as api
    importlib.reload(api)  # pick up DATA_PATH and a fresh Store
    from fastapi.testclient import TestClient

    with TestClient(api.app) as c:
        yield c
    api.store.close()


def make_device(client, device_id="dev-1", version="1.0.0"):
    r = client.post("/api/devices", json={"device_id": device_id, "version": version})
    assert r.status_code == 201, r.text
    return r.json()["device"]
