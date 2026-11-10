"""Exercise the Candlepin workload of the installed chart."""

import base64
import http.client
import json
import os
import selectors
import socket
import ssl
import subprocess

import pytest


@pytest.fixture(scope="module")
def namespace():
    return os.environ.get("KIND_NAMESPACE", "foreman")


@pytest.fixture(scope="module")
def release():
    return os.environ.get("HELM_RELEASE", "foreman")


@pytest.fixture(scope="module")
def candlepin(release):
    return f"{release}-foreman-candlepin"


def secret_file(kube_cluster, namespace, name, key, directory):
    secret = kube_cluster.kubectl(f"get secret {name}", namespace=namespace)
    path = directory / f"{name}-{key}"
    path.write_bytes(base64.b64decode(secret["data"][key]))
    path.chmod(0o600)
    return path


@pytest.fixture(scope="module")
def tls_context(kube_cluster, namespace, release, tmp_path_factory):
    """Trust the CA bundle Foreman trusts and present Foreman's client certificate."""
    directory = tmp_path_factory.mktemp("candlepin-tls")
    context = ssl.create_default_context(
        cafile=secret_file(kube_cluster, namespace, f"{release}-ca", "ca.crt", directory))
    context.load_cert_chain(
        secret_file(kube_cluster, namespace, f"{release}-client-tls", "tls.crt", directory),
        secret_file(kube_cluster, namespace, f"{release}-client-tls", "tls.key", directory))
    return context


@pytest.fixture(scope="module")
def local_port(kube_cluster, namespace, candlepin):
    with subprocess.Popen(
        ["kubectl", "--kubeconfig", kube_cluster.kube_config_path,
         "--namespace", namespace, "port-forward", "--address=127.0.0.1",
         f"service/{candlepin}", ":23443"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=30), "kubectl port-forward did not start"
                line = process.stdout.readline()
            assert line.startswith("Forwarding from 127.0.0.1:"), line
            yield int(line.split(" -> ")[0].rsplit(":", 1)[1])
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


class ForwardedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a forwarded local port, verifying the certificate for `host`."""

    def __init__(self, host, local_port, context):
        super().__init__(host, 23443, context=context, timeout=30)
        self.local_port = local_port

    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self.local_port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def get_status(host, local_port, context):
    connection = ForwardedHTTPSConnection(host, local_port, context)
    try:
        connection.request("GET", "/candlepin/status")
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_candlepin_deployment_is_available(kube_cluster, namespace, candlepin):
    deployment = kube_cluster.kubectl(f"get deployment {candlepin}", namespace=namespace)
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert deployment["status"]["availableReplicas"] == 1
    assert deployment["status"]["observedGeneration"] == deployment["metadata"]["generation"]


@pytest.mark.parametrize("name", ["{svc}", "{svc}.{ns}.svc", "localhost"])
def test_status_over_verified_tls(name, candlepin, namespace, local_port, tls_context):
    host = name.format(svc=candlepin, ns=namespace)
    status, payload = get_status(host, local_port, tls_context)
    assert status == 200
    assert payload["result"] is True
    assert payload["mode"] == "NORMAL"


def test_certificate_for_other_name_is_rejected(local_port, tls_context):
    with pytest.raises(ssl.SSLCertVerificationError):
        get_status("foreman.test", local_port, tls_context)


def test_credentials_are_not_logged(kube_cluster, namespace, candlepin):
    secret = kube_cluster.kubectl("get secret candlepin-runtime", namespace=namespace)
    logs = subprocess.run(
        ["kubectl", "--kubeconfig", kube_cluster.kube_config_path, "--namespace", namespace,
         "logs", f"deployment/{candlepin}", "--container", "candlepin"],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    assert "Embedded Artemis server started successfully" in logs
    for key in ("DATABASE_PASSWORD", "OAUTH_SECRET"):
        assert base64.b64decode(secret["data"][key]).decode() not in logs, key
