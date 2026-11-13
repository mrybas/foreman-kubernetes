"""Measure recovery after a sudden worker-node loss (scenario F2).

Unlike test_drain.py, which drains a node gracefully (pods are evicted while
PodDisruptionBudgets hold), this kills the worker node's container outright
(`podman kill`, or `docker kill`), the way a power loss would. The kubelet
stops without evicting anything, so Kubernetes only notices after
node-monitor-grace-period (~40 s by default) and the httpd Service keeps a dead
web endpoint until then. That window, not the replica count, dominates recovery.

With two workers and the release's hard anti-affinity, the web pod on the killed
node cannot reschedule (no third node), so web runs degraded at one replica
until the node returns. The user-facing signal is therefore "requests succeed
again", not "the Deployment is fully available" -- so this measures the window
of failing web requests through httpd and does not wait for full availability.

Measure-first (scenario F2 of artifacts/ha-slo.md): the test records the real
recovery window to artifacts/disruption-report.json and the JUnit properties,
and asserts only a generous safety ceiling. The F2 SLO is set from the measured
numbers, not asserted here. It restarts the killed node afterwards so the
cluster is healthy for teardown.

The F2 SLO is 60 s, set from the first measurement (recovery window ~42 s on
default Kubernetes, bounded by node-monitor-grace-period; see artifacts/ha-slo.md).
The test asserts that bound.

Env knobs: NODE_LOSS_OBSERVE_SECONDS (how long to watch after the kill, default
150), NODE_LOSS_CEILING_SECONDS (the asserted recovery bound, default 60).
"""

import os
import subprocess
import time

import pytest

from test_drain import (
    HTTPD,
    RELEASE,
    SETTLE,
    WEB,
    Client,
    failed,
    get,
    kubectl,
    nodes_of,
    save_logs,
    summary,
    wait_available,
)

pytestmark = pytest.mark.disruption

OBSERVE = int(os.environ.get("NODE_LOSS_OBSERVE_SECONDS", "150"))
# The F2 SLO, set from the first measurement (~42 s). See artifacts/ha-slo.md.
CEILING = int(os.environ.get("NODE_LOSS_CEILING_SECONDS", "60"))


def container_runtime():
    for runtime in ("podman", "docker"):
        if subprocess.run([runtime, "version"], capture_output=True).returncode == 0:
            return runtime
    pytest.skip("no podman or docker available to kill a Kind node container")


def node_container(runtime, node, action):
    # Kind names the node container exactly as the Kubernetes node.
    return subprocess.run([runtime, action, node], capture_output=True, text=True, timeout=120)


def ensure_web_spread(workers):
    """web replicas across distinct workers. Earlier drain tests evict a web pod
    and it does not move back after the node is uncordoned, so both replicas can
    end up on one worker; a rollout restart re-spreads them while both workers
    are schedulable."""
    web_nodes = [name for name in nodes_of("web") if name in workers]
    if len(set(web_nodes)) >= 2:
        return web_nodes
    kubectl("rollout", "restart", f"deployment/{WEB}")
    kubectl("rollout", "status", f"deployment/{WEB}", "--timeout=600s", timeout=620)
    wait_available(WEB)
    return [name for name in nodes_of("web") if name in workers]


def test_sudden_node_loss_is_measured(workers, report, record_property):
    runtime = container_runtime()
    # Kill a worker that runs a web replica while another web replica stays on a
    # different worker, so the Service still has one live endpoint to recover to.
    web_nodes = ensure_web_spread(workers)
    if len(set(web_nodes)) < 2:
        pytest.skip(f"need web replicas on at least two workers, found {web_nodes}")
    victim = web_nodes[0]

    client = Client("node-loss-client", ["web"])
    client.start()
    events = {
        "killed_node": victim,
        "runtime": runtime,
        "observe_seconds": OBSERVE,
        "ceiling_seconds": CEILING,
    }
    report["node_loss"] = events
    rows = []
    restarted = False
    try:
        time.sleep(SETTLE)
        baseline = client.results()
        assert baseline, "the client logged no requests"
        assert not failed(baseline), f"web failed before the kill: {failed(baseline)[:10]}"

        start = time.monotonic()
        events["start"] = since = int(time.time())
        killed = node_container(runtime, victim, "kill")
        assert killed.returncode == 0, f"{runtime} kill {victim}: {killed.stderr}"

        # Watch while the client keeps probing; do not wait for full availability
        # (the degraded replica cannot reschedule onto an occupied node).
        time.sleep(OBSERVE)

        # Restore the node so the cluster is healthy for the next test / teardown.
        started = node_container(runtime, victim, "start")
        restarted = started.returncode == 0
        events["node_restarted"] = restarted
        if restarted:
            # Best effort: let the node rejoin and web return to full strength.
            try:
                wait_available(WEB, timeout=OBSERVE + 180)
                events["web_full_strength_seconds"] = round(time.monotonic() - start)
            except AssertionError as exc:
                events["web_full_strength_error"] = str(exc)
        time.sleep(SETTLE)
    finally:
        rows = client.stop()
        save_logs()
        if not restarted:
            node_container(runtime, victim, "start")

    result = summary(rows, "web", since)
    events["probes"] = {"web": result}
    window = None
    if result["last_failure"] is not None:
        # Recovery window: seconds from the kill to the last failed request.
        window = result["last_failure"]
    events["measured_recovery_window_seconds"] = window

    record_property("node_loss_web_requests", result["requests"])
    record_property("node_loss_web_failed", result["failed"])
    record_property("node_loss_web_first_failure", result["first_failure"])
    record_property("node_loss_web_last_failure", result["last_failure"])
    record_property("node_loss_web_recovery_window", window)

    # Assert the F2 SLO bound (CEILING) and prove recovery actually happened.
    assert result["requests"] > 100, result
    assert result["failed"] < result["requests"], f"web never recovered: {result}"
    if window is not None:
        assert window <= CEILING, (
            f"web still failing {window}s after node loss (ceiling {CEILING}s): {result}")
        # There must be at least one successful web request after the last failure.
        recovered = [row for row in rows
                     if row[1] == "web" and row[0] > since + window and row[2].startswith("2")]
        assert recovered, f"no successful web request after recovery window: {result}"
