"""Exercise the Katello plugin through the httpd front door.

The content tests create an organization with a product and a yum repository,
sync it, publish a content view and create an activation key. The repository
is synced from KATELLO_TEST_REPO_URL, by default the in-cluster fixture that
tests/kind/rpm-repository.sh creates. The organization is deleted at the end.
"""

import json
import os
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
    assert [proxy["name"] for proxy in proxies] == [f"{FQDN}-pulp"]
    (proxy,) = proxies
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
    """Pulp serves the published repository only to clients with a certificate
    that Candlepin signed, here the organization's debug certificate."""
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
