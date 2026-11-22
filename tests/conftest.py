from __future__ import annotations

import os
from pathlib import Path

import pytest

from supplygate.generator import GenerationConfig, generate


@pytest.fixture(scope="session")
def generated(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("generated")
    manifest = generate(out, GenerationConfig(seed=int(os.environ.get("SUPPLYGATE_TEST_SEED", "7"))))
    return out, manifest
