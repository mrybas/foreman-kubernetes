"""Exercise the installed chart through Kubernetes and the Foreman API."""

import json
import os
import selectors
import subprocess
from urllib.request import Request, urlopen

import pytest


@pytest.fixture(scope="module")
def namespace():
    return os.environ.get("KIND_NAMESPACE", "foreman")


@pytest.fixture(scope="module")
def workload():
    return f"{os.environ.get('HELM_RELEASE', 'foreman')}-foreman"


@pytest.fixture(scope="module")
def api_url(kube_cluster, namespace, workload):
    # Bind an available loopback port; keep kubectl alive for the HTTP tests.
    with subprocess.Popen(
        ["kubectl", "--kubeconfig", kube_cluster.kube_config_path,
         "--namespace", namespace, "port-forward", "--address=127.0.0.1",
         f"service/{workload}", ":3000"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=30), "kubectl port-forward did not start"
                line = process.stdout.readline()
            assert line.startswith("Forwarding from 127.0.0.1:"), line
            port = line.split(" -> ")[0].rsplit(":", 1)[1]
            yield f"http://127.0.0.1:{port}"
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def test_web_deployment_is_available(kube_cluster, namespace, workload):
    deployment = kube_cluster.kubectl(f"get deployment {workload}", namespace=namespace)
    assert deployment["spec"]["replicas"] == 1
    assert deployment["status"]["availableReplicas"] == 1
    assert deployment["status"]["observedGeneration"] == deployment["metadata"]["generation"]


def test_database_is_active(api_url):
    request = Request(f"{api_url}/api/v2/ping", headers={"Host": "foreman.test"})
    with urlopen(request, timeout=30) as response:
        assert response.status == 200
        payload = json.load(response)
    assert payload["results"]["foreman"]["database"]["active"] is True


def test_seeded_admin_can_use_api(api_url):
    request = Request(
        f"{api_url}/api/v2/users?search=login%3Dadmin",
        headers={"Host": "foreman.test", "Authorization": "Basic YWRtaW46Zm9yZW1hbi10ZXN0"},
    )
    with urlopen(request, timeout=30) as response:
        assert response.status == 200
        payload = json.load(response)
    assert [user["login"] for user in payload["results"]] == ["admin"]
