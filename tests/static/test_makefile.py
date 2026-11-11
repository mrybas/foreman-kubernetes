"""Static checks of Makefile targets that do not need a cluster."""

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_lint_skips_artifacts_and_venv(tmp_path):
    """make lint must not descend into artifacts/ or .venv/: after
    `sudo make test` artifacts/ can hold directories the user cannot read."""
    shutil.copy(ROOT / "Makefile", tmp_path / "Makefile")
    chart = tmp_path / "charts" / "example"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: example\nversion: 0.1.0\n")
    for skipped in ("artifacts", ".venv"):
        broken = tmp_path / skipped / "broken"
        broken.mkdir(parents=True)
        (broken / "Chart.yaml").write_text("not: a chart\n")
    locked = tmp_path / "artifacts" / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        result = subprocess.run(["make", "-C", str(tmp_path), "lint"],
                                capture_output=True, text=True, check=False)
    finally:
        locked.chmod(0o700)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 chart(s) linted, 0 chart(s) failed" in result.stdout
