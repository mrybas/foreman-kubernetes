"""Drain a worker node while clients request content through a Smart Proxy
with content (the foreman-proxy chart).

make test-disruption installs the Smart Proxy with two httpd replicas
(DISRUPTION_CAPSULE_HELM_ARGS) after the foreman release. Its Pulp pods share
a node-local volume, so the test drains the worker that runs an httpd pod but
not Pulp. A client pod on the control-plane node requests, through the Smart
Proxy's Service, the Pulp content app, the Pulp status and the server CA under
/pub, about four times a second each. Every request must succeed during the
drain and after the node is uncordoned. Runs after test_drain.py, whose drain
can leave both httpd pods on one node; then the httpd Deployment is restarted
first, so that its pods spread again.
"""

import os
import time

import pytest

from test_drain import SETTLE, Client, get, kubectl, summary, wait_available

pytestmark = pytest.mark.disruption

CAPSULE_NAMESPACE = os.environ.get("CAPSULE_NAMESPACE", "capsule")
CAPSULE_RELEASE = os.environ.get("CAPSULE_RELEASE", "capsule")
CAPSULE_NAME = os.environ.get("CAPSULE_NAME", "capsule.test")
FULLNAME = f"{CAPSULE_RELEASE}-foreman-proxy"
HTTPD = f"{FULLNAME}-httpd"
PROBES = ["content", "pulp", "pub"]


def nodes_of(component):
    pods = get("pods", "--selector",
               f"app.kubernetes.io/instance={CAPSULE_RELEASE},app.kubernetes.io/component={component}",
               namespace=CAPSULE_NAMESPACE)
    return [pod["spec"].get("nodeName") for pod in pods["items"]
            if not pod["metadata"].get("deletionTimestamp")]


def wait_all_available():
    for deployment in get("deployments", "--selector", f"app.kubernetes.io/instance={CAPSULE_RELEASE}",
                          namespace=CAPSULE_NAMESPACE)["items"]:
        wait_available(deployment["metadata"]["name"], namespace=CAPSULE_NAMESPACE)


@pytest.fixture(scope="module")
def installed():
    if kubectl("get", "deployment", HTTPD, namespace=CAPSULE_NAMESPACE, check=False).returncode != 0:
        pytest.skip(f"the foreman-proxy release {CAPSULE_RELEASE} is not installed in {CAPSULE_NAMESPACE}")


def test_capsule_httpd_is_spread_with_a_budget(installed):
    wait_all_available()
    if len(set(nodes_of("httpd"))) < 2:
        kubectl("rollout", "restart", f"deployment/{HTTPD}", namespace=CAPSULE_NAMESPACE)
        kubectl("rollout", "status", f"deployment/{HTTPD}", "--timeout=300s",
                namespace=CAPSULE_NAMESPACE, timeout=320)
    deployment = get("deployment", HTTPD, namespace=CAPSULE_NAMESPACE)
    assert deployment["spec"]["replicas"] == 2
    nodes = nodes_of("httpd")
    assert len(set(nodes)) == len(nodes) == 2, nodes
    budget = get("poddisruptionbudget", HTTPD, namespace=CAPSULE_NAMESPACE)
    assert budget["spec"]["maxUnavailable"] == 1
    assert budget["status"]["disruptionsAllowed"] == 1, budget["status"]


def test_drain_keeps_capsule_content_available(installed, workers, report, record_property):
    pulp_nodes = set(nodes_of("pulp-api") + nodes_of("pulp-content"))
    candidates = [node for node in nodes_of("httpd") if node in workers and node not in pulp_nodes]
    assert candidates, f"every worker with a Smart Proxy httpd pod also runs its Pulp: {pulp_nodes}"
    node = candidates[0]
    client = Client("disruption-capsule-client", PROBES, fqdn=CAPSULE_NAME,
                    service=(CAPSULE_NAMESPACE, f"{FULLNAME}"))
    client.start()
    events = {"drained_node": node, "pulp_nodes": sorted(pulp_nodes)}
    report["capsule_drain"] = events
    rows = []
    try:
        time.sleep(SETTLE)
        baseline = client.results()
        assert baseline, "the client logged no requests"
        bad = [row for row in baseline if not row[2].startswith("2")]
        assert not bad, f"requests failed before the drain: {bad[:10]}"

        start = time.monotonic()
        events["start"] = since = int(time.time())
        try:
            drain = kubectl("drain", node, "--ignore-daemonsets", "--delete-emptydir-data",
                            "--timeout=900s", namespace=None, check=False, timeout=960)
            events["drain_seconds"] = round(time.monotonic() - start)
            events["drain_output"] = drain.stdout.splitlines() + drain.stderr.splitlines()
            assert drain.returncode == 0, drain.stderr
            wait_available(HTTPD, namespace=CAPSULE_NAMESPACE)
            events["httpd_restored_seconds"] = round(time.monotonic() - start)
            events["httpd_nodes_after_drain"] = nodes_of("httpd")
            assert node not in events["httpd_nodes_after_drain"]
            time.sleep(SETTLE)
        finally:
            kubectl("uncordon", node, namespace=None, check=False)
            events["uncordon_seconds"] = round(time.monotonic() - start)
        wait_all_available()
        events["all_restored_seconds"] = round(time.monotonic() - start)
        time.sleep(SETTLE)
    finally:
        rows = client.stop()

    events["probes"] = {probe: summary(rows, probe, since) for probe in PROBES}
    for probe in PROBES:
        record_property(f"capsule_drain_{probe}_requests", events["probes"][probe]["requests"])
        record_property(f"capsule_drain_{probe}_failed", events["probes"][probe]["failed"])
    for probe in PROBES:
        result = events["probes"][probe]
        assert result["requests"] > 100, (probe, result)
        assert result["failed"] == 0, (probe, result)
