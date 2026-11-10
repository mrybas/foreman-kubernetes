"""Exercise the Pulp workloads of the installed chart."""

import contextlib
import json
import os
import selectors
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

COMPONENTS = ("api", "content", "worker")


@pytest.fixture(scope="module")
def namespace():
    return os.environ.get("KIND_NAMESPACE", "foreman")


@pytest.fixture(scope="module")
def prefix():
    return f"{os.environ.get('HELM_RELEASE', 'foreman')}-foreman-pulp"


@contextlib.contextmanager
def port_forward(kube_cluster, namespace, service, port):
    """Yield a local URL forwarded to the Service port."""
    with subprocess.Popen(
        ["kubectl", "--kubeconfig", kube_cluster.kube_config_path,
         "--namespace", namespace, "port-forward", "--address=127.0.0.1",
         f"service/{service}", f":{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=30), "kubectl port-forward did not start"
                line = process.stdout.readline()
            assert line.startswith("Forwarding from 127.0.0.1:"), line
            yield f"http://127.0.0.1:{line.split(' -> ')[0].rsplit(':', 1)[1]}"
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.fixture(scope="module")
def api_url(kube_cluster, namespace, prefix):
    with port_forward(kube_cluster, namespace, f"{prefix}-api", 24817) as url:
        yield url


@pytest.fixture(scope="module")
def content_url(kube_cluster, namespace, prefix):
    with port_forward(kube_cluster, namespace, f"{prefix}-content", 24816) as url:
        yield url


def get(url, headers=None):
    """Return (status, body) without raising on HTTP errors."""
    try:
        with urlopen(Request(url, headers=headers or {}), timeout=30) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


@pytest.mark.parametrize("component", COMPONENTS)
def test_deployment_is_available(kube_cluster, namespace, prefix, component):
    deployment = kube_cluster.kubectl(f"get deployment {prefix}-{component}", namespace=namespace)
    assert deployment["status"]["availableReplicas"] == deployment["spec"]["replicas"] >= 1
    assert deployment["status"]["observedGeneration"] == deployment["metadata"]["generation"]


def test_pods_share_one_node(kube_cluster, namespace):
    components = {f"pulp-{component}" for component in COMPONENTS}
    # pytest-helm-charts returns the items of a List.
    pods = kube_cluster.kubectl("get pods", namespace=namespace)
    running = [pod for pod in pods if pod["status"]["phase"] == "Running"
               and pod["metadata"]["labels"].get("app.kubernetes.io/component") in components]
    assert len(running) >= len(COMPONENTS)
    assert len({pod["spec"]["nodeName"] for pod in running}) == 1


def test_status_reports_all_services_online(api_url):
    status, body = get(f"{api_url}/pulp/api/v3/status/")
    assert status == 200
    payload = json.loads(body)
    assert payload["database_connection"]["connected"] is True
    assert payload["redis_connection"]["connected"] is True
    assert payload["online_api_apps"]
    assert payload["online_content_apps"]
    assert payload["online_workers"]
    plugins = {v["component"] for v in payload["versions"]}
    assert {"core", "rpm", "container", "file", "certguard", "smart_proxy"} <= plugins


def test_api_requires_remote_user(api_url):
    status, _ = get(f"{api_url}/pulp/api/v3/repositories/")
    assert status in (401, 403)
    # The header a front door sets after it verified the client certificate.
    status, body = get(f"{api_url}/pulp/api/v3/repositories/", {"Remote-User": "admin"})
    assert status == 200, body
    assert "count" in json.loads(body)
    # Pulp does not create users it does not know.
    status, _ = get(f"{api_url}/pulp/api/v3/repositories/", {"Remote-User": "nobody"})
    assert status in (401, 403)


def test_content_app_answers(content_url):
    status, body = get(f"{content_url}/pulp/content/")
    assert status == 200, body


def curl_from_pod(kube_cluster, namespace, name, labels, url):
    """Run curl in a short-lived pod with the given labels; return its exit code."""
    kubectl = ["kubectl", "--kubeconfig", kube_cluster.kube_config_path, "--namespace", namespace]
    pod = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "restartPolicy": "Never",
            "securityContext": {"runAsNonRoot": True, "runAsUser": 100,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": "curl", "image": "curlimages/curl:8.17.0",
                "args": ["--silent", "--fail", "--max-time", "10", "--output", "/dev/null", url],
                "securityContext": {"allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                              "limits": {"memory": "64Mi"}},
            }],
        },
    }
    subprocess.run(kubectl + ["delete", "pod", name, "--ignore-not-found", "--wait"],
                   check=True, capture_output=True, timeout=120)
    subprocess.run(kubectl + ["create", "--filename=-"], input=json.dumps(pod), text=True,
                   check=True, capture_output=True, timeout=60)
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            state = kube_cluster.kubectl(f"get pod {name}", namespace=namespace)
            if state["status"]["phase"] in ("Succeeded", "Failed"):
                return state["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"]
            time.sleep(2)
        pytest.fail(f"pod {name} did not finish")
    finally:
        subprocess.run(kubectl + ["delete", "pod", name, "--ignore-not-found", "--wait=false"],
                       check=False, capture_output=True, timeout=60)


def test_network_policy_limits_api_clients(kube_cluster, namespace, prefix):
    release = os.environ.get("HELM_RELEASE", "foreman")
    url = f"http://{prefix}-api:24817/pulp/api/v3/status/"
    front_door = {"app.kubernetes.io/instance": release, "app.kubernetes.io/component": "httpd"}
    assert curl_from_pod(kube_cluster, namespace, "pulp-np-allowed", front_door, url) == 0
    # curl exit code 28 is a timeout: the policy drops the connection.
    other = {"app.kubernetes.io/name": "pulp-np-test"}
    assert curl_from_pod(kube_cluster, namespace, "pulp-np-denied", other, url) == 28
