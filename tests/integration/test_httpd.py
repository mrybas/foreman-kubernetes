"""Exercise the httpd HTTPS front door of the installed chart.

By default the tests reach httpd through `kubectl port-forward` to the
<release>-foreman-httpd Service. Set FOREMAN_HTTPD_ADDRESS to an IP address to test
a LoadBalancer or NodePort address instead (ports 80 and 443).
"""

import base64
import contextlib
import http.client
import json
import os
import selectors
import socket
import ssl
import subprocess
import time

import pytest

from test_foreman import ADMIN_AUTH as ADMIN, FQDN

FORGED_LOGIN = "forged-external-user"
# Rack maps "-" and "_" to the same variable, so mixed spellings count too.
FORGED_IDENTITY = {
    "REMOTE-USER": FORGED_LOGIN,
    "REMOTE_USER": FORGED_LOGIN,
    "SSL-CLIENT-VERIFY": "SUCCESS",
    "SSL_CLIENT_VERIFY": "SUCCESS",
    "SSL_CLIENT-VERIFY": "SUCCESS",
    "SSL-CLIENT-S-DN": f"CN={FQDN}",
    "SSL_CLIENT-S_DN": f"CN={FQDN}",
}
SPOOFED_ADDRESS = "192.0.2.99"


@pytest.fixture(scope="module")
def namespace():
    return os.environ.get("KIND_NAMESPACE", "foreman")


@pytest.fixture(scope="module")
def release():
    return os.environ.get("HELM_RELEASE", "foreman")


@contextlib.contextmanager
def port_forward(kube_cluster, namespace, target, *remote_ports):
    """Forward ports of a Service to loopback ports; yield them in the same order.

    kubectl prints one line per port, in order, naming the pod port."""
    with subprocess.Popen(
        ["kubectl", "--kubeconfig", kube_cluster.kube_config_path,
         "--namespace", namespace, "port-forward", "--address=127.0.0.1", target,
         *(f":{port}" for port in remote_ports)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    ) as process:
        try:
            # Read the pipe unbuffered: several lines can arrive in one chunk.
            output = b""
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while output.count(b"\n") < len(remote_ports):
                    assert selector.select(timeout=30), f"kubectl port-forward did not start: {output!r}"
                    chunk = os.read(process.stdout.fileno(), 4096)
                    assert chunk, f"kubectl port-forward exited: {output!r}"
                    output += chunk
            lines = output.decode().splitlines()[:len(remote_ports)]
            for line in lines:
                assert line.startswith("Forwarding from 127.0.0.1:"), line
            yield [int(line.split(" -> ")[0].rsplit(":", 1)[1]) for line in lines]
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.fixture(scope="module")
def httpd(kube_cluster, namespace, release):
    """(address, http port, https port) of the front door."""
    address = os.environ.get("FOREMAN_HTTPD_ADDRESS")
    if address:
        yield address, 80, 443
        return
    with port_forward(kube_cluster, namespace, f"service/{release}-foreman-httpd", 80, 443) as ports:
        yield ("127.0.0.1", *ports)


@pytest.fixture(scope="module")
def web(kube_cluster, namespace, release):
    """Loopback port of the Foreman web Service, bypassing httpd."""
    with port_forward(kube_cluster, namespace, f"service/{release}-foreman", 3000) as ports:
        yield ports[0]


@pytest.fixture(scope="module")
def pki(kube_cluster, namespace, release, tmp_path_factory):
    """Server CA, client certificate and key from the release Secrets, as files."""
    directory = tmp_path_factory.mktemp("pki")
    server = kube_cluster.kubectl(f"get secret {release}-server-tls", namespace=namespace)
    client = kube_cluster.kubectl(f"get secret {release}-client-tls", namespace=namespace)
    files = {
        "ca.crt": server["data"]["ca.crt"],
        "client.crt": client["data"]["tls.crt"],
        "client.key": client["data"]["tls.key"],
    }
    for name, data in files.items():
        (directory / name).write_bytes(base64.b64decode(data))
    return {name: str(directory / name) for name in files}


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a fixed address, with SNI, Host and hostname check for another name."""

    def __init__(self, hostname, address, port, context):
        super().__init__(hostname, 443, context=context, timeout=30)
        self._address = (address, port)

    def connect(self):
        sock = socket.create_connection(self._address, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def tls_context(pki, client_cert=False):
    context = ssl.create_default_context(cafile=pki["ca.crt"])
    if client_cert:
        context.load_cert_chain(pki["client.crt"], pki["client.key"])
    return context


def https(httpd, context, method, path, headers=None, body=None, hostname=FQDN):
    connection = PinnedHTTPSConnection(hostname, httpd[0], httpd[2], context)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


def plain_http(address, port, path, headers):
    connection = http.client.HTTPConnection(address, port, timeout=30)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


def test_https_ping_verifies_server_certificate(httpd, pki):
    status, _, body = https(httpd, tls_context(pki), "GET", "/api/v2/ping")
    assert status == 200
    assert json.loads(body)["results"]["foreman"]["database"]["active"] is True


def test_certificate_covers_in_cluster_service_name(httpd, pki, namespace, release):
    # Pods call Pulp through httpd by its Service name. Foreman itself only
    # answers to its FQDN, so fetch a file httpd serves.
    status, _, _ = https(httpd, tls_context(pki), "GET", "/pub/katello-server-ca.crt",
                         hostname=f"{release}-foreman-httpd.{namespace}.svc")
    assert status == 200


@pytest.mark.parametrize("forged", [
    {},
    {"X-Forwarded-Ssl": "on"},
    {"X_Forwarded-Proto": "https"},
    {"X-Forwarded-Scheme": "https"},
    {"Forwarded": "proto=https"},
])
def test_http_redirects_to_https(httpd, forged):
    status, headers, _ = plain_http(httpd[0], httpd[1], "/api/v2/ping", {"Host": FQDN, **forged})
    assert status in (301, 302, 308), forged
    assert headers["Location"] == f"https://{FQDN}/api/v2/ping"


def test_client_cannot_choose_its_address(httpd, pki):
    """Foreman looks up hosts for provisioning templates by the client address.
    httpd replaces the X-Forwarded-For list a client sends."""
    for header in ("X-Forwarded-For", "X_Forwarded_For", "Client-IP"):
        status, _, body = https(httpd, tls_context(pki), "GET", "/unattended/provision",
                                headers={header: SPOOFED_ADDRESS})
        assert status != 500, (header, body)
        assert SPOOFED_ADDRESS.encode() not in body, (header, body)


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_pub_serves_server_ca(httpd, pki, scheme):
    path = "/pub/katello-server-ca.crt"
    if scheme == "https":
        status, _, body = https(httpd, tls_context(pki), "GET", path)
    else:
        status, _, body = plain_http(httpd[0], httpd[1], path, {"Host": FQDN})
    assert status == 200
    with open(pki["ca.crt"], "rb") as ca:
        assert body == ca.read()


def admin_api(httpd, pki, method, path, payload=None):
    status, _, body = https(
        httpd, tls_context(pki), method, path,
        headers={"Authorization": ADMIN, "Content-Type": "application/json"},
        body=None if payload is None else json.dumps(payload),
    )
    assert status == 200, body
    return json.loads(body)


@pytest.fixture
def login_delegation(httpd, pki):
    """Let Foreman trust REMOTE_USER and create unknown users, as with external
    authentication. Restore the settings and remove the user afterwards."""
    wanted = {
        "authorize_login_delegation": True,
        "authorize_login_delegation_api": True,
        "authorize_login_delegation_auth_source_user_autocreate": "External",
    }
    previous = {name: admin_api(httpd, pki, "GET", f"/api/v2/settings/{name}")["value"]
                for name in wanted}
    previous = {name: "" if value is None else value for name, value in previous.items()}
    try:
        for name, value in wanted.items():
            admin_api(httpd, pki, "PUT", f"/api/v2/settings/{name}", {"setting": {"value": value}})
        yield
    finally:
        for name, value in previous.items():
            admin_api(httpd, pki, "PUT", f"/api/v2/settings/{name}", {"setting": {"value": value}})
        users = admin_api(httpd, pki, "GET", f"/api/v2/users?search=login%3D{FORGED_LOGIN}")
        for user in users["results"]:
            admin_api(httpd, pki, "DELETE", f"/api/v2/users/{user['id']}")


def test_forged_identity_headers_do_not_authenticate(login_delegation, httpd, pki, web):
    # Control: sent straight to Foreman, the header logs the user in.
    status, _, body = plain_http(
        "127.0.0.1", web, "/api/v2/current_user",
        {"Host": FQDN, "X-Forwarded-Proto": "https", **FORGED_IDENTITY},
    )
    assert status == 200, body
    assert json.loads(body)["login"] == FORGED_LOGIN
    # Through httpd the same headers are removed or overwritten.
    for client_cert in (False, True):
        status, _, body = https(httpd, tls_context(pki, client_cert), "GET",
                                "/api/v2/current_user", headers=FORGED_IDENTITY)
        assert status == 401, (client_cert, body)


def test_valid_client_certificate_is_accepted(httpd, pki):
    status, _, _ = https(httpd, tls_context(pki, client_cert=True), "GET", "/api/v2/ping")
    assert status == 200


def test_untrusted_client_certificate_is_rejected(httpd, pki, tmp_path):
    key, cert = tmp_path / "rogue.key", tmp_path / "rogue.crt"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", f"/CN={FQDN}", "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )
    context = tls_context(pki)
    context.load_cert_chain(str(cert), str(key))
    with pytest.raises((ssl.SSLError, ConnectionResetError, http.client.RemoteDisconnected)):
        https(httpd, context, "GET", "/api/v2/ping")


def test_pulp_api_admin_requires_client_certificate(httpd, pki):
    """httpd maps the Foreman client certificate to the Pulp admin user and
    drops REMOTE-USER headers sent by clients without one."""
    with_cert = tls_context(pki, client_cert=True)
    status, _, body = https(httpd, with_cert, "GET", "/pulp/api/v3/status/")
    assert status == 200, body
    status, _, body = https(httpd, with_cert, "GET", "/pulp/api/v3/users/?username=admin")
    assert status == 200, body
    assert json.loads(body)["count"] == 1
    forged = {"REMOTE-USER": "admin", "REMOTE_USER": "admin", "Content-Type": "application/json"}
    status, _, body = https(httpd, tls_context(pki), "GET", "/pulp/api/v3/users/", headers=forged)
    assert status in (401, 403), body
    status, _, body = https(httpd, tls_context(pki), "POST", "/pulp/api/v3/repositories/file/file/",
                            headers=forged, body=json.dumps({"name": "front-door-test"}))
    assert status in (401, 403), body


def run_pod(kube_cluster, namespace, name, labels, url):
    """Run a curl pod to completion; return its exit code."""
    overrides = {"spec": {
        "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": name, "image": os.environ.get("TEST_CURL_IMAGE", "curlimages/curl:8.17.0"),
            "args": ["--silent", "--output", "/dev/null", "--max-time", "10", url],
            "securityContext": {
                "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
                "runAsUser": 100,
            },
            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                          "limits": {"cpu": "100m", "memory": "32Mi"}},
        }],
    }}
    command = ["kubectl", "--kubeconfig", kube_cluster.kube_config_path, "--namespace", namespace]
    subprocess.run(
        [*command, "run", name, "--restart=Never", "--image=unused",
         f"--labels={labels}", f"--overrides={json.dumps(overrides)}"],
        check=True, capture_output=True,
    )
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            pod = kube_cluster.kubectl(f"get pod {name}", namespace=namespace)
            if pod["status"].get("phase") in ("Succeeded", "Failed"):
                return pod["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"]
            time.sleep(2)
        pytest.fail(f"pod {name} did not finish: {pod['status']}")
    finally:
        subprocess.run([*command, "delete", "pod", name, "--wait=false"],
                       check=False, capture_output=True)


@pytest.mark.skipif(os.environ.get("NETWORK_POLICY_ENFORCED", "true") != "true",
                    reason="the cluster network plugin does not enforce NetworkPolicy")
def test_only_httpd_pods_reach_foreman_web(kube_cluster, namespace, release):
    url = f"http://{release}-foreman:3000/api/v2/ping"
    httpd_labels = f"app.kubernetes.io/instance={release},app.kubernetes.io/component=httpd"
    assert run_pod(kube_cluster, namespace, "netpol-httpd", httpd_labels, url) == 0
    # curl exit code 7 (refused) or 28 (timeout): the connection is blocked.
    worker_labels = f"app.kubernetes.io/instance={release},app.kubernetes.io/component=pulp-worker"
    assert run_pod(kube_cluster, namespace, "netpol-release", worker_labels, url) in (7, 28)
    assert run_pod(kube_cluster, namespace, "netpol-other", "app.kubernetes.io/instance=other", url) in (7, 28)
