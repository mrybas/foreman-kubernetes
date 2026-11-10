"""Remote Execution and Ansible jobs through the Smart Proxy with content.

make test-content-proxy installs the foreman-proxy release with
proxy.remoteExecutionSsh.enabled and an SSH server fixture (ssh-target) in
CAPSULE_NAMESPACE that trusts only the Smart Proxy's key, not the key of the
foreman release's own Smart Proxy. A /24 subnet with the Smart Proxy as its
Remote Execution proxy makes Foreman send the jobs there. The host, subnet
and job template the tests create are deleted at the end.
"""

import base64
import ipaddress
import re
import subprocess
import time
import uuid
from urllib.parse import quote, urlencode

import pytest

from test_content_proxy import CAPSULE_NAME, CAPSULE_NAMESPACE, CAPSULE_RELEASE, installed  # noqa: F401
from test_httpd import httpd, namespace, pki, release  # noqa: F401
from test_katello import Api

pytestmark = pytest.mark.content_proxy

# The fixture image has no Python, so the Ansible job uses the raw module.
ANSIBLE_TEMPLATE = """---
- hosts: all
  gather_facts: false
  tasks:
    - name: Run uname
      raw: uname -a
      register: result
    - name: Show the output
      debug:
        msg: "{{ result.stdout }}"
"""


@pytest.fixture(scope="module")
def api(httpd, pki, installed):  # noqa: F811
    return Api(httpd, pki)


@pytest.fixture(scope="module")
def capsule_proxy(api):
    found = api.get("/api/v2/smart_proxies?" + urlencode({"search": f'name = "{CAPSULE_NAME}"'}))["results"]
    assert [proxy["name"] for proxy in found] == [CAPSULE_NAME], found
    return api.get(f"/api/v2/smart_proxies/{found[0]['id']}")


def public_key(kube_cluster, namespace, secret):  # noqa: F811
    data = kube_cluster.kubectl(f"get secret {secret}", namespace=namespace)["data"]
    return base64.b64decode(data["id_rsa_foreman_proxy.pub"]).decode().strip()


def test_smart_proxy_has_remote_execution_and_its_own_key(kube_cluster, capsule_proxy, namespace, release):  # noqa: F811
    features = {feature["name"] for feature in capsule_proxy["features"]}
    assert {"Script", "Ansible"} <= features, features
    # The registration Job refreshed the record, which stores the key.
    key = public_key(kube_cluster, CAPSULE_NAMESPACE, f"{CAPSULE_RELEASE}-proxy-ssh")
    assert (capsule_proxy["remote_execution_pubkey"] or "").strip() == key
    assert key != public_key(kube_cluster, namespace, f"{release}-proxy-ssh")


@pytest.fixture(scope="module")
def target_host(kube_cluster, api, capsule_proxy):
    """The ssh-target fixture as an unmanaged host, in a subnet whose Remote
    Execution proxy is the Smart Proxy, in its first organization and
    location."""
    service = kube_cluster.kubectl("get service ssh-target", namespace=CAPSULE_NAMESPACE)
    ip = service["spec"]["clusterIP"]
    suffix = uuid.uuid4().hex[:8]
    taxonomy = {"organization_ids": [capsule_proxy["organizations"][0]["id"]],
                "location_ids": [capsule_proxy["locations"][0]["id"]]}
    subnet = api.post("/api/v2/subnets", {"subnet": {
        "name": f"capsule-rex-{suffix}", "network_type": "IPv4",
        "network": str(ipaddress.ip_network(f"{ip}/24", strict=False).network_address), "mask": "255.255.255.0",
        "ipam": "None", "boot_mode": "Static", "remote_execution_proxy_ids": [capsule_proxy["id"]], **taxonomy}})
    host = api.post("/api/v2/hosts", {"host": {
        "name": f"capsule-rex-{suffix}.test", "managed": False, "ip": ip, "subnet_id": subnet["id"],
        "organization_id": taxonomy["organization_ids"][0], "location_id": taxonomy["location_ids"][0],
        "host_parameters_attributes": [
            {"name": "remote_execution_connect_by_ip", "value": "true", "parameter_type": "boolean"},
            {"name": "remote_execution_ssh_port", "value": "2222", "parameter_type": "integer"},
            {"name": "remote_execution_ssh_user", "value": "abc"},
        ]}})
    template = api.post("/api/v2/job_templates", {"job_template": {
        "name": f"capsule-rex-ansible-{suffix}", "template": ANSIBLE_TEMPLATE, "provider_type": "Ansible",
        "job_category": "Commands", "snippet": False,
        "effective_user_attributes": {"value": "", "overridable": True, "current_user": False},
        **taxonomy}})
    yield {**host, "ansible_template": template}
    api.request("DELETE", f"/api/v2/hosts/{host['id']}")
    api.request("DELETE", f"/api/v2/subnets/{subnet['id']}")
    api.request("DELETE", f"/api/v2/job_templates/{template['id']}")


def run_job(api, host, template_id, inputs=None):
    """Run a job on the host as the fixture's user (it cannot become root).
    Return (invocation, output text, Smart Proxy name that ran it)."""
    invocation = api.post("/api/v2/job_invocations", {"job_invocation": {
        "job_template_id": template_id, "targeting_type": "static_query",
        "search_query": f"name = {host['name']}", "inputs": inputs or {}, "effective_user": "abc"}})
    deadline = time.monotonic() + 300
    while True:
        invocation = api.get(f"/api/v2/job_invocations/{invocation['id']}")
        if invocation["status_label"] not in ("queued", "running"):
            break
        assert time.monotonic() < deadline, f"job still {invocation['status_label']}"
        time.sleep(3)
    output = api.get(f"/api/v2/job_invocations/{invocation['id']}/hosts/{host['id']}")
    text = "".join(line["output"] for line in output["output"])
    (row,) = api.get(f"/api/v2/job_invocations/{invocation['id']}/hosts")["results"]
    return invocation, text, row["smart_proxy_name"]


def test_script_job_runs_through_the_smart_proxy(api, target_host):
    found = api.get("/api/v2/job_templates?search=" + quote('name = "Run Command - Script Default"'))
    (template,) = found["results"]
    invocation, text, proxy = run_job(api, target_host, template["id"], {"command": "uname -a"})
    assert invocation["status_label"] == "succeeded", text
    assert re.search(r"^Linux ", text, re.M), text
    assert proxy == CAPSULE_NAME
    print(f"{target_host['name']} via {proxy}: {text.strip()}")


def test_ansible_job_runs_through_the_smart_proxy(api, target_host):
    invocation, text, proxy = run_job(api, target_host, target_host["ansible_template"]["id"])
    assert invocation["status_label"] == "succeeded", text
    assert "Linux " in text, text
    assert proxy == CAPSULE_NAME
    print(f"{target_host['name']} via {proxy}: {text.strip()[-500:]}")


def test_job_results_reach_foreman_from_the_smart_proxy(kube_cluster, namespace, release, target_host):  # noqa: F811
    """The Smart Proxy reports job results to the Foreman server's callback
    endpoint with its client certificate; the foreman httpd logs those
    requests from the Smart Proxy pod."""
    (pod,) = kube_cluster.kubectl(
        f"get pods -l app.kubernetes.io/instance={CAPSULE_RELEASE},app.kubernetes.io/component=proxy",
        namespace=CAPSULE_NAMESPACE)
    ip = pod["status"]["podIP"]
    logs = subprocess.run(
        ["kubectl", "--kubeconfig", kube_cluster.kube_config_path, "--namespace", namespace, "logs",
         "--selector", f"app.kubernetes.io/instance={release},app.kubernetes.io/component=httpd",
         "--tail=-1", "--prefix"],
        check=True, capture_output=True, text=True).stdout
    callbacks = [line for line in logs.splitlines()
                 if line.split("] ", 1)[-1].startswith(f"{ip} ")
                 and re.search(r'"POST /foreman_tasks/api/tasks/callback[? ]', line)]
    assert callbacks, f"no callback from {ip}"
    assert all('" 200 ' in line for line in callbacks), callbacks
