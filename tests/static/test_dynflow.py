"""Static assertions on the Dynflow workloads of the foreman Helm chart."""

import pytest

from test_render import (
    FULLNAME, containers, pod_spec,
    test_containers_use_restricted_security_context as check_security_context,
    test_resources_have_standard_labels as check_labels,
    test_workload_containers_request_cpu_and_memory as check_requests,
)

ENABLED = {"dynflow": {"enabled": True}}
PREFIX = f"{FULLNAME}-dynflow-"
SIDEKIQ = ["sidekiq", "-e", "production", "-r", "./extras/dynflow-sidekiq.rb", "-C"]
PROBE = ["grep", "-qF", "busy]", "/proc/1/cmdline"]


def env(container):
    return {e["name"]: e for e in container["env"]}


def dynflow(manifests):
    return {doc["metadata"]["name"][len(PREFIX):]: doc
            for doc in manifests.of_kind("Deployment")
            if doc["metadata"]["name"].startswith(PREFIX)}


def plugins(manifest):
    (container,) = pod_spec(manifest)["containers"]
    return env(container)["FOREMAN_ENABLED_PLUGINS"]["value"].split()


def test_disabled_by_default(manifests):
    assert not dynflow(manifests)
    for workload in manifests.of_kind("Deployment", "Job"):
        for container in containers(workload):
            names = env(container).keys()
            assert "foreman-tasks" not in plugins(workload)
            assert not any("REDIS" in name for name in names), container["name"]


@pytest.fixture
def enabled(render):
    return render(ENABLED)


def test_enabled_renders_three_deployments(enabled):
    deployments = dynflow(enabled)
    assert deployments.keys() == {"orchestrator", "worker", "worker-hosts-queue"}
    configs = {}
    for name, deployment in deployments.items():
        (container,) = pod_spec(deployment)["containers"]
        assert container["command"][:-1] == SIDEKIQ, name
        configs[name] = container["command"][-1]
        assert container["image"] == "quay.io/foreman/foreman:5.0"
        assert container["workingDir"] == "/usr/share/foreman"
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            assert container[probe]["exec"]["command"] == PROBE, (name, probe)
        assert container["resources"]["requests"]["memory"] == "512Mi"
        assert pod_spec(deployment)["terminationGracePeriodSeconds"] >= 30
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        assert labels["app.kubernetes.io/component"] == f"dynflow-{name}"
        assert deployment["spec"]["selector"]["matchLabels"].items() <= labels.items()
    assert configs == {
        "orchestrator": "/etc/foreman/dynflow/orchestrator.yml",
        "worker": "/etc/foreman/dynflow/worker.yml",
        "worker-hosts-queue": "/etc/foreman/dynflow/worker.yml",
    }


def test_enabled_workloads_are_restricted_and_labelled(enabled):
    check_security_context(enabled)
    check_requests(enabled)
    check_labels(enabled)


def test_queues_and_concurrency_come_from_values(render):
    manifests = render({"dynflow": {
        "enabled": True,
        "worker": {"replicas": 3, "concurrency": 7, "queues": ["default", "custom@2"]},
        "hostsQueue": {"replicas": 2, "concurrency": 4},
    }})
    expected = {
        "orchestrator": (1, None, "1", "6"),
        "worker": (3, "default,custom@2", "7", "12"),
        "worker-hosts-queue": (2, "hosts_queue", "4", "9"),
    }
    for name, deployment in dynflow(manifests).items():
        (container,) = pod_spec(deployment)["containers"]
        variables = env(container)
        queues = variables.get("FOREMAN_DYNFLOW_QUEUES", {}).get("value")
        replicas, *rest = expected[name]
        assert deployment["spec"]["replicas"] == replicas, name
        assert [queues, variables["FOREMAN_DYNFLOW_CONCURRENCY"]["value"],
                variables["FOREMAN_DATABASE_POOL"]["value"]] == rest, name


def test_orchestrator_is_a_single_replica_recreated(render):
    manifests = render({"dynflow": {"enabled": True, "orchestrator": {"replicas": 3}}})
    orchestrator = dynflow(manifests)["orchestrator"]
    assert orchestrator["spec"]["replicas"] == 1
    assert orchestrator["spec"]["strategy"] == {"type": "Recreate"}
    assert "strategy" not in dynflow(manifests)["worker"]["spec"]


def test_valkey_url_comes_from_existing_secret(render):
    manifests = render({"dynflow": {"enabled": True},
                        "foreman": {"existingSecret": "custom", "redisUrlSecretKey": "VALKEY"}})
    for workload in manifests.of_kind("Deployment", "Job"):
        (container,) = pod_spec(workload)["containers"]
        variables = env(container)
        where = workload["metadata"]["name"]
        assert variables["FOREMAN_REDIS_URL"]["valueFrom"]["secretKeyRef"] == {
            "name": "custom", "key": "VALKEY"}, where
        assert variables["FOREMAN_RAILS_CACHE_STORE_URLS"]["value"] == "$(FOREMAN_REDIS_URL)/4"
        assert "foreman-tasks" in plugins(workload), where
        # Kubernetes expands $(VAR) only for variables defined earlier.
        names = [e["name"] for e in container["env"]]
        if where.startswith(PREFIX):
            assert variables["DYNFLOW_REDIS_URL"]["value"] == "$(FOREMAN_REDIS_URL)/6"
            assert variables["REDIS_PROVIDER"]["value"] == "DYNFLOW_REDIS_URL"
            assert names.index("FOREMAN_REDIS_URL") < names.index("DYNFLOW_REDIS_URL")
        else:
            assert "DYNFLOW_REDIS_URL" not in variables, where


def test_web_service_selects_only_web_pods(enabled):
    selector = enabled["Service", FULLNAME]["spec"]["selector"]
    for name, deployment in dynflow(enabled).items():
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        assert not selector.items() <= labels.items(), name


def test_certificates_mounted_like_web_pod(render):
    manifests = render({"dynflow": {"enabled": True}, "certificates": {"enabled": True}})
    web = pod_spec(manifests["Deployment", FULLNAME])
    (web_container,) = web["containers"]
    for name, deployment in dynflow(manifests).items():
        spec = pod_spec(deployment)
        (container,) = spec["containers"]
        assert container["volumeMounts"] == web_container["volumeMounts"], name
        assert spec["volumes"] == web["volumes"], name


@pytest.mark.parametrize("values, release", [
    ({}, "r" * 53),
    ({"fullnameOverride": "f" * 63}, "foreman"),
])
def test_names_fit_and_stay_distinct(render, values, release):
    manifests = render({**ENABLED, **values}, release=release)
    names = [doc["metadata"]["name"] for doc in manifests.of_kind("Deployment")]
    assert len(names) == 4, names
    assert all(len(name) <= 63 for name in names), names
    assert sorted(name.rsplit("-dynflow-", 1)[-1] for name in names if "-dynflow-" in name) == [
        "orchestrator", "worker", "worker-hosts-queue"]
