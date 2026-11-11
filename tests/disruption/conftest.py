"""Disruption tests drain a node of a multi-node Kind cluster."""

import json
import subprocess
from pathlib import Path

import pytest

REPORT = Path(__file__).resolve().parents[2] / "artifacts" / "disruption-report.json"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "disruption: drains a worker node; run only with make test-disruption")


@pytest.fixture(scope="module")
def workers():
    nodes = json.loads(subprocess.run(["kubectl", "get", "nodes", "--output", "json"], check=True,
                                      capture_output=True, text=True, timeout=120).stdout)["items"]
    names = [node["metadata"]["name"] for node in nodes
             if "node-role.kubernetes.io/control-plane" not in node["metadata"]["labels"]]
    if len(names) < 2:
        pytest.fail("needs two worker nodes: make setup-kind KIND_CONFIG=tests/kind/cluster-disruption.yaml")
    return names


@pytest.fixture(scope="session")
def report():
    """Measurements of every test module, written to REPORT at the end."""
    result = {}
    yield result
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
