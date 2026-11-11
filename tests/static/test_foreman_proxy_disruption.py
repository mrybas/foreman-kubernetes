"""Static assertions on replicas, disruption budgets, spreading, graceful stop
and restricted Pod Security in the rendered foreman-proxy Helm chart."""

import pytest

from test_disruption import assert_restricted, budgets, pod_labels, spread
from test_foreman_proxy import FULLNAME, GATEWAY, PULP, REX, S3, deep_merge, render  # noqa: F401
from test_render import pod_spec

HTTPD = f"{FULLNAME}-httpd"
PULP_DEPLOYMENTS = {f"{PULP}-{name}": f"pulp-{name}" for name in ("api", "content", "worker")}
TWO = {"httpd": {"replicas": 2},
       "pulp": {"api": {"replicas": 2}, "content": {"replicas": 2}, "worker": {"replicas": 2}}}
SELECTOR = {"app.kubernetes.io/name": "foreman-proxy", "app.kubernetes.io/instance": "capsule"}


def test_single_replicas_render_no_budget_or_spread(render):  # noqa: F811
    manifests = render(deep_merge(REX, GATEWAY))
    assert not budgets(manifests)
    for deployment in manifests.of_kind("Deployment"):
        assert deployment["spec"]["replicas"] == 1
        assert spread(deployment) is None, deployment["metadata"]["name"]


def test_two_httpd_replicas_get_a_budget_and_a_soft_spread(render):  # noqa: F811
    manifests = render({"httpd": {"replicas": 2}})
    assert set(budgets(manifests)) == {HTTPD}
    budget = budgets(manifests)[HTTPD]
    assert budget["spec"]["maxUnavailable"] == 1
    assert budget["spec"]["unhealthyPodEvictionPolicy"] == "AlwaysAllow"
    assert budget["metadata"]["labels"]["app.kubernetes.io/component"] == "httpd"
    assert budget["spec"]["selector"]["matchLabels"] == {**SELECTOR, "app.kubernetes.io/component": "httpd"}
    (constraint,) = spread(manifests["Deployment", HTTPD])
    assert constraint == {
        "maxSkew": 1,
        "topologyKey": "kubernetes.io/hostname",
        "whenUnsatisfiable": "ScheduleAnyway",
        "labelSelector": {"matchLabels": budget["spec"]["selector"]["matchLabels"]},
        "matchLabelKeys": ["pod-template-hash"],
    }
    for other in manifests.of_kind("Deployment"):
        if other["metadata"]["name"] != HTTPD:
            assert spread(other) is None, other["metadata"]["name"]


def test_colocated_pulp_gets_no_budget_or_spread(render):  # noqa: F811
    manifests = render(TWO)
    assert set(budgets(manifests)) == {HTTPD}
    for name in PULP_DEPLOYMENTS:
        deployment = manifests["Deployment", name]
        assert deployment["spec"]["replicas"] == 2
        assert spread(deployment) is None
        assert pod_spec(deployment)["affinity"]["podAffinity"]


@pytest.mark.parametrize("storage", [{"colocate": False}, S3], ids=["shared-volume", "s3"])
def test_pulp_that_can_move_gets_a_budget_and_a_spread(render, storage):  # noqa: F811
    manifests = render(deep_merge(TWO, {"pulp": {"storage": storage}}))
    assert set(budgets(manifests)) == {HTTPD} | set(PULP_DEPLOYMENTS)
    for name, component in PULP_DEPLOYMENTS.items():
        deployment = manifests["Deployment", name]
        assert "affinity" not in pod_spec(deployment)
        (constraint,) = spread(deployment)
        assert constraint["labelSelector"]["matchLabels"]["app.kubernetes.io/component"] == component


def test_each_budget_selects_exactly_its_deployment(render):  # noqa: F811
    manifests = render(deep_merge(deep_merge(TWO, deep_merge(REX, GATEWAY)), {"pulp": {"storage": S3}}))
    found = budgets(manifests)
    assert set(found) == {HTTPD} | set(PULP_DEPLOYMENTS)
    for name, budget in found.items():
        selector = budget["spec"]["selector"]["matchLabels"].items()
        matching = [doc["metadata"]["name"] for doc in manifests.of_kind("Deployment", "Job")
                    if selector <= pod_labels(doc).items()]
        assert matching == [name], (name, matching)


def test_budgets_and_spread_can_be_disabled(render):  # noqa: F811
    manifests = render(deep_merge(TWO, {"podDisruptionBudget": {"enabled": False},
                                        "topologySpread": {"enabled": False},
                                        "pulp": {"storage": S3}}))
    assert not budgets(manifests)
    assert all(spread(doc) is None for doc in manifests.of_kind("Deployment"))


def test_foreman_proxy_stays_at_one_replica(render):  # noqa: F811
    """Dynflow keeps running jobs in memory; a replica value cannot change it."""
    manifests = render(deep_merge(TWO, {"proxy": {"replicas": 3}}))
    deployment = manifests["Deployment", f"{FULLNAME}-proxy"]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert spread(deployment) is None
    assert f"{FULLNAME}-proxy" not in budgets(manifests)


def test_httpd_waits_and_stops_gracefully(render):  # noqa: F811
    pod = pod_spec(render({"httpd": {"replicas": 2}})["Deployment", HTTPD])
    command = pod["containers"][0]["lifecycle"]["preStop"]["exec"]["command"]
    assert command[:2] == ["/bin/sh", "-c"]
    assert command[2].startswith("sleep 10;")
    assert "httpd -f /etc/httpd-foreman-proxy/httpd.conf -k graceful-stop" in command[2]
    assert pod["terminationGracePeriodSeconds"] == 60


@pytest.mark.parametrize("values", [
    {},
    deep_merge(TWO, deep_merge(REX, GATEWAY)),
    deep_merge(deep_merge(TWO, deep_merge(REX, GATEWAY)), {"pulp": {"storage": S3}}),
], ids=["default", "everything", "everything-s3"])
def test_every_pod_meets_the_restricted_profile(render, values):  # noqa: F811
    """Deployments and hook Jobs, with their init containers."""
    assert_restricted(render(values).of_kind("Deployment", "Job", "Pod"))
