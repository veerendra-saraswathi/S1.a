from __future__ import annotations

import json
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "TR-HILS-0058.json"


@pytest.fixture
def document() -> dict:
    """The worked example, decoded. Mutate it freely; each test gets its own copy."""
    return json.loads(EXAMPLE.read_text())
