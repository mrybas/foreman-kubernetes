"""Drain a worker node while clients send requests through httpd.

Needs a Kind cluster with two workers (make setup-kind
KIND_CONFIG=tests/kind/cluster-disruption.yaml) and a release with at least two
Foreman web and two httpd replicas (make test-disruption installs one).

Drain: a client pod on the control-plane node requests /api/v2/status as admin
through the httpd Service in a loop while `kubectl drain` evicts the pods of
the worker that runs Pulp, respecting the PodDisruptionBudgets. Every request
must succeed, also after the node is uncordoned. Without Katello the client
also requests /api/v2/ping, and every ping must succeed. With Katello it
requests the Pulp status through httpd instead; Pulp has its pods on one node,
so its interruption is measured, not asserted.

Candlepin restart (Katello only): a second client requests /api/v2/ping and
/rhsm/status, which wait for Candlepin, while the Candlepin pod is deleted.
Nothing is asserted; the failures of all probes in this phase, including
/api/v2/status, are measured.

Results go to artifacts/disruption-report.json (see conftest.py) and the JUnit
properties.
"""

import json
import os
import subprocess
import time
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.disruption

NAMESPACE = os.environ.get("KIND_NAMESPACE", "foreman")
RELEASE = os.environ.get("HELM_RELEASE", "foreman")
FQDN = os.environ.get("FOREMAN_FQDN", "foreman.test")
ADMIN_PASSWORD = os.environ.get("FOREMAN_ADMIN_PASSWORD", "foreman-test")
WEB = f"{RELEASE}-foreman"
HTTPD = f"{RELEASE}-foreman-httpd"
CANDLEPIN = f"{RELEASE}-foreman-candlepin"
CLIENT_IMAGE = os.environ.get("DISRUPTION_CLIENT_IMAGE", "curlimages/curl:8.17.0")
ARTIFACTS = Path(__file__).resolve().parents[2] / "artifacts"
# Seconds of requests before and after each disruption.
SETTLE = 20

# Every probe named in PROBES requests its path through httpd every quarter
# second until /tmp/stop exists and prints
# "<epoch seconds> <probe> <HTTP code> <seconds>". curl prints code 000 when it
# gets no HTTP response within 10 seconds.
CLIENT_SCRIPT = r"""
probe() {
  while [ ! -e /tmp/stop ]; do
    out=$(curl --silent --insecure --output /dev/null --max-time 10 \
      --write-out '%{http_code} %{time_total}' \
      --resolve "${FQDN}:443:${HTTPD_IP}" "https://${FQDN}$2" ${3:+--user "$3"}) || true
    echo "$(date +%s) $1 ${out:-000 0}"
    sleep 0.25
  done
}
for name in ${PROBES}; do
  case "${name}" in
    web) probe web /api/v2/status "admin:${ADMIN_PASSWORD}" & ;;
    ping) probe ping /api/v2/ping & ;;
    pulp) probe pulp /pulp/api/v3/status/ & ;;
    candlepin) probe candlepin /rhsm/status & ;;
    content) probe content /pulp/content/ & ;;
    pub) probe pub /pub/katello-server-ca.crt & ;;
  esac
done
wait
echo finished
"""


def kubectl(*args, namespace=NAMESPACE, check=True, timeout=120, stdin=None):
    command = ["kubectl"]
    if namespace:
        command += ["--namespace", namespace]
    result = subprocess.run(command + list(args), capture_output=True, text=True,
                            input=stdin, timeout=timeout, check=False)
    if check:
        assert result.returncode == 0, f"kubectl {' '.join(args)}: {result.stderr}"
    return result


def get(*args, namespace=NAMESPACE):
    return json.loads(kubectl("get", *args, "--output", "json", namespace=namespace).stdout)


def nodes_of(component):
    pods = get("pods", "--selector",
               f"app.kubernetes.io/instance={RELEASE},app.kubernetes.io/component={component}")
    return [pod["spec"].get("nodeName") for pod in pods["items"]
            if not pod["metadata"].get("deletionTimestamp")]


def wait_available(name, timeout=900, namespace=NAMESPACE):
    deadline = time.monotonic() + timeout
    while True:
        deployment = get("deployment", name, namespace=namespace)
        wanted = deployment["spec"]["replicas"]
        status = deployment["status"]
        if (status.get("availableReplicas") == wanted and status.get("updatedReplicas") == wanted
                and status.get("observedGeneration") == deployment["metadata"]["generation"]):
            return
        assert time.monotonic() < deadline, f"{name} not available: {status}"
        time.sleep(5)


class Client:
    """A pod on the control-plane node, which is never drained, that runs
    the probes against an httpd Service, by default the release's."""

    def __init__(self, name, probes, fqdn=FQDN, service=(NAMESPACE, HTTPD)):
        self.name = name
        self.probes = probes
        self.fqdn = fqdn
        self.service = service

    def start(self):
        httpd_ip = get("service", self.service[1], namespace=self.service[0])["spec"]["clusterIP"]
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": self.name, "labels": {"app.kubernetes.io/name": self.name}},
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "nodeSelector": {"node-role.kubernetes.io/control-plane": ""},
                "tolerations": [{"key": "node-role.kubernetes.io/control-plane",
                                 "effect": "NoSchedule"}],
                "securityContext": {"runAsNonRoot": True, "runAsUser": 100,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{
                    "name": "client",
                    "image": CLIENT_IMAGE,
                    "command": ["/bin/sh", "-c", CLIENT_SCRIPT],
                    "env": [{"name": "FQDN", "value": self.fqdn},
                            {"name": "HTTPD_IP", "value": httpd_ip},
                            {"name": "PROBES", "value": " ".join(self.probes)},
                            {"name": "ADMIN_PASSWORD", "value": ADMIN_PASSWORD}],
                    "securityContext": {"allowPrivilegeEscalation": False,
                                        "capabilities": {"drop": ["ALL"]}},
                    "resources": {"requests": {"cpu": "50m", "memory": "32Mi"},
                                  "limits": {"memory": "128Mi"}},
                }],
            },
        }
        kubectl("delete", "pod", self.name, "--ignore-not-found", "--wait=true")
        kubectl("apply", "--filename=-", stdin=json.dumps(pod))
        kubectl("wait", "pod", self.name, "--for=condition=Ready", "--timeout=300s", timeout=320)

    def results(self):
        log = kubectl("logs", self.name, timeout=60).stdout
        rows = []
        for line in log.splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[0].isdigit():
                rows.append((int(parts[0]), parts[1], parts[2], float(parts[3])))
        return rows

    def stop(self):
        kubectl("exec", self.name, "--", "touch", "/tmp/stop", check=False)
        time.sleep(2)
        rows = self.results()
        kubectl("delete", "pod", self.name, "--ignore-not-found", "--wait=false", check=False)
        return rows


def failed(rows):
    return [row for row in rows if not row[2].startswith("2")]


def summary(rows, probe, since, until=None):
    """Counts of one probe's requests logged in [since, until), with failure
    times in seconds after since."""
    mine = [row for row in rows if row[1] == probe and row[0] >= since
            and (until is None or row[0] < until)]
    bad = failed(mine)
    return {
        "requests": len(mine),
        "failed": len(bad),
        "codes": dict(Counter(row[2] for row in mine)),
        "first_failure": bad[0][0] - since if bad else None,
        "last_failure": bad[-1][0] - since if bad else None,
        "max_seconds": max((row[3] for row in mine), default=None),
    }


def save_logs():
    """Logs of the web and httpd pods, next to the report."""
    directory = ARTIFACTS / "disruption-logs"
    directory.mkdir(parents=True, exist_ok=True)
    for component in ("web", "httpd"):
        pods = get("pods", "--selector",
                   f"app.kubernetes.io/instance={RELEASE},app.kubernetes.io/component={component}")
        for pod in pods["items"]:
            name = pod["metadata"]["name"]
            log = kubectl("logs", name, "--all-containers", check=False, timeout=120)
            (directory / f"{name}.log").write_text(log.stdout + log.stderr)


@pytest.fixture(scope="module")
def katello():
    return kubectl("get", "deployment", CANDLEPIN, check=False).returncode == 0


def test_replicas_are_spread_with_budgets(workers):
    for name, component in ((WEB, "web"), (HTTPD, "httpd")):
        deployment = get("deployment", name)
        assert deployment["spec"]["replicas"] >= 2, name
        wait_available(name)
        nodes = nodes_of(component)
        assert len(set(nodes)) == len(nodes) == deployment["spec"]["replicas"], (name, nodes)
        budget = get("poddisruptionbudget", name)
        assert budget["spec"]["maxUnavailable"] == 1
        assert budget["status"]["disruptionsAllowed"] == 1, budget["status"]


def test_drain_keeps_api_available(workers, katello, report, record_property):
    # The worker with Pulp when it runs, to measure its interruption, else
    # the worker of the first web pod.
    node = next(found[0] for found in (nodes_of("pulp-api"), nodes_of("web"))
                if found and found[0] in workers)
    probes = ["web", "pulp"] if katello else ["web", "ping"]
    client = Client("disruption-client", probes)
    client.start()
    events = {"drained_node": node, "katello": katello}
    report["drain"] = events
    rows = []
    try:
        time.sleep(SETTLE)
        baseline = client.results()
        assert baseline, "the client logged no requests"
        assert not failed(baseline), f"requests failed before the drain: {failed(baseline)[:10]}"

        start = time.monotonic()
        events["start"] = since = int(time.time())
        try:
            drain = kubectl("drain", node, "--ignore-daemonsets", "--delete-emptydir-data",
                            "--timeout=900s", namespace=None, check=False, timeout=960)
            events["drain_seconds"] = round(time.monotonic() - start)
            events["drain_output"] = drain.stdout.splitlines() + drain.stderr.splitlines()
            assert drain.returncode == 0, drain.stderr
            for name in (WEB, HTTPD):
                wait_available(name)
            events["web_and_httpd_restored_seconds"] = round(time.monotonic() - start)
            events["web_nodes_after_drain"] = nodes_of("web")
            events["httpd_nodes_after_drain"] = nodes_of("httpd")
            assert node not in events["web_nodes_after_drain"] + events["httpd_nodes_after_drain"]
            time.sleep(SETTLE)
        finally:
            kubectl("uncordon", node, namespace=None, check=False)
            events["uncordon_seconds"] = round(time.monotonic() - start)
        for deployment in get("deployments", "--selector",
                              f"app.kubernetes.io/instance={RELEASE}")["items"]:
            wait_available(deployment["metadata"]["name"])
        events["all_restored_seconds"] = round(time.monotonic() - start)
        time.sleep(SETTLE)
    finally:
        rows = client.stop()
        save_logs()

    events["probes"] = {probe: summary(rows, probe, since) for probe in probes}
    for probe in probes:
        record_property(f"drain_{probe}_requests", events["probes"][probe]["requests"])
        record_property(f"drain_{probe}_failed", events["probes"][probe]["failed"])
    for probe in ("web", "ping"):
        if probe in probes:
            result = events["probes"][probe]
            assert result["requests"] > 100, (probe, result)
            assert result["failed"] == 0, (probe, result)


def test_candlepin_restart_is_measured(katello, report, record_property):
    if not katello:
        pytest.skip("Candlepin is not enabled")
    probes = ["web", "ping", "candlepin"]
    client = Client("disruption-candlepin-client", probes)
    client.start()
    events = {}
    report["candlepin_restart"] = events
    try:
        time.sleep(SETTLE)
        events["start"] = since = int(time.time())
        start = time.monotonic()
        kubectl("delete", "pod", "--selector",
                f"app.kubernetes.io/instance={RELEASE},app.kubernetes.io/component=candlepin",
                timeout=180)
        wait_available(CANDLEPIN)
        events["candlepin_restored_seconds"] = round(time.monotonic() - start)
        time.sleep(SETTLE)
        for name in (WEB, HTTPD):
            wait_available(name)
    finally:
        rows = client.stop()
        save_logs()
    events["probes"] = {probe: summary(rows, probe, since) for probe in probes}
    for probe in probes:
        record_property(f"candlepin_{probe}_requests", events["probes"][probe]["requests"])
        record_property(f"candlepin_{probe}_failed", events["probes"][probe]["failed"])
