"""Exercise the Katello plugin through the httpd front door.

The content tests create an organization with a product and a yum repository,
sync it, publish a content view and create an activation key. The repository
is synced from KATELLO_TEST_REPO_URL, by default the in-cluster fixture that
tests/kind/rpm-repository.sh creates. The organization is deleted at the end.
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
import warnings
from urllib.parse import quote, urlencode, urlsplit

import pytest

# Fixtures and helpers shared with the front door tests.
from test_httpd import ADMIN, FQDN, https, httpd, namespace, pki, release, tls_context  # noqa: F401

REPO_URL = os.environ.get("KATELLO_TEST_REPO_URL", "http://rpm-repository/")
SERVICES = ("candlepin", "candlepin_auth", "foreman_tasks", "katello_events", "pulp3", "pulp3_content")


class Api:
    """Foreman API as admin, over HTTPS through httpd."""

    def __init__(self, httpd, pki):
        self.httpd = httpd
        self.context = tls_context(pki)

    def request(self, method, path, payload=None, expect=(200, 201, 202)):
        status, _, body = https(
            self.httpd, self.context, method, path,
            headers={"Authorization": ADMIN, "Content-Type": "application/json",
                     "Accept": "application/json"},
            body=None if payload is None else json.dumps(payload),
        )
        assert status in expect, f"{method} {path}: {status} {body[:2000]!r}"
        return json.loads(body) if body else None

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload=None):
        return self.request("POST", path, payload or {})

    def wait_for_task(self, task, timeout=600):
        deadline = time.monotonic() + timeout
        while True:
            task = self.get(f"/foreman_tasks/api/tasks/{task['id']}")
            if task["state"] in ("stopped", "paused"):
                return task
            assert time.monotonic() < deadline, f"task did not finish: {task}"
            time.sleep(3)


@pytest.fixture(scope="module")
def api(httpd, pki):  # noqa: F811
    return Api(httpd, pki)


@pytest.fixture(scope="module")
def content(api):
    """State shared by the content steps; the organization is deleted afterwards."""
    state = {}
    yield state
    if "organization" in state:
        task = api.request("DELETE", f"/katello/api/organizations/{state['organization']['id']}")
        task = api.wait_for_task(task)
        if task["result"] != "success":
            warnings.warn(f"organization cleanup: {task['state']} {task['result']}")


def step(content, *keys):
    missing = [key for key in keys if key not in content]
    if missing:
        pytest.skip(f"earlier step failed: {missing}")


def test_ping_reports_all_services_ok(api):
    # The event daemon starts inside the web process shortly after boot.
    deadline = time.monotonic() + 300
    while True:
        ping = api.get("/katello/api/ping")
        services = ping["services"]
        if ping["status"] == "ok" or time.monotonic() > deadline:
            break
        time.sleep(5)
    assert ping["status"] == "ok", ping
    for name in SERVICES:
        assert services[name]["status"] == "ok", (name, services)


def test_pulp_is_registered_as_smart_proxy(api, namespace, release):  # noqa: F811
    proxies = api.get("/api/v2/smart_proxies?search=" + quote("feature = Pulpcore"))["results"]
    # Smart Proxies with content (make test-content-proxy) have the feature too.
    (proxy,) = [proxy for proxy in proxies if proxy["name"] == f"{FQDN}-pulp"]
    assert proxy["url"] == f"https://{release}-foreman-httpd.{namespace}.svc/pulp/api/v3/smart_proxy"
    detail = api.get(f"/api/v2/smart_proxies/{proxy['id']}")
    (pulp,) = [f for f in detail["features"] if f["name"] == "Pulpcore"]
    assert pulp["capabilities"], pulp


def test_create_organization_and_product(api, content):
    name = f"katello-test-{uuid.uuid4().hex[:8]}"
    content["organization"] = api.post("/katello/api/organizations", {"organization": {"name": name}})
    content["product"] = api.post("/katello/api/products", {
        "organization_id": content["organization"]["id"], "name": "Fixtures"})


def test_sync_yum_repository(api, content):
    step(content, "product")
    repository = api.post("/katello/api/repositories", {
        "product_id": content["product"]["id"], "name": "rpm-unsigned", "content_type": "yum",
        "url": REPO_URL, "download_policy": "immediate", "unprotected": False,
    })
    task = api.wait_for_task(api.post(f"/katello/api/repositories/{repository['id']}/sync"))
    assert task["result"] == "success", task
    repository = api.get(f"/katello/api/repositories/{repository['id']}")
    assert repository["content_counts"]["rpm"] > 0, repository["content_counts"]
    content["repository"] = repository


def test_repository_content_needs_an_entitlement_certificate(api, content, httpd, pki,  # noqa: F811
                                                             tmp_path):
    """Pulp serves the published repository and its packages only to clients
    with a certificate that Candlepin signed, here the organization's debug
    certificate."""
    step(content, "repository")
    path = urlsplit(content["repository"]["full_path"]).path + "repodata/repomd.xml"
    status, _, _ = https(httpd, api.context, "GET", path)
    assert status == 403
    status, _, pem = https(
        httpd, api.context, "GET",
        f"/katello/api/organizations/{content['organization']['id']}/download_debug_certificate",
        headers={"Authorization": ADMIN})
    assert status == 200
    certificate = tmp_path / "debug.pem"
    certificate.write_bytes(pem)
    with_cert = ssl.create_default_context(cafile=pki["ca.crt"])
    with_cert.load_cert_chain(str(certificate))
    status, _, body = https(httpd, with_cert, "GET", path)
    assert status == 200
    assert b"<repomd" in body
    # A package, from the Pulp storage backend.
    base = path.removesuffix("repodata/repomd.xml")
    primary = re.search(rb'<data type="primary">.*?<location href="([^"]+)"', body, re.S).group(1)
    status, _, data = https(httpd, with_cert, "GET", base + primary.decode())
    assert status == 200
    package = re.search(rb'<location href="([^"]+\.rpm)"', gzip.decompress(data)).group(1)
    status, _, rpm = https(httpd, with_cert, "GET", base + package.decode())
    assert status == 200
    assert rpm[:4] == b"\xed\xab\xee\xdb", rpm[:100]


def test_publish_content_view(api, content):
    step(content, "repository")
    view = api.post("/katello/api/content_views", {
        "organization_id": content["organization"]["id"], "name": "Fixtures",
        "repository_ids": [content["repository"]["id"]],
    })
    task = api.wait_for_task(api.post(f"/katello/api/content_views/{view['id']}/publish"))
    assert task["result"] == "success", task
    view = api.get(f"/katello/api/content_views/{view['id']}")
    assert [version["version"] for version in view["versions"]] == ["1.0"]
    content["content_view"] = view


def test_create_activation_key(api, content):
    step(content, "content_view")
    view = content["content_view"]
    key = api.post("/katello/api/activation_keys", {
        "organization_id": content["organization"]["id"], "name": "fixtures",
        "content_view_environments": [f"Library/{view['label']}"],
    })
    key = api.get(f"/katello/api/activation_keys/{key['id']}")
    (assigned,) = key["content_view_environments"]
    assert assigned["content_view"]["id"] == view["id"]
    assert assigned["lifecycle_environment"]["lifecycle_environment_library"] is True
    content["activation_key"] = key


# Every "-"/"_" spelling maps to HTTP_X_RHSM_SSL_CLIENT_CERT in Rack.
RHSM_HEADER_SPELLINGS = ("X-RHSM-SSL-CLIENT-CERT", "X_RHSM_SSL_CLIENT_CERT",
                         "X-RHSM_SSL-CLIENT-CERT", "X_RHSM-SSL_CLIENT-CERT",
                         "x-rhsm-ssl_client_cert")


def test_forged_consumer_certificate_header_is_rejected(api, content, httpd, pki,  # noqa: F811
                                                        tmp_path):
    """Katello reads X-RHSM-SSL-CLIENT-CERT before the verified client
    certificate and does not check its signature. A client without a valid
    certificate must not act as a consumer by sending that header itself."""
    step(content, "activation_key")
    name = f"forgery-{uuid.uuid4().hex[:8]}.example.test"
    status, _, body = https(
        httpd, api.context, "POST",
        "/rhsm/consumers?" + urlencode({"owner": content["organization"]["label"],
                                        "activation_keys": content["activation_key"]["name"]}),
        headers={"Content-Type": "application/json"},
        body=json.dumps({"name": name, "type": {"label": "system"}, "facts": {
            "network.hostname": name, "system.certificate_version": "3.2",
            "uname.machine": "x86_64", "distribution.name": "Fedora",
            "distribution.version": "40"}}))
    assert status == 200, body[:2000]
    consumer = json.loads(body)
    try:
        path = f"/rhsm/consumers/{consumer['uuid']}"
        # Control: the consumer's own certificate, signed by Candlepin, works.
        identity = tmp_path / "identity.pem"
        identity.write_text(consumer["idCert"]["cert"] + consumer["idCert"]["key"])
        with_identity = ssl.create_default_context(cafile=pki["ca.crt"])
        with_identity.load_cert_chain(str(identity))
        status, _, body = https(httpd, with_identity, "GET", path)
        assert status == 200, body[:2000]
        # A self-signed certificate for the same consumer, sent as a header.
        key, cert = tmp_path / "forged.key", tmp_path / "forged.crt"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", f"/CN={consumer['uuid']}", "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True)
        forged = "".join(line for line in cert.read_text().splitlines() if "-----" not in line)
        for header in RHSM_HEADER_SPELLINGS:
            status, _, body = https(httpd, api.context, "GET", path, headers={header: forged})
            assert status in (401, 403), (header, status, body[:500])
    finally:
        hosts = api.get("/api/v2/hosts?search=" + quote(f"name = {name}"))["results"]
        for host in hosts:
            api.request("DELETE", f"/api/v2/hosts/{host['id']}")


# A Smart Proxy with content forwards /rhsm for its hosts. CAPSULE_NAME is the
# CN of the client certificate tests/kind/certificates.sh issues as
# <release>-capsule-client-tls and the release lists in
# httpd.trustedProxyClientNames.
CAPSULE_NAME = os.environ.get("CAPSULE_NAME", "capsule.test")
CANONICAL_RHSM_HEADER = "X_RHSM_SSL_CLIENT_CERT"


@pytest.fixture(scope="module")
def capsule(kube_cluster, namespace, release, pki, tmp_path_factory):  # noqa: F811
    """TLS context with the capsule client certificate, if the release trusts it."""
    config = kube_cluster.kubectl(f"get configmap {release}-foreman-httpd", namespace=namespace)
    if f"'{CAPSULE_NAME}'" not in config["data"]["foreman-ssl.conf"]:
        pytest.skip(f"httpd.trustedProxyClientNames does not list {CAPSULE_NAME}")
    secret = kube_cluster.kubectl(f"get secret {release}-capsule-client-tls", namespace=namespace)
    directory = tmp_path_factory.mktemp("capsule")
    for key in ("tls.crt", "tls.key"):
        (directory / key).write_bytes(base64.b64decode(secret["data"][key]))
    context = ssl.create_default_context(cafile=pki["ca.crt"])
    context.load_cert_chain(str(directory / "tls.crt"), str(directory / "tls.key"))
    return context


def register_consumer(api, content, httpd, context, headers):  # noqa: F811
    """Register a host with the test activation key; return the consumer."""
    name = f"proxy-test-{uuid.uuid4().hex[:8]}.example.test"
    status, _, body = https(
        httpd, context, "POST",
        "/rhsm/consumers?" + urlencode({"owner": content["organization"]["label"],
                                        "activation_keys": content["activation_key"]["name"]}),
        headers={"Content-Type": "application/json", **headers},
        body=json.dumps({"name": name, "type": {"label": "system"}, "facts": {
            "network.hostname": name, "system.certificate_version": "3.2",
            "uname.machine": "x86_64", "distribution.name": "Fedora",
            "distribution.version": "40"}}))
    assert status == 200, body[:2000]
    content.setdefault("proxy_test_hosts", []).append(name)
    return json.loads(body)


@pytest.fixture(scope="module")
def proxy_test_hosts(api, content):
    yield
    for name in content.get("proxy_test_hosts", []):
        for host in api.get("/api/v2/hosts?search=" + quote(f"name = {name}"))["results"]:
            api.request("DELETE", f"/api/v2/hosts/{host['id']}")


def registered_through(api, consumer):
    hosts = api.get("/api/v2/hosts?search=" + quote(f"name = {consumer['name']}"))["results"]
    (host,) = hosts
    return api.get(f"/api/v2/hosts/{host['id']}")["subscription_facet_attributes"]["registered_through"]


def as_forwarded(pem):
    """A certificate as httpd's %{SSL_CLIENT_CERT}s puts it in a header."""
    return " ".join(pem.strip().splitlines())


def test_only_a_trusted_proxy_sets_the_registering_host(api, content, httpd, capsule,  # noqa: F811
                                                        proxy_test_hosts):
    """Katello assigns a host's content source by the first X-Forwarded-Host
    value. From a trusted proxy httpd keeps the last value the proxy sent, the
    one its own mod_proxy added; from anyone else it keeps none."""
    step(content, "activation_key")
    forged = {"X-Forwarded-Host": CAPSULE_NAME, "X_Forwarded_Host": CAPSULE_NAME}
    consumer = register_consumer(api, content, httpd, api.context, forged)
    assert registered_through(api, consumer) == FQDN
    # A capsule forwards a new host without a certificate as an empty header.
    consumer = register_consumer(api, content, httpd, capsule, {
        CANONICAL_RHSM_HEADER: "",
        "X-Forwarded-Host": f"client-chosen.example.test, {CAPSULE_NAME}"})
    assert registered_through(api, consumer) == CAPSULE_NAME
    content["proxy_consumer"] = consumer


def test_trusted_proxy_forwards_the_host_certificate(api, content, httpd, pki, capsule,  # noqa: F811
                                                     proxy_test_hosts):
    """With the capsule client certificate, X_RHSM_SSL_CLIENT_CERT carrying a
    host's own certificate authenticates as that host. Any other spelling, a
    certificate the CA signed but whose CN is not listed, or no certificate:
    httpd drops the header and Katello sees only the TLS client."""
    step(content, "proxy_consumer")
    consumer = content["proxy_consumer"]
    path = f"/rhsm/consumers/{consumer['uuid']}"
    header = as_forwarded(consumer["idCert"]["cert"])
    status, _, body = https(httpd, capsule, "GET", path, headers={CANONICAL_RHSM_HEADER: header})
    assert status == 200, body[:2000]
    assert json.loads(body)["uuid"] == consumer["uuid"]
    rejected = [
        ("capsule, hyphen spelling", capsule, "X-RHSM-SSL-CLIENT-CERT"),
        ("capsule, mixed spelling", capsule, "X-RHSM_SSL-CLIENT-CERT"),
        ("CA-valid certificate not listed", tls_context(pki, client_cert=True), CANONICAL_RHSM_HEADER),
        ("no certificate", tls_context(pki), CANONICAL_RHSM_HEADER),
    ]
    for label, context, name in rejected:
        status, _, body = https(httpd, context, "GET", path, headers={name: header})
        assert status in (401, 403, 404), (label, status, body[:500])
        assert consumer["uuid"].encode() not in body, label
