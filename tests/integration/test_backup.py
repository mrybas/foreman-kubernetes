"""Back up the installed release, delete its namespace and restore it elsewhere.

Marked `backup` and run by `make test-backup` after `make test`: it deletes the
namespace that the other integration tests use. The release is restored into
BACKUP_RESTORE_NAMESPACE (default <namespace>-restore) with test certificates
from the same test CA, issued for the new namespace.
"""

import base64
import gzip
import json
import os
import re
import ssl
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest

from test_foreman import FQDN
from test_httpd import ADMIN, https, port_forward, tls_context
from test_katello import REPO_URL, Api

pytestmark = pytest.mark.backup

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"
KIND = REPO_ROOT / "tests" / "kind"
SOURCE_NAMESPACE = os.environ.get("KIND_NAMESPACE", "foreman")
TARGET_NAMESPACE = os.environ.get("BACKUP_RESTORE_NAMESPACE", f"{SOURCE_NAMESPACE}-restore")
RELEASE = os.environ.get("HELM_RELEASE", "foreman")
RPM_MAGIC = b"\xed\xab\xee\xdb"


def httpd_service(kube_cluster, namespace):
    """Name of the release's httpd Service, found by its labels."""
    services = kube_cluster.kubectl(
        "get services --selector "
        f"app.kubernetes.io/instance={RELEASE},app.kubernetes.io/component=httpd",
        namespace=namespace)
    items = services["items"] if isinstance(services, dict) and "items" in services else services
    (service,) = items
    return service["metadata"]["name"]


@pytest.fixture(scope="module")
def env(kube_cluster):
    return {**os.environ, "KUBECONFIG": kube_cluster.kube_config_path}


@pytest.fixture(scope="module")
def state():
    """What the steps created and checked, shared by the ordered steps."""
    return {}


def step(state, *keys):
    missing = [key for key in keys if key not in state]
    if missing:
        pytest.skip(f"earlier step failed: {missing}")


def run(env, *command, timeout=1800):
    result = subprocess.run([str(part) for part in command], env=env, capture_output=True,
                            text=True, timeout=timeout, check=False)
    assert result.returncode == 0, f"{command}: {result.stdout[-4000:]}\n{result.stderr[-4000:]}"
    return result


def kubectl(env, namespace, *args, timeout=600):
    return run(env, "kubectl", "--namespace", namespace, *args, timeout=timeout)


class Release:
    """The front door of the release in one namespace."""

    def __init__(self, kube_cluster, namespace, tmp_path):
        self.kube_cluster = kube_cluster
        self.namespace = namespace
        self.tmp_path = tmp_path

    def __enter__(self):
        service = httpd_service(self.kube_cluster, self.namespace)
        self._forward = port_forward(self.kube_cluster, self.namespace,
                                     f"service/{service}", 80, 443)
        self.httpd = ("127.0.0.1", *self._forward.__enter__())
        secret = self.kube_cluster.kubectl(f"get secret {RELEASE}-server-tls", namespace=self.namespace)
        ca = self.tmp_path / f"{self.namespace}-ca.crt"
        ca.write_bytes(base64.b64decode(secret["data"]["ca.crt"]))
        self.ca = str(ca)
        self.api = Api(self.httpd, {"ca.crt": self.ca})
        return self

    def __exit__(self, *exc):
        return self._forward.__exit__(*exc)

    def with_cert(self, cert_pem, key_pem=None):
        context = ssl.create_default_context(cafile=self.ca)
        cert = self.tmp_path / f"cert-{uuid.uuid4().hex}.pem"
        cert.write_text(cert_pem + (key_pem or ""))
        context.load_cert_chain(str(cert))
        return context

    def get(self, path, context=None, headers=None):
        return https(self.httpd, context or tls_context({"ca.crt": self.ca}), "GET", path,
                     headers=headers)

    def debug_certificate(self, organization_id):
        status, _, pem = self.get(
            f"/katello/api/organizations/{organization_id}/download_debug_certificate",
            headers={"Authorization": ADMIN})
        assert status == 200, pem[:500]
        return pem.decode()


def first_package(release, context, base_path):
    """Path of the first package listed in the repository metadata."""
    status, _, repomd = release.get(base_path + "repodata/repomd.xml", context)
    assert status == 200, repomd[:500]
    primary = re.search(rb'<data type="primary">.*?<location href="([^"]+)"', repomd, re.S).group(1)
    status, _, data = release.get(base_path + primary.decode(), context)
    assert status == 200
    package = re.search(rb'<location href="([^"]+\.rpm)"', gzip.decompress(data)).group(1)
    return base_path + package.decode()


def wait_for_ping(api):
    deadline = time.monotonic() + 600
    while True:
        ping = api.get("/katello/api/ping")
        if ping["status"] == "ok" or time.monotonic() > deadline:
            return ping
        time.sleep(10)


def test_create_content_and_host(kube_cluster, state, tmp_path_factory):
    with Release(kube_cluster, SOURCE_NAMESPACE, tmp_path_factory.mktemp("source")) as release:
        api = release.api
        organization = api.post("/katello/api/organizations",
                                {"organization": {"name": f"backup-{uuid.uuid4().hex[:8]}"}})
        product = api.post("/katello/api/products",
                           {"organization_id": organization["id"], "name": "Backup"})
        repository = api.post("/katello/api/repositories", {
            "product_id": product["id"], "name": "rpm-unsigned", "content_type": "yum",
            "url": REPO_URL, "download_policy": "immediate", "unprotected": False,
        })
        task = api.wait_for_task(api.post(f"/katello/api/repositories/{repository['id']}/sync"))
        assert task["result"] == "success", task
        repository = api.get(f"/katello/api/repositories/{repository['id']}")
        assert repository["content_counts"]["rpm"] > 0
        key = api.post("/katello/api/activation_keys", {
            "organization_id": organization["id"], "name": "backup",
            "content_view_environments": ["Library"],
        })

        # A host registered the way subscription-manager does it.
        host = f"backup-{uuid.uuid4().hex[:8]}.example.test"
        status, _, body = https(
            release.httpd, tls_context({"ca.crt": release.ca}), "POST",
            f"/rhsm/consumers?owner={quote(organization['label'])}&activation_keys={quote(key['name'])}",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            body=json.dumps({"name": host, "type": {"label": "system"}, "facts": {
                "network.hostname": host, "uname.machine": "x86_64", "cpu.cpu_socket(s)": "1",
                "distribution.name": "Red Hat Enterprise Linux", "distribution.version": "9.6",
                "dmi.system.uuid": str(uuid.uuid4()),
            }}))
        assert status == 200, body[:2000]
        consumer = json.loads(body)

        base_path = urlsplit(repository["full_path"]).path
        debug = release.debug_certificate(organization["id"])
        package = first_package(release, release.with_cert(debug), base_path)
        status, _, rpm = release.get(package, release.with_cert(debug))
        assert status == 200 and rpm.startswith(RPM_MAGIC)
        state.update(httpd=httpd_service(kube_cluster, SOURCE_NAMESPACE),
                     organization=organization, product=product, repository=repository,
                     host=host, consumer=consumer, debug=debug, package=package, rpm=rpm,
                     proxies=sorted(p["name"] for p in api.get("/api/v2/smart_proxies")["results"]))


def test_backup(env, state, tmp_path_factory):
    step(state, "rpm")
    destination = tmp_path_factory.mktemp("backups")
    result = run(env, TOOLS / "backup.sh", "--namespace", SOURCE_NAMESPACE, "--release", RELEASE,
                 destination)
    backup = Path(result.stdout.strip().splitlines()[-1])
    metadata = json.loads((backup / "metadata.json").read_text())
    assert metadata["databases"] == ["foreman", "candlepin", "pulp"]
    assert metadata["mode"] == "offline"
    assert metadata["pulpContent"] is True
    for name in ("foreman-runtime", "candlepin-runtime", "pulp-runtime", f"{RELEASE}-ca",
                 f"{RELEASE}-candlepin-ca", f"{RELEASE}-proxy-ssh"):
        assert name in metadata["secrets"]
    for name in ("foreman.dump", "candlepin.dump", "pulp.dump", "pulp-content.tar.gz"):
        assert (backup / name).stat().st_size > 0
    # No secret value appears in what the script printed.
    secrets = kubectl(env, SOURCE_NAMESPACE, "get", "secret", "foreman-runtime", "pulp-runtime",
                      "candlepin-runtime", "--output", "json")
    for item in json.loads(secrets.stdout)["items"]:
        for key, value in item["data"].items():
            if re.search(r"PASSWORD|SECRET|KEY|URL", key):
                decoded = base64.b64decode(value).decode(errors="ignore")
                assert decoded not in result.stdout + result.stderr, key
    state["backup"] = backup


def test_backup_left_the_release_running(env, state):
    step(state, "backup")
    deployments = json.loads(kubectl(env, SOURCE_NAMESPACE, "get", "deployments", "--selector",
                                     f"app.kubernetes.io/instance={RELEASE}", "--output",
                                     "json").stdout)["items"]
    assert deployments
    for deployment in deployments:
        assert deployment["status"].get("readyReplicas") == deployment["spec"]["replicas"], \
            deployment["metadata"]["name"]
    jobs = kubectl(env, SOURCE_NAMESPACE, "get", "jobs", "--output", "name").stdout
    assert "backup-helper" not in jobs


def test_delete_namespace_and_restore(env, state):
    step(state, "backup")
    run(env, "kubectl", "delete", "namespace", SOURCE_NAMESPACE, "--wait=true", "--timeout=10m")
    ns = TARGET_NAMESPACE
    run(env, "kubectl", "create", "namespace", ns)
    # Empty databases and Valkey, as for a new installation.
    for name in ("dependencies.yaml", "dependencies-valkey.yaml"):
        kubectl(env, ns, "apply", "--filename", KIND / name)
    kubectl(env, ns, "rollout", "status", "deployment/postgresql", "--timeout=300s")
    kubectl(env, ns, "rollout", "status", "deployment/valkey", "--timeout=300s")
    for name in ("pulp", "candlepin"):
        kubectl(env, ns, "apply", "--filename", KIND / f"dependencies-{name}.yaml")
        kubectl(env, ns, "wait", f"job/{name}-database", "--for=condition=complete",
                "--timeout=300s")
    # Certificates that name the new namespace, from the same test CA, created
    # before the restore so that it keeps them.
    # Service names do not depend on the namespace.
    httpd = state["httpd"]
    old_proxy = f"proxy.{RELEASE}-foreman-proxy.{SOURCE_NAMESPACE}.svc.cluster.local"
    run({**env,
         "CERT_DIR": str(REPO_ROOT / "artifacts" / "certificates" / f"{SOURCE_NAMESPACE}-{RELEASE}"),
         "PROXY_SANS": f"proxy.{RELEASE}-foreman-proxy.{ns}.svc.cluster.local"},
        KIND / "certificates.sh", ns, RELEASE, FQDN, FQDN, httpd, f"{httpd}.{ns}.svc",
        f"{httpd}.{ns}.svc.cluster.local")
    run(env, TOOLS / "restore.sh", "--namespace", ns, state["backup"], "--",
        "--set-string", f"pulp.settings.smartProxyPulpUrl=https://{httpd}.{ns}.svc",
        "--set-string", f"proxy.name={old_proxy}", timeout=3600)
    state["restored"] = True


@pytest.fixture(scope="module")
def restored(kube_cluster, state, tmp_path_factory):
    step(state, "restored")
    with Release(kube_cluster, TARGET_NAMESPACE, tmp_path_factory.mktemp("target")) as release:
        yield release


def test_restored_katello_ping_is_ok(restored):
    ping = wait_for_ping(restored.api)
    assert ping["status"] == "ok", ping


def test_restored_organization_product_and_repository(restored, state):
    api = restored.api
    organization = api.get(f"/katello/api/organizations/{state['organization']['id']}")
    assert organization["name"] == state["organization"]["name"]
    product = api.get(f"/katello/api/products/{state['product']['id']}")
    assert product["name"] == "Backup"
    repository = api.get(f"/katello/api/repositories/{state['repository']['id']}")
    assert repository["name"] == "rpm-unsigned"
    assert repository["content_counts"]["rpm"] == state["repository"]["content_counts"]["rpm"]


def test_restored_content_is_served(restored, state):
    """The certificate Candlepin signed before the backup still opens the
    repository, and the package bytes come back from the restored volume."""
    context = restored.with_cert(state["debug"])
    status, _, rpm = restored.get(state["package"], context)
    assert status == 200
    assert rpm == state["rpm"]
    status, _, _ = restored.get(state["package"])
    assert status == 403


def test_restored_host_and_consumer(restored, state):
    hosts = restored.api.get("/api/v2/hosts?search=" + quote(f'name = "{state["host"]}"'))["results"]
    assert [host["name"] for host in hosts] == [state["host"]]
    consumer = state["consumer"]
    context = restored.with_cert(consumer["idCert"]["cert"], consumer["idCert"]["key"])
    status, _, body = restored.get(f"/rhsm/consumers/{consumer['uuid']}", context)
    assert status == 200, body[:1000]
    assert json.loads(body)["uuid"] == consumer["uuid"]


def test_restored_smart_proxies_point_at_the_new_namespace(restored, state):
    proxies = restored.api.get("/api/v2/smart_proxies")["results"]
    assert sorted(proxy["name"] for proxy in proxies) == state["proxies"]
    for proxy in proxies:
        assert f".{TARGET_NAMESPACE}.svc" in proxy["url"], proxy
