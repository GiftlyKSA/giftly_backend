"""Tests for the OpenAPI export and the TaskIQ broker entry points."""

from __future__ import annotations

import json
from pathlib import Path

import app.export_openapi as export_openapi
import pytest


def test_openapi_export_writes_schema(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "openapi.json"
    monkeypatch.setattr(export_openapi, "_OUTPUT", out)
    export_openapi.main()
    schema = json.loads(out.read_text())
    assert "/api/health" in schema["paths"]


def test_broker_module_imports() -> None:
    from app.workers.broker import broker

    assert broker is not None
