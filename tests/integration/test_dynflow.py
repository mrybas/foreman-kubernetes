"""Exercise Dynflow and the foreman-tasks plugin in the installed chart."""

import json
import time
import warnings
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

# Fixtures shared with the web tests.
from test_foreman import ADMIN_AUTH, FQDN, api_url, namespace, release, workload  # noqa: F401

# Straight to the web Service, as httpd would send it: Foreman requires HTTPS
# when the front door is enabled.
HEADERS = {
    "Host": FQDN,
    "X-Forwarded-Proto": "https",
    "Authorization": ADMIN_AUTH,
    "Content-Type": "application/json",
}
PROCESSES = ("orchestrator", "worker", "worker-hosts-queue")


def api(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, method=method, headers=HEADERS)
    with urlopen(request, timeout=30) as response:
        return response.status, json.load(response)


@pytest.mark.parametrize("process", PROCESSES)
def test_dynflow_deployment_is_available(kube_cluster, namespace, workload,  # noqa: F811
                                         process):
    name = f"{workload}-dynflow-{process}"
    deployment = kube_cluster.kubectl(f"get deployment {name}", namespace=namespace)
    replicas = deployment["spec"]["replicas"]
    if process == "orchestrator":
        assert replicas == 1
    assert deployment["status"]["availableReplicas"] == replicas
    assert deployment["status"]["observedGeneration"] == deployment["metadata"]["generation"]
    # On a fresh database the three processes can race while scheduling the
    # first recurring tasks and one of them restarts once. Report, do not fail.
    pods = kube_cluster.kubectl(
        f"get pods --selector app.kubernetes.io/component=dynflow-{process}",
        namespace=namespace)
    assert pods
    restarts = sum(status["restartCount"] for pod in pods
                   for status in pod["status"].get("containerStatuses", []))
    if restarts:
        warnings.warn(f"{name}: {restarts} container restarts")


def test_foreman_tasks_plugin_is_loaded(api_url):  # noqa: F811
    status, payload = api(f"{api_url}/api/v2/statuses")
    assert status == 200
    plugins = [plugin["name"] for plugin in payload["results"]["foreman"]["plugins"]]
    assert "foreman-tasks" in plugins


def test_rails_cache_uses_valkey(api_url):  # noqa: F811
    # The ping lists cache servers only for the Redis cache store.
    status, payload = api(f"{api_url}/api/v2/ping")
    assert status == 200
    servers = payload["results"]["foreman"]["cache"]["servers"]
    assert servers and all(server["status"] == "ok" for server in servers)


def test_report_rendering_task_succeeds(api_url):  # noqa: F811
    """Scheduling a report runs TemplateRenderJob as a Dynflow task on a worker."""
    search = quote('name="User - Registered Users"')
    _, templates = api(f"{api_url}/api/v2/report_templates?search={search}")
    (template,) = templates["results"]
    tasks_url = f"{api_url}/foreman_tasks/api/tasks?per_page=100&search=" + quote(
        "label=TemplateRenderJob")
    _, before = api(tasks_url)
    known = {task["id"] for task in before["results"]}

    status, scheduled = api(
        f"{api_url}/api/v2/report_templates/{template['id']}/schedule_report",
        method="POST", body={"report_format": "json"})
    assert status == 200

    deadline = time.monotonic() + 180
    while True:
        _, after = api(tasks_url)
        new = [task for task in after["results"] if task["id"] not in known]
        if new and new[0]["state"] == "stopped":
            break
        assert time.monotonic() < deadline, f"task did not stop: {new}"
        time.sleep(2)
    (task,) = new
    assert task["result"] == "success", task

    status, report = api(f"{api_url}{scheduled['data_url']}")
    assert status == 200
    assert "admin" in [row["Login"] for row in report]
