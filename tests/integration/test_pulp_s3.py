"""Pulp content in S3-compatible object storage (pulp.storage.type s3).

Skipped unless the installed release uses S3 storage, for example after
`make test PULP_STORAGE=s3`. The tests sync the in-cluster yum repository
through the Pulp API, then look for the artifacts in the bucket with the MinIO
client and download a package from the content app.
"""

import gzip
import hashlib
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest

from test_katello import REPO_URL
from test_pulp import (  # noqa: F401
    COMPONENTS, api_url, content_url, get, namespace, object_storage, port_forward, prefix)

MC_IMAGE = ("cgr.dev/chainguard/minio-client@sha256:"
            "dbdb3b237441a5985c65af5a4bceed052fd01f872dae5d22d8ca5460d09c91ab")


@pytest.fixture(scope="module", autouse=True)
def s3(kube_cluster, namespace, prefix):  # noqa: F811
    """Bucket, endpoint and credentials Secret of the release."""
    if not object_storage(kube_cluster, namespace, prefix):
        pytest.skip("the release keeps Pulp content on a volume")
    settings = kube_cluster.kubectl(f"get configmap {prefix}-config",
                                    namespace=namespace)["data"]["settings.py"]
    options = json.loads(re.search(r'"OPTIONS": (\{.*\}),\n', settings).group(1)
                         .replace("True", "true").replace("False", "false").replace("None", "null"))
    api = kube_cluster.kubectl(f"get deployment {prefix}-api", namespace=namespace)
    (container,) = api["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e for e in container["env"]}
    return {
        "bucket": options["bucket_name"],
        "endpoint": options["endpoint_url"],
        "location": options.get("location", ""),
        "secret": env["AWS_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"],
        "secret_key": env["AWS_SECRET_ACCESS_KEY"]["valueFrom"]["secretKeyRef"],
        "redirect": "REDIRECT_TO_OBJECT_STORAGE = True" in settings,
    }


class PulpApi:
    """Pulp REST API as admin through a port-forward to the API Service."""

    def __init__(self, url):
        self.url = url

    def request(self, method, path, payload=None):
        url = path if path.startswith("http") else self.url + path
        request = urllib.request.Request(url, method=method, headers={
            "Remote-User": "admin", "Content-Type": "application/json"},
            data=None if payload is None else json.dumps(payload).encode())
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
        return json.loads(body) if body else None

    def wait(self, response, timeout=600):
        deadline = time.monotonic() + timeout
        while True:
            task = self.request("GET", response["task"])
            if task["state"] in ("completed", "failed", "canceled"):
                assert task["state"] == "completed", task
                return task
            assert time.monotonic() < deadline, f"task did not finish: {task}"
            time.sleep(2)


@pytest.fixture(scope="module")
def synced(api_url):  # noqa: F811
    """A yum repository synced with the immediate policy and distributed."""
    pulp = PulpApi(api_url)
    name = f"s3-{uuid.uuid4().hex[:8]}"
    remote = pulp.request("POST", "/pulp/api/v3/remotes/rpm/rpm/",
                          {"name": name, "url": REPO_URL, "policy": "immediate"})
    repository = pulp.request("POST", "/pulp/api/v3/repositories/rpm/rpm/",
                              {"name": name, "remote": remote["pulp_href"], "autopublish": True})
    created = []
    try:
        pulp.wait(pulp.request("POST", f"{repository['pulp_href']}sync/", {}))
        task = pulp.wait(pulp.request("POST", "/pulp/api/v3/distributions/rpm/rpm/", {
            "name": name, "base_path": name, "repository": repository["pulp_href"]}))
        created = task["created_resources"]
        repository = pulp.request("GET", repository["pulp_href"])
        version = repository["latest_version_href"]
        (package,) = pulp.request(
            "GET", f"/pulp/api/v3/content/rpm/packages/?repository_version={version}"
            "&limit=1&fields=location_href,sha256,artifact")["results"]
        artifact = pulp.request("GET", package["artifact"])
        yield {"pulp": pulp, "base_path": name, "package": package, "artifact": artifact}
    finally:
        for href in created + [repository["pulp_href"], remote["pulp_href"]]:
            response = pulp.request("DELETE", href)
            if response and "task" in response:
                pulp.wait(response)


def run_pod(kube_cluster, namespace, pod, timeout=300):  # noqa: F811
    """Run a pod to completion and return (exit code, logs)."""
    kubectl = ["kubectl", "--kubeconfig", kube_cluster.kube_config_path, "--namespace", namespace]
    name = pod["metadata"]["name"]
    subprocess.run(kubectl + ["delete", "pod", name, "--ignore-not-found", "--wait"],
                   check=True, capture_output=True, timeout=120)
    subprocess.run(kubectl + ["create", "--filename=-"], input=json.dumps(pod), text=True,
                   check=True, capture_output=True, timeout=60)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = kube_cluster.kubectl(f"get pod {name}", namespace=namespace)
            if state["status"]["phase"] in ("Succeeded", "Failed"):
                logs = subprocess.run(kubectl + ["logs", name], check=True, capture_output=True,
                                      text=True, timeout=60).stdout
                code = state["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"]
                return code, logs
            time.sleep(2)
        pytest.fail(f"pod {name} did not finish")
    finally:
        subprocess.run(kubectl + ["delete", "pod", name, "--ignore-not-found", "--wait=false"],
                       check=False, capture_output=True, timeout=60)


def bucket_keys(kube_cluster, namespace, s3):  # noqa: F811
    """Object keys in the bucket, listed by the MinIO client in a pod."""
    endpoint = re.sub(r"^(https?://)", r"\1$(AWS_ACCESS_KEY_ID):$(AWS_SECRET_ACCESS_KEY)@",
                      s3["endpoint"])
    pod = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": "pulp-s3-list"},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65532,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": "mc", "image": MC_IMAGE,
                "args": ["--config-dir=/tmp/mc", "ls", "--recursive", "--json",
                         f"s3/{s3['bucket']}"],
                "env": [
                    {"name": "AWS_ACCESS_KEY_ID", "valueFrom": {"secretKeyRef": s3["secret"]}},
                    {"name": "AWS_SECRET_ACCESS_KEY",
                     "valueFrom": {"secretKeyRef": s3["secret_key"]}},
                    {"name": "MC_HOST_s3", "value": endpoint},
                ],
                "securityContext": {"allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                              "limits": {"memory": "128Mi"}},
                "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
            }],
            "volumes": [{"name": "tmp", "emptyDir": {}}],
        },
    }
    code, logs = run_pod(kube_cluster, namespace, pod)
    assert code == 0, logs
    return {json.loads(line)["key"] for line in logs.splitlines() if line.startswith("{")}


def test_no_claim_and_no_node_affinity(kube_cluster, namespace, prefix):  # noqa: F811
    claims = kube_cluster.kubectl("get persistentvolumeclaims", namespace=namespace)
    assert f"{prefix}-storage" not in {claim["metadata"]["name"] for claim in claims}
    for component in COMPONENTS:
        deployment = kube_cluster.kubectl(f"get deployment {prefix}-{component}",
                                          namespace=namespace)
        spec = deployment["spec"]["template"]["spec"]
        assert "affinity" not in spec, component
        (storage,) = [v for v in spec["volumes"] if v["name"] == "storage"]
        assert "emptyDir" in storage, storage


def test_default_storage_is_s3(kube_cluster, namespace, prefix):  # noqa: F811
    """Write, read and delete an object through Django's default storage."""
    script = (
        "from django.core.files.base import ContentFile\n"
        "from django.core.files.storage import storages\n"
        "s = storages['default']\n"
        "name = s.save('pulp-s3-test/probe.txt', ContentFile(b'probe'))\n"
        "data = s.open(name).read()\n"
        "s.delete(name)\n"
        "print(type(s).__module__ + '.' + type(s).__name__, data.decode(), s.exists(name))\n"
    )
    result = subprocess.run(
        ["kubectl", "--kubeconfig", kube_cluster.kube_config_path, "--namespace", namespace,
         "exec", f"deployment/{prefix}-api", "--container", "api", "--",
         "pulpcore-manager", "shell", "--command", script],
        capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[-3:] == ["storages.backends.s3.S3Storage", "probe", "False"]


def test_synced_artifacts_are_in_the_bucket(kube_cluster, namespace, s3, synced):  # noqa: F811
    file = synced["artifact"]["file"]
    sha256 = synced["package"]["sha256"]
    assert file.endswith(f"artifact/{sha256[:2]}/{sha256[2:]}"), file
    key = f"{s3['location'].strip('/')}/{file}" if s3["location"] else file
    keys = bucket_keys(kube_cluster, namespace, s3)
    assert key in keys, sorted(keys)[:20]


def test_status_reports_object_storage(api_url, synced):  # noqa: F811
    status, body = get(f"{api_url}/pulp/api/v3/status/")
    assert status == 200
    storage = json.loads(body)["storage"]
    # Pulp reports total and free space only for filesystem storage.
    assert storage["total"] is None and storage["free"] is None, storage
    assert storage["used"] >= synced["artifact"]["size"] > 0, storage


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url, keep_redirect):
    """GET url; return (status, headers, body). Redirects are not followed
    when keep_redirect is set."""
    handlers = [NoRedirect] if keep_redirect else []
    try:
        with urllib.request.build_opener(*handlers).open(url, timeout=60) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def test_content_app_serves_package_from_the_bucket(content_url, s3, synced):  # noqa: F811
    base = f"{content_url}/pulp/content/{synced['base_path']}/"
    # The package path in the publication comes from the published metadata.
    # It is served from the bucket like the package.
    filename = synced["package"]["location_href"].rsplit("/", 1)[-1]
    status, headers, repomd = fetch(base + "repodata/repomd.xml", s3["redirect"])
    if s3["redirect"]:
        # Pulp sends the client to a signed URL on the object store.
        assert status == 302, repomd[:500]
        assert headers["Location"].startswith(s3["endpoint"]), headers["Location"]
        return
    assert status == 200, repomd[:500]
    href = re.search(rb'<data type="primary">.*?<location href="([^"]+)"', repomd, re.S).group(1)
    status, _, data = fetch(base + href.decode(), False)
    assert status == 200
    path = re.search(rf'<location href="([^"]*{re.escape(filename)})"'.encode(),
                     gzip.decompress(data)).group(1).decode()
    status, _, body = fetch(base + path, False)
    assert status == 200, body[:500]
    assert hashlib.sha256(body).hexdigest() == synced["package"]["sha256"]


def test_pod_nodes(kube_cluster, namespace):  # noqa: F811
    """Record where the Pulp pods run; nothing forces them onto one node."""
    pods = kube_cluster.kubectl("get pods", namespace=namespace)
    nodes = {pod["metadata"]["name"]: pod["spec"].get("nodeName") for pod in pods
             if pod["metadata"]["labels"].get("app.kubernetes.io/component", "").startswith("pulp-")
             and pod["status"]["phase"] == "Running"}
    print(json.dumps(nodes, indent=2))
    assert nodes
