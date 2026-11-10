"""Render the foreman Helm chart with `helm template` for static assertions."""

import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "charts" / "foreman"
RELEASE = "foreman"
NAMESPACE = "foreman"


class Manifests(dict):
    """Rendered documents keyed by (kind, metadata.name)."""

    def of_kind(self, *kinds):
        return [doc for (kind, _), doc in self.items() if kind in kinds]


def render_chart(values_file=None):
    command = ["helm", "template", RELEASE, str(CHART), "--namespace", NAMESPACE]
    if values_file:
        command += ["--values", str(values_file)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    manifests = Manifests()
    for doc in yaml.safe_load_all(result.stdout):
        if doc:
            key = (doc["kind"], doc["metadata"]["name"])
            assert key not in manifests, f"duplicate manifest {key}"
            manifests[key] = doc
    return manifests


@pytest.fixture(scope="session")
def manifests():
    """The chart rendered with its default values."""
    return render_chart()


@pytest.fixture
def render(tmp_path):
    """Render the chart with a dict of values overrides."""

    def _render(values):
        values_file = tmp_path / "values.yaml"
        values_file.write_text(yaml.safe_dump(values))
        return render_chart(values_file)

    return _render
