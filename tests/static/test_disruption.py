"""Static assertions on replicas, disruption budgets, spreading and restricted
Pod Security in the rendered foreman Helm chart."""

import pytest

from test_checksums import EVERYTHING, merged
from test_render import containers, pod_spec

F = "foreman-foreman"
# Deployment name -> (component, values that set its replicas to 2).
REPLICATED = {
    F: ("web", {"replicaCount": 2}),
    f"{F}-httpd": ("httpd", {"httpd": {"replicas": 2}}),
    f"{F}-dynflow-worker": ("dynflow-worker", {"dynflow": {"worker": {"replicas": 2}}}),
    f"{F}-dynflow-worker-hosts-queue": ("dynflow-worker-hosts-queue",
                                        {"dynflow": {"hostsQueue": {"replicas": 2}}}),
}
PULP = {f"{F}-pulp-{name}": (f"pulp-{name}", {"pulp": {name: {"replicas": 2}}})
        for name in ("api", "content", "worker")}
SINGLE = (f"{F}-dynflow-orchestrator", f"{F}-candlepin", f"{F}-proxy")
TWO = merged(EVERYTHING, {"replicaCount": 2, "httpd": {"replicas": 2},
                          "dynflow": {"worker": {"replicas": 2}, "hostsQueue": {"replicas": 2}},
                          "pulp": {"api": {"replicas": 2}, "content": {"replicas": 2},
                                   "worker": {"replicas": 2}}})


def budgets(manifests):
    return {doc["metadata"]["name"]: doc for doc in manifests.of_kind("PodDisruptionBudget")}


def spread(deployment):
    return pod_spec(deployment).get("topologySpreadConstraints")


def pod_labels(workload):
    if workload["kind"] == "Pod":
        return workload["metadata"]["labels"]
    return workload["spec"]["template"]["metadata"]["labels"]


def test_single_replicas_render_no_budget_or_spread(render):
    manifests = render(EVERYTHING)
    assert not budgets(manifests)
    for deployment in manifests.of_kind("Deployment"):
        assert deployment["spec"]["replicas"] == 1
        assert spread(deployment) is None, deployment["metadata"]["name"]


@pytest.mark.parametrize("name", REPLICATED)
def test_two_replicas_get_a_budget_and_a_soft_spread(render, name):
    component, override = REPLICATED[name]
    manifests = render(merged(EVERYTHING, override))
    deployment = manifests["Deployment", name]
    assert deployment["spec"]["replicas"] == 2
    assert set(budgets(manifests)) == {name}
    budget = budgets(manifests)[name]
    assert budget["spec"]["maxUnavailable"] == 1
    assert budget["spec"]["unhealthyPodEvictionPolicy"] == "AlwaysAllow"
    assert budget["metadata"]["labels"]["app.kubernetes.io/component"] == component
    assert budget["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/name": "foreman", "app.kubernetes.io/instance": "foreman",
        "app.kubernetes.io/component": component}
    (constraint,) = spread(deployment)
    assert constraint == {
        "maxSkew": 1,
        "topologyKey": "kubernetes.io/hostname",
        "whenUnsatisfiable": "ScheduleAnyway",
        "labelSelector": {"matchLabels": budget["spec"]["selector"]["matchLabels"]},
        "matchLabelKeys": ["pod-template-hash"],
    }
    for other in manifests.of_kind("Deployment"):
        if other["metadata"]["name"] != name:
            assert spread(other) is None, other["metadata"]["name"]


def test_each_budget_selects_exactly_its_deployment(render):
    manifests = render(merged(TWO, {"pulp": {"storage": {"colocate": False}}}))
    found = budgets(manifests)
    assert set(found) == set(REPLICATED) | set(PULP)
    for name, budget in found.items():
        selector = budget["spec"]["selector"]["matchLabels"].items()
        matching = [doc["metadata"]["name"] for doc in manifests.of_kind("Deployment", "Job", "Pod")
                    if selector <= pod_labels(doc).items()]
        assert matching == [name], (name, matching)


def test_colocated_pulp_gets_no_budget_or_spread(render):
    manifests = render(TWO)
    assert set(budgets(manifests)) == set(REPLICATED)
    for name in PULP:
        deployment = manifests["Deployment", name]
        assert deployment["spec"]["replicas"] == 2
        assert spread(deployment) is None
        assert pod_spec(deployment)["affinity"]["podAffinity"]


def test_pulp_with_shared_storage_is_spread(render):
    manifests = render(merged(TWO, {"pulp": {"storage": {"colocate": False}}}))
    for name, (component, _) in PULP.items():
        deployment = manifests["Deployment", name]
        assert "affinity" not in pod_spec(deployment)
        (constraint,) = spread(deployment)
        assert constraint["labelSelector"]["matchLabels"]["app.kubernetes.io/component"] == component


def test_pulp_with_s3_storage_gets_a_budget_and_a_spread(render):
    """S3 storage drops the pod affinity, so the default colocate does not apply."""
    manifests = render(merged(TWO, {"pulp": {"storage": {"type": "s3", "s3": {"bucket": "pulp"}}}}))
    assert set(budgets(manifests)) == set(REPLICATED) | set(PULP)
    for name, (component, _) in PULP.items():
        deployment = manifests["Deployment", name]
        assert "affinity" not in pod_spec(deployment)
        (constraint,) = spread(deployment)
        assert constraint["labelSelector"]["matchLabels"]["app.kubernetes.io/component"] == component
        assert budgets(manifests)[name]["spec"]["maxUnavailable"] == 1


def test_budgets_and_spread_can_be_disabled(render):
    manifests = render(merged(TWO, {"podDisruptionBudget": {"enabled": False},
                                    "topologySpread": {"enabled": False}}))
    assert not budgets(manifests)
    assert all(spread(doc) is None for doc in manifests.of_kind("Deployment"))


def test_spread_settings_are_values(render):
    manifests = render(merged(TWO, {"topologySpread": {
        "topologyKey": "topology.kubernetes.io/zone", "maxSkew": 2,
        "whenUnsatisfiable": "DoNotSchedule"}}))
    (constraint,) = spread(manifests["Deployment", F])
    assert constraint["topologyKey"] == "topology.kubernetes.io/zone"
    assert constraint["maxSkew"] == 2
    assert constraint["whenUnsatisfiable"] == "DoNotSchedule"


def test_single_replica_components_ignore_replica_values(render):
    manifests = render(merged(TWO, {
        "dynflow": {"orchestrator": {"replicas": 3}},
        "candlepin": {"replicas": 3},
        "proxy": {"replicas": 3},
    }))
    for name in SINGLE:
        deployment = manifests["Deployment", name]
        assert deployment["spec"]["replicas"] == 1, name
        assert deployment["spec"]["strategy"] == {"type": "Recreate"}, name
        assert spread(deployment) is None, name
        assert name not in budgets(manifests)


def test_web_and_httpd_wait_before_stopping(render):
    manifests = render(TWO)
    web = manifests["Deployment", F]["spec"]["template"]["spec"]["containers"][0]
    assert web["lifecycle"]["preStop"]["exec"]["command"] == ["sleep", "10"]
    httpd_pod = manifests["Deployment", f"{F}-httpd"]["spec"]["template"]["spec"]
    command = httpd_pod["containers"][0]["lifecycle"]["preStop"]["exec"]["command"]
    assert command[:2] == ["/bin/sh", "-c"]
    assert command[2].startswith("sleep 10;")
    assert "-k graceful-stop" in command[2]
    assert httpd_pod["terminationGracePeriodSeconds"] > 10


# Volume types the restricted Pod Security profile allows.
RESTRICTED_VOLUMES = {"configMap", "csi", "downwardAPI", "emptyDir", "ephemeral",
                      "persistentVolumeClaim", "projected", "secret"}


def assert_restricted(workloads):
    """The checks of the Kubernetes restricted Pod Security profile, for every
    pod template, with its init containers."""
    assert workloads
    for workload in workloads:
        spec = pod_spec(workload)
        pod = spec.get("securityContext", {})
        name = f"{workload['kind']}/{workload['metadata']['name']}"
        for key in ("hostNetwork", "hostPID", "hostIPC"):
            assert not spec.get(key), (name, key)
        for volume in spec.get("volumes", []):
            assert set(volume) - {"name"} <= RESTRICTED_VOLUMES, (name, volume)
        assert pod.get("runAsUser", 1) != 0, name
        assert "sysctls" not in pod, name
        for container in containers(workload):
            where = f"{name}/{container['name']}"
            context = container.get("securityContext", {})
            assert context.get("privileged", False) is False, where
            assert context.get("allowPrivilegeEscalation") is False, where
            assert context.get("runAsNonRoot", pod.get("runAsNonRoot")) is True, where
            assert context.get("runAsUser", pod.get("runAsUser", 1)) != 0, where
            assert context.get("capabilities", {}).get("drop") == ["ALL"], where
            assert set(context.get("capabilities", {}).get("add", [])) <= {"NET_BIND_SERVICE"}, where
            seccomp = context.get("seccompProfile", pod.get("seccompProfile", {}))
            assert seccomp.get("type") in ("RuntimeDefault", "Localhost"), where
            assert "procMount" not in context, where
            assert not any(port.get("hostPort") for port in container.get("ports", [])), where


@pytest.mark.parametrize("values", [{}, TWO], ids=["default", "everything"])
def test_every_pod_meets_the_restricted_profile(render, values):
    """Deployments, hook Jobs and the helm test Pod of the chart."""
    assert_restricted(render(values).of_kind("Deployment", "Job", "Pod"))


def test_web_database_pool_has_room_beside_puma_threads(render):
    """Each Puma thread needs a connection, and so do threads beside them such
    as the Katello event daemon; foremanctl uses threads + 4."""
    manifests = render(merged(EVERYTHING, {"foreman": {"puma": {"threadsMax": 7}}}))
    for name in (F, f"{F}-migrate-1"):
        container = pod_spec(manifests["Deployment" if name == F else "Job", name])["containers"][0]
        variables = {item["name"]: item.get("value") for item in container["env"]}
        assert variables["FOREMAN_DATABASE_POOL"] == "11", name


def test_web_readiness_does_not_wait_for_other_components(render):
    """With Katello the API ping waits for Candlepin and Pulp; the readiness
    probe must not, or their restart removes every web pod from the Service."""
    manifests = render(EVERYTHING)
    container = pod_spec(manifests["Deployment", F])["containers"][0]
    assert container["readinessProbe"]["httpGet"]["path"] == "/users/login"
