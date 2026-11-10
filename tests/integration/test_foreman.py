"""Exercise the installed chart through Kubernetes and the Foreman API."""

import base64
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
def release():
    return os.environ.get("HELM_RELEASE", "foreman")


@pytest.fixture(scope="module")
def workload(release):
    return f"{release}-foreman"


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


def web_exec(kube_cluster, namespace, workload, *command):
    """Run a command in the web container as its own user (foreman)."""
    return subprocess.run(
        ["kubectl", "--kubeconfig", kube_cluster.kube_config_path, "--namespace", namespace,
         "exec", f"deployment/{workload}", "--container", "foreman", "--", *command],
        capture_output=True, text=True, check=False, timeout=60,
    )


def test_web_pod_mounts_ca_bundle(kube_cluster, namespace, workload, release):
    secret = kube_cluster.kubectl(f"get secret {release}-ca", namespace=namespace)
    expected = base64.b64decode(secret["data"]["ca.crt"]).decode()
    result = web_exec(kube_cluster, namespace, workload, "cat", "/etc/foreman/certs/ca.crt")
    assert result.returncode == 0, result.stderr
    assert "BEGIN CERTIFICATE" in result.stdout
    assert result.stdout == expected


def test_foreman_user_can_use_client_certificate(kube_cluster, namespace, workload):
    certs = "/etc/foreman/certs"
    result = web_exec(
        kube_cluster, namespace, workload, "sh", "-c",
        f"id -un && test -r {certs}/client.key"
        f" && openssl verify -CAfile {certs}/ca.crt {certs}/client.crt",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["foreman", f"{certs}/client.crt: OK"]
