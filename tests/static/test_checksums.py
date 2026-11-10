"""Static assertions on the checksum/config pod annotation.

helm upgrade replaces pods only when their pod template changes. Every
workload that reads a chart ConfigMap carries the sha256 of that data, so a
configuration change restarts exactly the workloads that read it.
"""

import shutil
import subprocess

import pytest
import yaml

from conftest import CHART, NAMESPACE, RELEASE, Manifests, render_chart

ANNOTATION = "checksum/config"
EVERYTHING = {
    **{name: {"enabled": True} for name in
       ("certificates", "dynflow", "httpd", "pulp", "candlepin", "katello", "proxy")},
    "foreman": {"fqdn": "foreman.test", "externalUrl": "https://foreman.test"},
}
FOREMAN = {"foreman-foreman", "foreman-foreman-dynflow-orchestrator",
           "foreman-foreman-dynflow-worker", "foreman-foreman-dynflow-worker-hosts-queue"}
PULP = {"foreman-foreman-pulp-api", "foreman-foreman-pulp-content", "foreman-foreman-pulp-worker"}
CANDLEPIN = {"foreman-foreman-candlepin"}
HTTPD = {"foreman-foreman-httpd"}
PROXY = {"foreman-foreman-proxy"}
WORKLOADS = FOREMAN | PULP | CANDLEPIN | HTTPD | PROXY

# Override -> Deployments whose configuration it changes.
CHANGES = {
    "foreman settings.yaml": ({"foreman": {"trustedProxies": ["192.0.2.0/24"]}}, FOREMAN),
    "katello.yaml": ({"katello": {"restClientTimeout": 1234}}, FOREMAN),
    "candlepin": ({"candlepin": {"javaOpts": "-Xmx2g"}}, CANDLEPIN),
    "pulp settings.py": ({"pulp": {"settings": {"extra": "WORKER_TTL = 60"}}}, PULP),
    "httpd": ({"httpd": {"serverAliases": ["alias.foreman.test"]}}, HTTPD),
    "proxy": ({"proxy": {"logLevel": "debug"}}, PROXY),
    "no ConfigMap": ({"replicaCount": 2, "dynflow": {"worker": {"replicas": 2}}}, set()),
}


def merged(base, override):
    result = yaml.safe_load(yaml.safe_dump(base))
    for key, value in override.items():
        if isinstance(value, dict):
            result[key] = merged(result.get(key, {}), value)
        else:
            result[key] = value
    return result


def checksums(manifests):
    """Deployment name -> checksum/config annotation (None when missing)."""
    return {doc["metadata"]["name"]:
            (doc["spec"]["template"]["metadata"].get("annotations") or {}).get(ANNOTATION)
            for doc in manifests.of_kind("Deployment")}


def config_maps_read(deployment):
    """Names of the ConfigMaps a Deployment mounts."""
    names = set()
    for volume in deployment["spec"]["template"]["spec"].get("volumes", []):
        if "configMap" in volume:
            names.add(volume["configMap"]["name"])
        for source in volume.get("projected", {}).get("sources", []):
            if "configMap" in source:
                names.add(source["configMap"]["name"])
    return names


def test_every_workload_reading_a_chart_config_map_has_a_checksum(render):
    manifests = render(EVERYTHING)
    chart_config_maps = {doc["metadata"]["name"] for doc in manifests.of_kind("ConfigMap")}
    found = checksums(manifests)
    assert set(found) == WORKLOADS
    for deployment in manifests.of_kind("Deployment"):
        name = deployment["metadata"]["name"]
        assert config_maps_read(deployment) & chart_config_maps, name
        assert found[name] and len(found[name]) == 64, (name, found[name])


def test_default_web_has_a_checksum(manifests):
    assert checksums(manifests) == {"foreman-foreman": checksums(manifests)["foreman-foreman"]}
    assert checksums(manifests)["foreman-foreman"]


def test_checksums_are_stable_across_renders(render):
    assert checksums(render_chart()) == checksums(render_chart())
    assert checksums(render(EVERYTHING)) == checksums(render(EVERYTHING))


@pytest.mark.parametrize("change", list(CHANGES))
def test_config_change_restarts_exactly_the_workloads_that_read_it(render, change):
    override, affected = CHANGES[change]
    before = checksums(render(EVERYTHING))
    after = checksums(render(merged(EVERYTHING, override)))
    assert set(after) == set(before)
    changed = {name for name in before if before[name] != after[name]}
    assert changed == affected


def test_new_chart_version_keeps_the_checksums(tmp_path):
    """Only ConfigMap data is hashed, not labels such as helm.sh/chart."""
    chart = tmp_path / "foreman"
    shutil.copytree(CHART, chart)
    metadata = yaml.safe_load((chart / "Chart.yaml").read_text())
    metadata["version"] = "99.0.0"
    (chart / "Chart.yaml").write_text(yaml.safe_dump(metadata))
    values = tmp_path / "values.yaml"
    values.write_text(yaml.safe_dump(EVERYTHING))

    def render_version(path):
        result = subprocess.run(
            ["helm", "template", RELEASE, str(path), "--namespace", NAMESPACE,
             "--values", str(values)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        manifests = Manifests()
        for doc in yaml.safe_load_all(result.stdout):
            if doc:
                manifests[(doc["kind"], doc["metadata"]["name"])] = doc
        return manifests

    original, bumped = render_version(CHART), render_version(chart)
    labels = {doc["metadata"]["labels"]["helm.sh/chart"]
              for doc in bumped.of_kind("ConfigMap") if "helm.sh/chart" in doc["metadata"]["labels"]}
    assert labels == {"foreman-99.0.0"}
    assert checksums(bumped) == checksums(original)
