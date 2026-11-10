"""Upgrade tests reuse the fixtures and helpers of tests/integration."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "upgrade: upgrades the installed release; run only with make test-upgrade")
