from __future__ import annotations

import json
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
BENCH_REPORT = EXAMPLES / "TR-HILS-0058.json"
BUILD_RECORD = EXAMPLES / "BLD-FC-2026.11.4.json"
BENCH_PASS = EXAMPLES / "TR-HILS-0059.json"


@pytest.fixture
def bench_document() -> dict:
    """The worked bench report, decoded. Mutate it freely; each test gets its own copy."""
    return json.loads(BENCH_REPORT.read_text())


@pytest.fixture
def build_document() -> dict:
    """The worked build record, decoded. Mutate it freely; each test gets its own copy."""
    return json.loads(BUILD_RECORD.read_text())


@pytest.fixture
def bench_pass_document() -> dict:
    """The worked bench report whose HILS run passed, decoded."""
    return json.loads(BENCH_PASS.read_text())
