"""Static assertions on the Katello plugin settings of the foreman Helm chart."""

import pytest
import yaml

from test_render import (
    FULLNAME, containers, pod_spec,
    test_containers_use_restricted_security_context as check_security_context,
    test_resources_have_standard_labels as check_labels,
    test_workload_containers_request_cpu_and_memory as check_requests,
)

DEPENDENCIES = ("dynflow", "pulp", "candlepin", "certificates", "httpd")
ENABLED = {
    **{name: {"enabled": True} for name in DEPENDENCIES},
    "katello": {"enabled": True},
    "foreman": {"fqdn": "foreman.test", "externalUrl": "https://foreman.test"},
}
JOB = f"{FULLNAME}-pulp-smart-proxy-1"
CONFIG_MAPS = (f"{FULLNAME}-config", f"{FULLNAME}-migration-config")
KATELLO_YAML = "/etc/foreman/plugins/katello.yaml"


def values(**overrides):
    result = yaml.safe_load(yaml.safe_dump(ENABLED))
    for key, value in overrides.items():
        result.setdefault(key, {}).update(value)
    return result


@pytest.fixture
def enabled(render):
    return render(values())


def foreman_workloads(manifests):
    """Workloads that run the Foreman image with Rails: web, Dynflow, Jobs, CronJobs."""
    return [doc for doc in manifests.of_kind("Deployment", "Job", "CronJob")
            if pod_spec(doc)["containers"][0]["image"].startswith("quay.io/foreman/foreman:")]


def env(container):
    return {e["name"]: e for e in container.get("env", [])}


def katello_settings(manifests, config_map):
    return yaml.safe_load(manifests["ConfigMap", config_map]["data"]["katello.yaml"])


def test_disabled_by_default(manifests, render):
    for manifest in (manifests, render({name: {"enabled": True} for name in DEPENDENCIES})):
        assert ("Job", JOB) not in manifest
        for name in CONFIG_MAPS:
            assert "katello.yaml" not in manifest["ConfigMap", name]["data"]
        for workload in foreman_workloads(manifest):
            assert "initContainers" not in pod_spec(workload)
            for container in containers(workload):
                plugins = env(container).get("FOREMAN_ENABLED_PLUGINS", {}).get("value", "")
                assert "katello" not in plugins.split()


@pytest.mark.parametrize("dependency", DEPENDENCIES)
def test_requires_dependencies(render, dependency):
    with pytest.raises(AssertionError, match=f"katello.enabled requires {dependency}.enabled"):
        render(values(**{dependency: {"enabled": False}}))


def test_requires_https_external_url(render):
    with pytest.raises(AssertionError, match="https foreman.externalUrl"):
        render(values(foreman={"externalUrl": "http://foreman.test"}))


def test_chart_wide_checks_hold(enabled):
    check_security_context(enabled)
    check_requests(enabled)
    check_labels(enabled)
    assert not enabled.of_kind("Secret")


@pytest.mark.parametrize("config_map", CONFIG_MAPS)
def test_katello_yaml_has_no_secret(enabled, config_map):
    settings = katello_settings(enabled, config_map)
    assert settings == {":katello": {
        ":rest_client_timeout": 3600,
        ":candlepin": {
            ":url": "https://foreman-foreman-candlepin:23443/candlepin",
            ":oauth_key": "katello",
            ":ca_cert_file": "/etc/foreman/certs/ca.crt",
        },
    }}


def test_every_foreman_pod_loads_katello(enabled):
    workloads = {doc["metadata"]["name"]: doc for doc in foreman_workloads(enabled)}
    assert set(workloads) == {
        FULLNAME, f"{FULLNAME}-migrate-1", JOB,
        *(f"{FULLNAME}-dynflow-{name}" for name in ("orchestrator", "worker", "worker-hosts-queue")),
        *(f"{FULLNAME}-cron-{period}" for period in ("hourly", "daily", "weekly", "monthly")),
    }
    for name, workload in workloads.items():
        pod = pod_spec(workload)
        (init,) = pod["initContainers"]
        assert init["name"] == "katello-config"
        assert init["command"][:3] == ["ruby", "-ryaml", "-e"]
        assert env(init)["KATELLO_CANDLEPIN_OAUTH_SECRET"]["valueFrom"]["secretKeyRef"] == {
            "name": "candlepin-runtime", "key": "OAUTH_SECRET"}
        (main,) = pod["containers"]
        assert env(main)["FOREMAN_ENABLED_PLUGINS"]["value"].split() == [
            "foreman-tasks", "foreman_remote_execution", "katello"], name
        assert "KATELLO_CANDLEPIN_OAUTH_SECRET" not in env(main), name
        (mount,) = [m for m in main["volumeMounts"] if m["mountPath"] == KATELLO_YAML]
        assert mount == {"name": "katello-config", "mountPath": KATELLO_YAML,
                         "subPath": "katello.yaml", "readOnly": True}, name
        (volume,) = [v for v in pod["volumes"] if v["name"] == "katello-config"]
        assert volume["emptyDir"]["medium"] == "Memory", name
        config_map = next(v for v in pod["volumes"] if v["name"] == "configuration")
        assert "katello.yaml" in enabled["ConfigMap", config_map["configMap"]["name"]]["data"]


def test_oauth_secret_key_is_configurable(render):
    manifests = render(values(candlepin={"existingSecret": "cp", "oauthSecretSecretKey": "oauth"}))
    (init,) = pod_spec(manifests["Deployment", FULLNAME])["initContainers"]
    assert env(init)["KATELLO_CANDLEPIN_OAUTH_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": "cp", "key": "oauth"}


def test_web_removes_stale_event_daemon_pid_file(enabled):
    (container,) = pod_spec(enabled["Deployment", FULLNAME])["containers"]
    assert container["command"][:2] == ["/bin/bash", "-c"]
    script = container["command"][2]
    assert script == (
        "rm -f /usr/share/foreman/tmp/pids/katello_event_daemon.pid &&"
        " exec /usr/share/foreman/bin/rails server --environment production --pid /tmp/rails.pid")


def test_pulp_smart_proxy_job_runs_after_install_and_upgrade(enabled):
    job = enabled["Job", JOB]
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "post-install,post-upgrade"
    assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation,hook-succeeded"
    pod = pod_spec(job)
    (container,) = pod["containers"]
    assert container["command"][:2] == ["bin/rails", "runner"]
    assert 'features.include?("Pulpcore")' in container["command"][2]
    assert env(container)["PULP_SMART_PROXY_NAME"]["value"] == "foreman.test-pulp"
    assert env(container)["PULP_SMART_PROXY_URL"]["value"] == (
        "https://foreman-foreman-httpd.foreman.svc/pulp/api/v3/smart_proxy")
    volumes = {v["name"]: v for v in pod["volumes"]}
    # The web configuration and client certificate: Foreman fetches the
    # features through httpd, which maps the certificate to the Pulp admin.
    assert volumes["configuration"]["configMap"]["name"] == f"{FULLNAME}-config"
    assert "projected" in volumes["certificates"]
    assert any(m["mountPath"] == "/etc/foreman/certs" for m in container["volumeMounts"])
    labels = job["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/component"] == "pulp-smart-proxy"


def test_pulp_smart_proxy_name_and_url_are_configurable(render):
    manifests = render(values(katello={"enabled": True, "pulpSmartProxy": {
        "name": "pulp", "url": "https://foreman.test/pulp/api/v3/smart_proxy"}}))
    (container,) = pod_spec(manifests["Job", JOB])["containers"]
    assert env(container)["PULP_SMART_PROXY_NAME"]["value"] == "pulp"
    assert env(container)["PULP_SMART_PROXY_URL"]["value"] == "https://foreman.test/pulp/api/v3/smart_proxy"
