"""Back up the Smart Proxy with content, delete its namespace and restore it
into another one.

Part of make test-content-proxy, and run after its other tests (the
runs_last marker, see conftest.py): it deletes CAPSULE_NAMESPACE and leaves
the Smart Proxy in CAPSULE_RESTORE_NAMESPACE (default
<CAPSULE_NAMESPACE>-restore). The name capsule.test does not depend on the
namespace, and the Service keeps its fixed cluster IP, so the Smart Proxy
records in Foreman stay as they are. Before the backup the test syncs a
repository of its own organization to the Smart Proxy, registers a host
through it and downloads a package, which the Smart Proxy's Pulp then keeps.
After the restore the same package must come from the restored volume with
the entitlement certificate issued before the backup, without a new request
to the foreman release.
"""

import base64
import contextlib
import gzip
import json
import os
import re
import ssl
import subprocess
import time
import uuid
import warnings
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest

from test_content_proxy import (CAPSULE_NAME, CAPSULE_NAMESPACE, CAPSULE_RELEASE, FULLNAME, PACKAGE,
                                PULP_NAME, REPO_URL, RPM_MAGIC, capsule_packages, installed,  # noqa: F401
                                main_log_lines, proxy_named, register_through)
from test_httpd import https, httpd, namespace, pki, port_forward, release, tls_context  # noqa: F401
from test_katello import Api

pytestmark = [pytest.mark.content_proxy, pytest.mark.runs_last]

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"
KIND = REPO_ROOT / "tests" / "kind"
RESTORE_NAMESPACE = os.environ.get("CAPSULE_RESTORE_NAMESPACE", f"{CAPSULE_NAMESPACE}-restore")


@pytest.fixture(scope="module")
def env(kube_cluster):
    return {**os.environ, "KUBECONFIG": kube_cluster.kube_config_path}


def run(env, *command, timeout=1800):
    result = subprocess.run([str(part) for part in command], env=env, capture_output=True,
                            text=True, timeout=timeout, check=False)
    assert result.returncode == 0, f"{command}: {result.stdout[-4000:]}\n{result.stderr[-4000:]}"
    return result


@contextlib.contextmanager
def capsule_in(kube_cluster, namespace):  # noqa: F811
    """(address, http port, https port) of the Smart Proxy's httpd in a namespace."""
    with port_forward(kube_cluster, namespace, f"deployment/{FULLNAME}-httpd", 8080, 8443) as ports:
        yield ("127.0.0.1", *ports)


def get(capsule, context, path):
    return https(capsule, context, "GET", path, hostname=CAPSULE_NAME)


@pytest.fixture(scope="module")
def api(httpd, pki):  # noqa: F811
    return Api(httpd, pki)


@pytest.fixture(scope="module")
def state(kube_cluster, namespace, release, pki):  # noqa: F811
    """Shared by the ordered steps; the organization and host are deleted at
    the end through a new connection."""
    found = {}
    yield found
    if "organization" not in found:
        return
    with port_forward(kube_cluster, namespace, f"service/{release}-foreman-httpd", 80, 443) as ports:
        cleanup = Api(("127.0.0.1", *ports), pki)
        for name in found.get("hosts", []):
            for host in cleanup.get("/api/v2/hosts?search=" + quote(f"name = {name}"))["results"]:
                cleanup.request("DELETE", f"/api/v2/hosts/{host['id']}")
        task = cleanup.wait_for_task(
            cleanup.request("DELETE", f"/katello/api/organizations/{found['organization']['id']}"))
        if task["result"] != "success":
            warnings.warn(f"organization cleanup: {task['state']} {task['result']}")


def step(state, *keys):
    missing = [key for key in keys if key not in state]
    if missing:
        pytest.skip(f"earlier step failed: {missing}")


def certificate_context(pki, tmp_path, cert, key):  # noqa: F811
    path = tmp_path / f"cert-{uuid.uuid4().hex}.pem"
    path.write_text(cert + key)
    context = ssl.create_default_context(cafile=pki["ca.crt"])
    context.load_cert_chain(str(path))
    return context


def test_content_on_the_smart_proxy(kube_cluster, installed, api, state, pki, tmp_path):  # noqa: F811
    organization = api.post("/katello/api/organizations",
                            {"organization": {"name": f"capsule-backup-{uuid.uuid4().hex[:8]}"}})
    state["organization"] = organization
    product = api.post("/katello/api/products", {"organization_id": organization["id"], "name": "Backup"})
    repository = api.post("/katello/api/repositories", {
        "product_id": product["id"], "name": "rpm-unsigned", "content_type": "yum",
        "url": REPO_URL, "download_policy": "immediate", "unprotected": False,
    })
    task = api.wait_for_task(api.post(f"/katello/api/repositories/{repository['id']}/sync"))
    assert task["result"] == "success", task
    repository = api.get(f"/katello/api/repositories/{repository['id']}")
    key = api.post("/katello/api/activation_keys", {
        "organization_id": organization["id"], "name": "capsule-backup",
        "content_view_environments": ["Library"]})
    library = api.get(f"/katello/api/organizations/{organization['id']}/environments?library=true")["results"][0]

    # The new organization on both records and its Library on the Pulp one,
    # as the registration Job does on upgrade.
    for name in (CAPSULE_NAME, PULP_NAME):
        proxy = proxy_named(api, name)
        api.request("PUT", f"/api/smart_proxies/{proxy['id']}", {"smart_proxy": {
            "organization_ids": [org["id"] for org in proxy["organizations"]] + [organization["id"]]}})
    pulp = proxy_named(api, PULP_NAME)
    api.post(f"/katello/api/capsules/{pulp['id']}/content/lifecycle_environments",
             {"environment_id": library["id"]})
    task = api.wait_for_task(api.post(f"/katello/api/capsules/{pulp['id']}/content/sync",
                                      {"environment_id": library["id"]}), timeout=900)
    assert task["result"] == "success", task

    with capsule_in(kube_cluster, CAPSULE_NAMESPACE) as capsule:
        content = {"organization": organization, "activation_key": key}
        consumer = register_through(capsule, tls_context(pki), content, {})
        state["hosts"] = content["hosts"]
        identity = certificate_context(pki, tmp_path, consumer["idCert"]["cert"], consumer["idCert"]["key"])
        status, _, body = get(capsule, identity, f"/rhsm/consumers/{consumer['uuid']}/certificates")
        assert status == 200, body[:1000]
        entitlement = json.loads(body)[0]
        entitled = certificate_context(pki, tmp_path, entitlement["cert"], entitlement["key"])

        base = urlsplit(repository["full_path"]).path
        status, _, repomd = get(capsule, entitled, base + "repodata/repomd.xml")
        assert status == 200, repomd[:500]
        primary = re.search(rb'<data type="primary">.*?<location href="([^"]+)"', repomd, re.S).group(1)
        status, _, data = get(capsule, entitled, base + primary.decode())
        assert status == 200
        package = re.search(rf'<location href="([^"]*/{PACKAGE}-[^"]+\.rpm)"'.encode(),
                            gzip.decompress(data)).group(1)
        path = base + package.decode()

        # Drop on_demand files that earlier tests stored, so that this
        # download is the one that fills the volume.
        task = api.wait_for_task(api.post(f"/katello/api/capsules/{pulp['id']}/content/reclaim_space"))
        assert task["result"] == "success", task
        assert [item["artifact"] for item in capsule_packages(capsule, pki)] == [None]
        status, _, rpm = get(capsule, entitled, path)
        assert status == 200 and rpm[:4] == RPM_MAGIC
        deadline = time.monotonic() + 120
        while capsule_packages(capsule, pki)[0]["artifact"] is None:
            assert time.monotonic() < deadline, "the Smart Proxy's Pulp did not keep the package"
            time.sleep(3)
    state.update(consumer=consumer, entitlement=entitlement, path=path, rpm=rpm,
                 proxies={name: proxy_named(api, name) for name in (CAPSULE_NAME, PULP_NAME)})


def test_backup(env, state, kube_cluster, tmp_path_factory):
    step(state, "rpm")
    destination = tmp_path_factory.mktemp("capsule-backups")
    command = [TOOLS / "backup.sh", "--namespace", CAPSULE_NAMESPACE, "--release", CAPSULE_RELEASE, destination]
    # Like foremanctl, the script refuses while Pulp tasks run, for example
    # after the organization of an earlier test was deleted.
    deadline = time.monotonic() + 600
    while True:
        result = subprocess.run([str(part) for part in command], env=env, capture_output=True, text=True,
                                timeout=1800, check=False)
        if result.returncode == 0 or "task(s) are running" not in result.stderr \
                or time.monotonic() > deadline:
            break
        time.sleep(15)
    assert result.returncode == 0, f"{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
    backup = Path(result.stdout.strip().splitlines()[-1])
    assert backup.name.startswith("foreman-proxy-backup-")
    metadata = json.loads((backup / "metadata.json").read_text())
    assert metadata["chartName"] == "foreman-proxy"
    assert metadata["proxyName"] == CAPSULE_NAME
    assert metadata["databases"] == ["pulp", "container_gateway"]
    assert metadata["mode"] == "offline"
    assert metadata["pulpContent"] is True
    secrets = {f"{CAPSULE_RELEASE}-bundle", f"{CAPSULE_RELEASE}-proxy-ssh", "pulp-runtime",
               "container-gateway-runtime"}
    assert set(metadata["secrets"]) == secrets
    for name in ("pulp.dump", "container_gateway.dump", "pulp-content.tar.gz", "values.yaml"):
        assert (backup / name).stat().st_size > 0, name
    # No secret value appears in what the script printed.
    printed = result.stdout + result.stderr
    items = json.loads(run(env, "kubectl", "--namespace", CAPSULE_NAMESPACE, "get", "secret", *sorted(secrets),
                           "--output", "json").stdout)["items"]
    for item in items:
        for key, value in item["data"].items():
            if re.search(r"PASSWORD|SECRET|KEY|URL|secret|key", key):
                assert base64.b64decode(value).decode(errors="ignore") not in printed, key
    # The release runs again.
    deployments = kube_cluster.kubectl(f"get deployments -l app.kubernetes.io/instance={CAPSULE_RELEASE}",
                                       namespace=CAPSULE_NAMESPACE)
    for deployment in deployments:
        assert deployment["status"].get("readyReplicas") == deployment["spec"]["replicas"], \
            deployment["metadata"]["name"]
    state["backup"] = backup


def test_delete_namespace_and_restore(env, state):
    step(state, "backup")
    run(env, "kubectl", "delete", "namespace", CAPSULE_NAMESPACE, "--wait=true", "--timeout=10m")
    ns = RESTORE_NAMESPACE
    run(env, "kubectl", "create", "namespace", ns)
    run(env, "kubectl", "label", "namespace", ns, "pod-security.kubernetes.io/enforce=restricted",
        "pod-security.kubernetes.io/warn=restricted")
    # Empty databases and Valkey, as for a new installation.
    for name in ("dependencies-content-proxy.yaml", "dependencies-valkey.yaml"):
        run(env, "kubectl", "--namespace", ns, "apply", "--filename", KIND / name)
    for name in ("postgresql", "valkey"):
        run(env, "kubectl", "--namespace", ns, "rollout", "status", f"deployment/{name}", "--timeout=300s")
    result = run(env, TOOLS / "restore.sh", "--namespace", ns, state["backup"], timeout=3600)
    # capsule.test does not name the namespace: no rename.
    assert "renamed" not in result.stderr
    state["restored"] = True


@pytest.fixture(scope="module")
def restored(kube_cluster, state):
    step(state, "restored")
    with capsule_in(kube_cluster, RESTORE_NAMESPACE) as capsule:
        yield capsule


def test_foreman_sees_the_restored_smart_proxy(api, state, restored):
    """The same two records, refreshed with all features from the restored
    pods, and a sync of the Library to it succeeds."""
    for name, before in state["proxies"].items():
        proxy = proxy_named(api, name)
        assert proxy["id"] == before["id"], name
        assert proxy["url"] == before["url"], name
        api.request("PUT", f"/api/smart_proxies/{proxy['id']}/refresh")
        features = {feature["name"] for feature in proxy_named(api, name)["features"]}
        assert features == {feature["name"] for feature in before["features"]}, name
    pulp = state["proxies"][PULP_NAME]
    library = api.get(f"/katello/api/organizations/{state['organization']['id']}"
                      "/environments?library=true")["results"][0]
    task = api.wait_for_task(api.post(f"/katello/api/capsules/{pulp['id']}/content/sync",
                                      {"environment_id": library["id"]}), timeout=900)
    assert task["result"] == "success", task


def test_restored_content_is_served_from_the_restored_volume(kube_cluster, namespace, release, state,  # noqa: F811
                                                             restored, pki, tmp_path):
    """The restored Pulp database knows the stored package and the file is on
    the restored volume: the certificate issued before the backup downloads
    the same bytes, and the foreman release sees no new request for it."""
    assert capsule_packages(restored, pki)[0]["artifact"] is not None
    log = (kube_cluster, namespace, release, state["organization"]["label"], state["path"].rsplit("/", 1)[1])
    fetched = main_log_lines(*log)
    entitlement = state["entitlement"]
    entitled = certificate_context(pki, tmp_path, entitlement["cert"], entitlement["key"])
    status, _, rpm = get(restored, entitled, state["path"])
    assert status == 200
    assert rpm == state["rpm"]
    assert main_log_lines(*log) == fetched
    status, _, _ = get(restored, tls_context(pki), state["path"])
    assert status == 403


def test_host_reaches_foreman_through_the_restored_smart_proxy(state, restored, pki, tmp_path):  # noqa: F811
    consumer = state["consumer"]
    identity = certificate_context(pki, tmp_path, consumer["idCert"]["cert"], consumer["idCert"]["key"])
    status, _, body = get(restored, identity, f"/rhsm/consumers/{consumer['uuid']}")
    assert status == 200, body[:1000]
    assert json.loads(body)["uuid"] == consumer["uuid"]


def test_restored_release_runs_in_a_restricted_namespace(kube_cluster):
    """The namespace enforces the restricted profile, so every pod of the
    release was admitted under it."""
    labels = kube_cluster.kubectl(f"get namespace {RESTORE_NAMESPACE}")["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
    pods = kube_cluster.kubectl(f"get pods -l app.kubernetes.io/instance={CAPSULE_RELEASE}",
                                namespace=RESTORE_NAMESPACE)
    running = {pod["metadata"]["labels"]["app.kubernetes.io/component"] for pod in pods
               if pod["status"].get("phase") == "Running"}
    assert running >= {"httpd", "proxy", "pulp-api", "pulp-content", "pulp-worker"}, running
