"""Reach httpd through the entry points that pass TLS through to it.

make test-entry installs ingress-nginx (with --enable-ssl-passthrough) and
Envoy Gateway, then the release with tests/kind/values-entry.yaml: the httpd
Service as NodePort, an Ingress with the ingress-nginx passthrough annotation
and a Gateway with a TLS passthrough listener. The tests connect to the Kind
node address on each entry point's node ports, so httpd sees TLS from a
proxy, not from a port-forward, and check that client certificates still
reach it.
"""

import json
import os
import ssl
import subprocess
import time
import uuid
from urllib.parse import quote, urlencode

import pytest

# Fixtures and helpers shared with the front door tests.
from test_httpd import (ADMIN, FQDN, https, httpd, namespace, plain_http, pki,  # noqa: F401
                        release, tls_context)
from test_katello import Api

pytestmark = pytest.mark.entry

ENTRY_POINTS = ("nodeport", "ingress", "gateway")
INGRESS_NAMESPACE = os.environ.get("ENTRY_INGRESS_NAMESPACE", "ingress-nginx")
INGRESS_SERVICE = os.environ.get("ENTRY_INGRESS_SERVICE", "ingress-nginx-controller")
GATEWAY_NAMESPACE = os.environ.get("ENTRY_GATEWAY_NAMESPACE", "envoy-gateway-system")


@pytest.fixture(scope="module")
def node_address(kube_cluster):
    address = os.environ.get("ENTRY_NODE_ADDRESS")
    if address:
        return address
    node = kube_cluster.kubectl("get nodes")[0]
    (address,) = [item["address"] for item in node["status"]["addresses"] if item["type"] == "InternalIP"]
    return address


def node_ports(service):
    """{service port: node port} of a Service."""
    return {port["port"]: port["nodePort"] for port in service["spec"]["ports"] if port.get("nodePort")}


def gateway_service(kube_cluster, namespace, release):  # noqa: F811
    """The Service Envoy Gateway created for the release's Gateway."""
    selector = (f"gateway.envoyproxy.io/owning-gateway-name={release}-foreman-httpd,"
                f"gateway.envoyproxy.io/owning-gateway-namespace={namespace}")
    deadline = time.monotonic() + 300
    while True:
        services = kube_cluster.kubectl("get services", namespace=GATEWAY_NAMESPACE, selector=selector)
        if services:
            (service,) = services
            return service
        assert time.monotonic() < deadline, f"no Service with {selector} in {GATEWAY_NAMESPACE}"
        time.sleep(5)


def wait_until_served(entry, pki):  # noqa: F811
    """The proxies pick up new routes asynchronously; wait for the first answer."""
    deadline = time.monotonic() + 300
    while True:
        try:
            status, _, body = https(entry, tls_context(pki), "GET", "/api/v2/ping")
            if status == 200:
                return
            error = f"HTTP {status}: {body[:200]!r}"
        except (OSError, ssl.SSLError) as exception:
            error = repr(exception)
        assert time.monotonic() < deadline, f"{entry} not served: {error}"
        time.sleep(5)


@pytest.fixture(scope="module", params=ENTRY_POINTS)
def entry(request, kube_cluster, namespace, release, node_address, pki):  # noqa: F811
    """(address, http port, https port) of one entry point, as test_httpd's httpd fixture."""
    if request.param == "nodeport":
        service = kube_cluster.kubectl(f"get service {release}-foreman-httpd", namespace=namespace)
        assert service["spec"]["type"] == "NodePort", "install with tests/kind/values-entry.yaml"
    elif request.param == "ingress":
        service = kube_cluster.kubectl(f"get service {INGRESS_SERVICE}", namespace=INGRESS_NAMESPACE)
    else:
        service = gateway_service(kube_cluster, namespace, release)
    ports = node_ports(service)
    address = (node_address, ports[80], ports[443])
    wait_until_served(address, pki)
    return address


@pytest.fixture(scope="module")
def consumer(httpd, pki, tmp_path_factory):  # noqa: F811
    """A host registered through /rhsm in a new organization, registered
    through the port-forward, and a TLS context with its identity certificate."""
    api = Api(httpd, pki)
    organization = api.post("/katello/api/organizations",
                            {"organization": {"name": f"entry-test-{uuid.uuid4().hex[:8]}"}})
    name = f"entry-{uuid.uuid4().hex[:8]}.example.test"
    try:
        status, _, body = https(
            httpd, tls_context(pki), "POST",
            "/rhsm/consumers?" + urlencode({"owner": organization["label"]}),
            headers={"Authorization": ADMIN, "Content-Type": "application/json"},
            body=json.dumps({"name": name, "type": {"label": "system"}, "facts": {
                "network.hostname": name, "system.certificate_version": "3.2",
                "uname.machine": "x86_64", "distribution.name": "Fedora",
                "distribution.version": "40"}}))
        assert status == 200, body[:2000]
        registered = json.loads(body)
        identity = tmp_path_factory.mktemp("consumer") / "identity.pem"
        identity.write_text(registered["idCert"]["cert"] + registered["idCert"]["key"])
        context = ssl.create_default_context(cafile=pki["ca.crt"])
        context.load_cert_chain(str(identity))
        yield registered["uuid"], context
    finally:
        for host in api.get("/api/v2/hosts?search=" + quote(f"name = {name}"))["results"]:
            api.request("DELETE", f"/api/v2/hosts/{host['id']}")
        task = api.wait_for_task(api.request("DELETE", f"/katello/api/organizations/{organization['id']}"))
        assert task["result"] == "success", task


def test_https_verifies_server_certificate(entry, pki):  # noqa: F811
    status, _, body = https(entry, tls_context(pki), "GET", "/api/v2/ping")
    assert status == 200
    assert json.loads(body)["results"]["foreman"]["database"]["active"] is True


def test_web_ui_login_page(entry, pki):  # noqa: F811
    status, headers, _ = https(entry, tls_context(pki), "GET", "/")
    assert status == 302
    assert headers["Location"] == f"https://{FQDN}/users/login"
    status, _, body = https(entry, tls_context(pki), "GET", "/users/login")
    assert status == 200
    # The form itself is rendered in the browser.
    assert b"<title>Login</title>" in body


def test_plain_http_serves_pub(entry, pki):  # noqa: F811
    status, _, body = plain_http(entry[0], entry[1], "/pub/katello-server-ca.crt", {"Host": FQDN})
    assert status == 200
    with open(pki["ca.crt"], "rb") as ca:
        assert body == ca.read()


def test_plain_http_redirects_to_https(request, entry):
    if request.node.callspec.params["entry"] == "ingress":
        pytest.skip("the Ingress routes only httpd.ingress.httpPaths to port 80")
    status, headers, _ = plain_http(entry[0], entry[1], "/api/v2/ping", {"Host": FQDN})
    assert status in (301, 302, 308)
    assert headers["Location"] == f"https://{FQDN}/api/v2/ping"


def test_httpd_checks_client_certificates(entry, pki, tmp_path):  # noqa: F811
    """httpd, not the proxy, terminates TLS: it accepts a certificate from its
    CA and refuses a self-signed one."""
    status, _, _ = https(entry, tls_context(pki, client_cert=True), "GET", "/api/v2/ping")
    assert status == 200
    key, cert = tmp_path / "rogue.key", tmp_path / "rogue.crt"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", f"/CN={FQDN}", "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True)
    context = tls_context(pki)
    context.load_cert_chain(str(cert), str(key))
    with pytest.raises((ssl.SSLError, ConnectionResetError, BrokenPipeError, OSError)):
        https(entry, context, "GET", "/api/v2/ping")


def test_pulp_api_maps_the_client_certificate(entry, pki):  # noqa: F811
    status, _, body = https(entry, tls_context(pki, client_cert=True), "GET",
                            "/pulp/api/v3/users/?username=admin")
    assert status == 200, body
    assert json.loads(body)["count"] == 1
    status, _, body = https(entry, tls_context(pki), "GET", "/pulp/api/v3/users/",
                            headers={"REMOTE-USER": "admin"})
    assert status in (401, 403), body


def test_rhsm_authenticates_the_consumer_certificate(entry, consumer, pki):  # noqa: F811
    """Katello reads the consumer certificate that httpd took from the TLS
    handshake; an escaped or missing certificate would fail."""
    consumer_uuid, identity = consumer
    path = f"/rhsm/consumers/{consumer_uuid}"
    status, _, body = https(entry, identity, "GET", path)
    assert status == 200, body[:2000]
    assert json.loads(body)["uuid"] == consumer_uuid
    status, _, body = https(entry, tls_context(pki), "GET", path)
    assert status in (401, 403), body[:500]
