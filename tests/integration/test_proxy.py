"""Exercise the Smart Proxy, its registration and a Remote Execution job.

The job runs `uname -a` over SSH. By default the target is the ssh-target
fixture from tests/kind/dependencies-proxy.yaml. Set REX_TARGET_IP (and
optionally REX_TARGET_PORT, REX_TARGET_USER, REX_TARGET_NAME) to run it against another machine that
trusts the proxy's public key.
"""

import base64
import json
import os
import subprocess
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

# Fixtures and helpers shared with the web and front door tests.
from test_foreman import (  # noqa: F401
    ADMIN_AUTH, WEB_HEADERS, api_url, namespace, release, web_exec, workload)
from test_httpd import https, httpd, pki, tls_context  # noqa: F401

EXPECTED_FEATURES = {"Ansible", "Dynflow", "Logs", "Script"}
HEADERS = {"Authorization": ADMIN_AUTH, "Content-Type": "application/json",
           "Accept": "application/json"}


@pytest.fixture(scope="module")
def front_door(kube_cluster, namespace, release):  # noqa: F811
    services = kube_cluster.kubectl("get services", namespace=namespace)
    # pytest-helm-charts returns the items of a List.
    services = services["items"] if isinstance(services, dict) else services
    return any(item["metadata"]["name"] == f"{release}-foreman-httpd" for item in services)


@pytest.fixture(scope="module")
def api(request, front_door):
    """Foreman API as admin: through httpd when the release has it, else
    straight to the web Service. The proxy and its Job use the same path."""
    if front_door:
        address, context = request.getfixturevalue("httpd"), tls_context(request.getfixturevalue("pki"))

        def call(path, method="GET", body=None):
            status, _, data = https(address, context, method, path, headers=HEADERS,
                                    body=None if body is None else json.dumps(body))
            assert 200 <= status < 300, f"{method} {path}: {status} {data[:2000]!r}"
            return status, json.loads(data)
    else:
        url = request.getfixturevalue("api_url")

        def call(path, method="GET", body=None):
            data = json.dumps(body).encode() if body is not None else None
            with urlopen(Request(f"{url}{path}", data=data, method=method,
                                 headers={**WEB_HEADERS, **HEADERS}), timeout=120) as response:
                return response.status, json.load(response)
    return call


@pytest.fixture(scope="module")
def proxy_name(namespace, workload):  # noqa: F811
    return f"proxy.{workload}-proxy.{namespace}.svc.cluster.local"


@pytest.fixture(scope="module")
def smart_proxy(api, proxy_name):
    search = quote(f'name="{proxy_name}"')
    _, found = api(f"/api/v2/smart_proxies?search={search}")
    (proxy,) = found["results"]
    _, proxy = api(f"/api/v2/smart_proxies/{proxy['id']}")
    return proxy


def test_proxy_deployment_is_available(kube_cluster, namespace, workload):  # noqa: F811
    deployment = kube_cluster.kubectl(f"get deployment {workload}-proxy", namespace=namespace)
    assert deployment["spec"]["replicas"] == 1
    assert deployment["status"]["availableReplicas"] == 1


def test_plugins_are_loaded(api):
    _, payload = api("/api/v2/statuses")
    plugins = {plugin["name"] for plugin in payload["results"]["foreman"]["plugins"]}
    assert {"foreman_remote_execution", "foreman_ansible"} <= plugins


@pytest.fixture(scope="module")
def ssh_public_key(kube_cluster, namespace, release):  # noqa: F811
    secret = kube_cluster.kubectl(f"get secret {release}-proxy-ssh", namespace=namespace)
    return base64.b64decode(secret["data"]["id_rsa_foreman_proxy.pub"]).decode().strip()


def test_proxy_is_registered_with_features(smart_proxy, proxy_name, ssh_public_key):
    assert smart_proxy["url"] == f"https://{proxy_name}:8443"
    features = {feature["name"] for feature in smart_proxy["features"]}
    assert EXPECTED_FEATURES <= features
    # Katello registers Pulp as a separate Smart Proxy.
    assert "Pulpcore" not in features
    assert smart_proxy["organizations"] and smart_proxy["locations"]
    # The registration Job refreshes the proxy, which stores its SSH public
    # key. Global Registration installs this key on hosts.
    assert (smart_proxy["remote_execution_pubkey"] or "").strip() == ssh_public_key


def test_proxy_reaches_foreman_with_its_own_identity(kube_cluster, namespace, release,  # noqa: F811
                                                    workload, front_door, proxy_name):  # noqa: F811
    """The proxy calls Foreman through httpd when the release has it, with a
    client certificate named after the proxy URL host; else the web Service."""
    result = subprocess.run(
        ["kubectl", "--kubeconfig", kube_cluster.kube_config_path, "--namespace", namespace,
         "exec", f"deployment/{workload}-proxy", "--", "ruby", "-ryaml", "-ropenssl", "-e",
         'settings = YAML.load_file("/etc/foreman-proxy/settings.yml", permitted_classes: [Symbol]); '
         'puts settings[:foreman_url]; '
         'if settings[:foreman_ssl_cert]; '
         'cert = OpenSSL::X509::Certificate.new(File.read(settings[:foreman_ssl_cert])); '
         'puts cert.subject.to_a.assoc("CN")[1]; end'],
        capture_output=True, text=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr
    if front_door:
        assert result.stdout.split() == [f"https://{release}-foreman-httpd.{namespace}.svc", proxy_name]
    else:
        assert result.stdout.split() == [f"http://{workload}.{namespace}.svc:3000"]


def test_refresh_features(api, smart_proxy):
    """Foreman reaches the proxy over HTTPS with its client certificate."""
    status, refreshed = api(f"/api/v2/smart_proxies/{smart_proxy['id']}/refresh",
                            method="PUT")
    assert status == 200
    assert EXPECTED_FEATURES <= {feature["name"] for feature in refreshed["features"]}
    _, versions = api(f"/api/v2/smart_proxies/{smart_proxy['id']}/version")
    assert versions["result"]["version"]


def test_proxy_serves_ssh_public_key(kube_cluster, namespace, release,  # noqa: F811
                                    ssh_public_key):
    """The key the proxy offers equals the public key in the Secret."""
    expected = ssh_public_key
    result = web_exec(kube_cluster, namespace, f"{release}-foreman", "curl", "--silent",
                      "--fail", "--cacert", "/etc/foreman/certs/ca.crt",
                      "--cert", "/etc/foreman/certs/client.crt",
                      "--key", "/etc/foreman/certs/client.key",
                      f"https://proxy.{release}-foreman-proxy.{namespace}.svc.cluster.local:8443/ssh/pubkey")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.fixture(scope="module")
def target_host(kube_cluster, api, namespace, smart_proxy):  # noqa: F811
    """A host in Foreman for the SSH target, with REX parameters, in an
    organization and location of the proxy: Remote Execution picks only
    proxies there."""
    ip = os.environ.get("REX_TARGET_IP")
    if ip:
        port = os.environ.get("REX_TARGET_PORT", "22")
        user = os.environ.get("REX_TARGET_USER", "root")
        name = os.environ.get("REX_TARGET_NAME", "rex-target.test")
    else:
        service = kube_cluster.kubectl("get service ssh-target", namespace=namespace)
        ip, port, user, name = service["spec"]["clusterIP"], "2222", "foreman", "ssh-target.test"
    parameters = [
        {"name": "remote_execution_connect_by_ip", "value": "true", "parameter_type": "boolean"},
        {"name": "remote_execution_ssh_port", "value": port, "parameter_type": "integer"},
        {"name": "remote_execution_ssh_user", "value": user},
    ]
    search = quote(f'name="{name}"')
    _, found = api(f"/api/v2/hosts?search={search}")
    for host in found["results"]:
        api(f"/api/v2/hosts/{host['id']}", method="DELETE")
    _, host = api("/api/v2/hosts", method="POST", body={"host": {
        "name": name, "managed": False, "ip": ip,
        "organization_id": smart_proxy["organizations"][0]["id"],
        "location_id": smart_proxy["locations"][0]["id"],
        "host_parameters_attributes": parameters,
    }})
    return host


def test_remote_execution_job_succeeds(api, target_host):
    search = quote('name="Run Command - Script Default"')
    _, templates = api(f"/api/v2/job_templates?search={search}")
    (template,) = templates["results"]
    status, invocation = api("/api/v2/job_invocations", method="POST", body={
        "job_invocation": {
            "job_template_id": template["id"],
            "targeting_type": "static_query",
            "search_query": f'name = {target_host["name"]}',
            "inputs": {"command": "uname -a"},
        }})
    assert status == 201

    deadline = time.monotonic() + 300
    while True:
        _, invocation = api(f"/api/v2/job_invocations/{invocation['id']}")
        if invocation["status_label"] not in ("queued", "running"):
            break
        assert time.monotonic() < deadline, f"job still {invocation['status_label']}"
        time.sleep(3)
    _, output = api(f"/api/v2/job_invocations/{invocation['id']}"
                    f"/hosts/{target_host['id']}")
    text = "".join(line["output"] for line in output["output"])
    assert invocation["status_label"] == "succeeded", text
    assert invocation["succeeded"] == 1
    assert "Linux" in text, text
    print(f"{target_host['name']}: {text.strip()}")
