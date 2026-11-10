"""Static assertions on the Smart Proxy of the foreman Helm chart."""

import subprocess

import pytest
import yaml

from conftest import CHART
from test_render import (
    FULLNAME, containers, pod_spec,
    test_containers_use_restricted_security_context as check_security_context,
    test_resources_have_standard_labels as check_labels,
    test_workload_containers_request_cpu_and_memory as check_requests,
)

ENABLED = {"proxy": {"enabled": True}, "certificates": {"enabled": True},
           "dynflow": {"enabled": True}}
NAME = f"{FULLNAME}-proxy"
PROXY_HOST = f"proxy.{NAME}.foreman.svc.cluster.local"
TLS = "/etc/foreman-proxy/tls"


def env(container):
    return {e["name"]: e for e in container["env"]}


def settings(manifests, name="settings.yml"):
    return yaml.safe_load(manifests["ConfigMap", NAME]["data"][name])


def plugins(manifest):
    return env(pod_spec(manifest)["containers"][0])["FOREMAN_ENABLED_PLUGINS"]["value"].split()


@pytest.fixture
def enabled(render):
    return render(ENABLED)


def test_disabled_by_default(manifests):
    assert not [key for key in manifests if "proxy" in key[1]]
    for workload in manifests.of_kind("Deployment", "Job"):
        assert not {"foreman_remote_execution", "foreman_ansible"} & set(plugins(workload))
    config = yaml.safe_load(manifests["ConfigMap", f"{FULLNAME}-config"]["data"]["settings.yaml"])
    assert config[":hosts"] == ["foreman.example.test"]


@pytest.mark.parametrize("missing", ["certificates", "dynflow"])
def test_requires_certificates_and_dynflow(tmp_path, missing):
    values = {**ENABLED, missing: {"enabled": False}}
    values_file = tmp_path / "values.yaml"
    values_file.write_text(yaml.safe_dump(values))
    result = subprocess.run(["helm", "template", "foreman", str(CHART), "--values", str(values_file)],
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert f"proxy.enabled requires {missing}.enabled" in result.stderr


def test_enabled_workloads_are_restricted_and_labelled(enabled):
    check_security_context(enabled)
    check_requests(enabled)
    check_labels(enabled)


def test_deployment(enabled):
    deployment = enabled["Deployment", NAME]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    spec = pod_spec(deployment)
    assert spec["securityContext"]["runAsUser"] == 991
    assert spec["securityContext"]["fsGroup"] == 991
    assert spec["automountServiceAccountToken"] is False
    (container,) = spec["containers"]
    assert container["image"] == "quay.io/foreman/foreman-proxy:nightly"
    assert container["command"] == ["/usr/share/foreman-proxy/bin/smart-proxy"]
    assert env(container)["FOREMAN_PROXY_ENABLED_PLUGINS"]["value"] == "remote_execution_ssh ansible"
    assert container["resources"]["requests"]["memory"]
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/instance"] == "foreman"
    assert deployment["spec"]["selector"]["matchLabels"].items() <= labels.items()
    service = enabled["Service", NAME]
    assert service["spec"]["selector"].items() <= labels.items()
    # The pod's DNS name, and the reverse DNS name of its IP, is the host of
    # the default proxy URL.
    assert service["spec"]["clusterIP"] == "None"
    assert (spec["hostname"], spec["subdomain"]) == ("proxy", NAME)
    probe = container["readinessProbe"]["httpGet"]
    assert (probe["scheme"], probe["path"]) == ("HTTPS", "/features")
    assert probe["httpHeaders"] == [{"name": "Host", "value": PROXY_HOST}]
    (port,) = service["spec"]["ports"]
    assert port["port"] == 8443
    assert {p["name"]: p["containerPort"] for p in container["ports"]}[port["targetPort"]] == 8443
    # The web Service must not select the proxy pod.
    web = enabled["Service", FULLNAME]["spec"]["selector"]
    assert not web.items() <= labels.items()


def test_mounts_settings_certificates_and_ssh_key(enabled):
    spec = pod_spec(enabled["Deployment", NAME])
    (container,) = spec["containers"]
    mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
    assert all(m.get("readOnly") for m in mounts.values())
    modules = {path.rsplit("/", 1)[1] for path in mounts
               if path.startswith("/etc/foreman-proxy/settings.d/")}
    assert modules == {"logs.yml", "dynflow.yml", "remote_execution_ssh.yml", "ansible.yml"}
    for path, mount in mounts.items():
        if mount["name"] == "configuration":
            assert mount["subPath"] == path.rsplit("/", 1)[1]
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert mounts[TLS]["name"] == "tls"
    tls = volumes["tls"]["projected"]
    assert tls["defaultMode"] == 0o440
    sources = {s["secret"]["name"]: {i["key"]: i["path"] for i in s["secret"]["items"]}
               for s in tls["sources"]}
    # Plain HTTP to Foreman: no client certificate.
    assert sources == {
        "foreman-ca": {"ca.crt": "ca.crt"},
        "foreman-proxy-tls": {"tls.crt": "server.crt", "tls.key": "server.key"},
    }
    ssh = volumes["ssh-key"]["secret"]
    assert mounts["/etc/foreman-proxy/ssh"]["name"] == "ssh-key"
    assert ssh["secretName"] == "foreman-proxy-ssh"
    assert ssh["defaultMode"] == 0o440
    assert [i["key"] for i in ssh["items"]] == ["id_rsa_foreman_proxy", "id_rsa_foreman_proxy.pub"]


def tls_sources(manifests):
    tls = {v["name"]: v for v in pod_spec(manifests["Deployment", NAME])["volumes"]}["tls"]
    return {s["secret"]["name"]: {i["key"]: i["path"] for i in s["secret"]["items"]}
            for s in tls["projected"]["sources"]}


def test_settings(enabled):
    main = settings(enabled)
    assert main[":foreman_url"] == "http://foreman-foreman.foreman.svc:3000"
    assert main[":https_port"] == 8443
    assert main[":trusted_hosts"] == ["foreman.example.test"]
    assert main[":permitted_hosts"] == [PROXY_HOST]
    assert main[":ssl_ca_file"] == f"{TLS}/ca.crt"
    assert main[":ssl_certificate"] == f"{TLS}/server.crt"
    assert main[":ssl_private_key"] == f"{TLS}/server.key"
    assert main[":log_file"] == "STDOUT"
    # Plain HTTP to Foreman: no client certificate settings.
    assert ":foreman_ssl_cert" not in main
    assert ":http_port" not in main
    for module in ("logs", "dynflow", "remote_execution_ssh", "ansible"):
        assert settings(enabled, f"{module}.yml")[":enabled"] == "https", module
    rex = settings(enabled, "remote_execution_ssh.yml")
    assert rex[":mode"] == "ssh"
    assert rex[":ssh_identity_key_file"] == "/etc/foreman-proxy/ssh/id_rsa_foreman_proxy"
    # Foreman accepts the Host header the proxy sends.
    web = yaml.safe_load(enabled["ConfigMap", f"{FULLNAME}-config"]["data"]["settings.yaml"])
    assert "foreman-foreman.foreman.svc" in web[":hosts"]


def test_https_foreman_url_uses_client_certificate(render):
    manifests = render({**ENABLED, "proxy": {
        "enabled": True, "foremanUrl": "https://foreman.example.test",
        "url": "https://proxy.example.test:8443", "trustedHosts": ["a.test", "b.test"]}})
    main = settings(manifests)
    assert main[":foreman_ssl_ca"] == f"{TLS}/ca.crt"
    assert main[":foreman_ssl_cert"] == f"{TLS}/client.crt"
    assert main[":foreman_ssl_key"] == f"{TLS}/client.key"
    assert main[":trusted_hosts"] == ["a.test", "b.test"]
    assert main[":permitted_hosts"] == ["proxy.example.test"]
    assert tls_sources(manifests)["foreman-proxy-client-tls"] == {
        "tls.crt": "client.crt", "tls.key": "client.key"}
    ansible_env = manifests["ConfigMap", NAME]["data"]["ansible.env"]
    assert f'FOREMAN_SSL_CERT="{TLS}/client.crt"' in ansible_env
    web = yaml.safe_load(manifests["ConfigMap", f"{FULLNAME}-config"]["data"]["settings.yaml"])
    # The Foreman URL host is foreman.fqdn here, listed once.
    assert web[":hosts"] == ["foreman.example.test"]
    (container,) = pod_spec(manifests["Job", f"{NAME}-register-1"])["containers"]
    assert env(container)["PROXY_NAME"]["value"] == "proxy.example.test"


def test_http_foreman_url_has_no_client_certificate(enabled):
    assert ":foreman_ssl_cert" not in settings(enabled)
    assert "FOREMAN_SSL_CERT" not in enabled["ConfigMap", NAME]["data"]["ansible.env"]


def test_front_door_is_the_default_foreman_url(render):
    """With httpd and Katello the proxy and its Job call Foreman through
    httpd, and the proxy presents its own client certificate."""
    manifests = render({
        **{name: {"enabled": True} for name in (
            "certificates", "dynflow", "httpd", "pulp", "candlepin", "katello", "proxy")},
        "foreman": {"fqdn": "foreman.test", "externalUrl": "https://foreman.test"},
    })
    front_door = "https://foreman-foreman-httpd.foreman.svc"
    main = settings(manifests)
    assert main[":foreman_url"] == front_door
    assert main[":foreman_ssl_ca"] == f"{TLS}/ca.crt"
    assert main[":foreman_ssl_cert"] == f"{TLS}/client.crt"
    assert main[":trusted_hosts"] == ["foreman.test"]
    assert tls_sources(manifests) == {
        "foreman-ca": {"ca.crt": "ca.crt"},
        "foreman-proxy-tls": {"tls.crt": "server.crt", "tls.key": "server.key"},
        "foreman-proxy-client-tls": {"tls.crt": "client.crt", "tls.key": "client.key"},
    }
    assert f"export FOREMAN_URL=\"{front_door}\"" in manifests["ConfigMap", NAME]["data"]["ansible.env"]
    web = yaml.safe_load(manifests["ConfigMap", f"{FULLNAME}-config"]["data"]["settings.yaml"])
    assert web[":hosts"] == ["foreman.test", "foreman-foreman-httpd.foreman.svc"]
    assert web[":require_ssl"] is True
    variables = env(pod_spec(manifests["Job", f"{NAME}-register-1"])["containers"][0])
    # Katello's Pulp Smart Proxy is registered by its own Job, under another name.
    jobs = {job["metadata"]["name"] for job in manifests.of_kind("Job")}
    assert {f"{NAME}-register-1", f"{FULLNAME}-pulp-smart-proxy-1"} <= jobs
    katello_job = env(pod_spec(manifests["Job", f"{FULLNAME}-pulp-smart-proxy-1"])["containers"][0])
    assert katello_job["PULP_SMART_PROXY_NAME"]["value"] != variables["PROXY_NAME"]["value"]
    for workload in manifests.of_kind("Deployment", "Job"):
        if containers(workload)[0]["image"].startswith("quay.io/foreman/foreman:"):
            assert plugins(workload) == [
                "foreman-tasks", "foreman_remote_execution", "foreman_ansible", "katello"]
    # The proxy goes through httpd; the web NetworkPolicy does not admit it.
    policy = manifests["NetworkPolicy", f"{FULLNAME}-web"]
    (rule,) = policy["spec"]["ingress"]
    (peer,) = rule["from"]
    proxy_labels = manifests["Deployment", NAME]["spec"]["template"]["metadata"]["labels"]
    (expression,) = peer["podSelector"]["matchExpressions"]
    assert proxy_labels["app.kubernetes.io/component"] not in expression["values"]


def test_ansible_can_be_disabled(render):
    manifests = render({**ENABLED, "proxy": {"enabled": True, "ansible": {"enabled": False}}})
    assert "ansible.yml" not in manifests["ConfigMap", NAME]["data"]
    (container,) = pod_spec(manifests["Deployment", NAME])["containers"]
    assert env(container)["FOREMAN_PROXY_ENABLED_PLUGINS"]["value"] == "remote_execution_ssh"
    assert "foreman_ansible" not in plugins(manifests["Deployment", FULLNAME])
    job = manifests["Job", f"{NAME}-register-1"]
    assert env(pod_spec(job)["containers"][0])["PROXY_FEATURES"]["value"] == "Dynflow Logs Script"


def test_plugins_enabled_in_every_foreman_pod(enabled):
    foreman = [w for w in enabled.of_kind("Deployment", "Job")
               if containers(w)[0]["image"].startswith("quay.io/foreman/foreman:")]
    assert len(foreman) == 6  # web, three Dynflow processes, migration, proxy registration
    for workload in foreman:
        assert plugins(workload) == ["foreman-tasks", "foreman_remote_execution", "foreman_ansible"]


def test_registration_job(enabled):
    job = enabled["Job", f"{NAME}-register-1"]
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "post-install,post-upgrade"
    assert "before-hook-creation" in annotations["helm.sh/hook-delete-policy"]
    spec = pod_spec(job)
    assert spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False
    (container,) = spec["containers"]
    # Like the Katello Pulp registration: rails runner in the Foreman image,
    # against the database, without Foreman credentials.
    assert container["image"].startswith("quay.io/foreman/foreman:")
    assert container["workingDir"] == "/usr/share/foreman"
    assert container["command"] == ["bin/rails", "runner", "/etc/foreman-proxy-registration/register.rb"]
    variables = env(container)
    assert not {"FOREMAN_USER", "FOREMAN_PASSWORD", "FOREMAN_URL"} & variables.keys()
    for variable in variables.values():
        key = variable.get("valueFrom", {}).get("secretKeyRef", {}).get("key", "")
        assert "ADMIN" not in key, variable
    assert variables["PROXY_URL"]["value"] == f"https://{PROXY_HOST}:8443"
    assert variables["PROXY_NAME"]["value"] == PROXY_HOST
    assert variables["PROXY_FEATURES"]["value"] == "Dynflow Logs Script Ansible"
    web = pod_spec(enabled["Deployment", FULLNAME])
    (web_container,) = web["containers"]
    mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
    for mount in web_container["volumeMounts"]:
        assert mounts[mount["mountPath"]] == mount
    assert mounts["/etc/foreman-proxy-registration/register.rb"]["subPath"] == "register.rb"
    script = next(v for v in spec["volumes"] if v["name"] == "script")
    assert script["configMap"]["name"] == NAME
    register = enabled["ConfigMap", NAME]["data"]["register.rb"]
    assert "SmartProxy.unscoped.find_or_initialize_by" in register
    assert "proxy.refresh" in register


def test_registration_job_loads_katello_settings(render):
    manifests = render({
        **{name: {"enabled": True} for name in (
            "certificates", "dynflow", "httpd", "pulp", "candlepin", "katello", "proxy")},
        "foreman": {"fqdn": "foreman.test", "externalUrl": "https://foreman.test"},
    })
    spec = pod_spec(manifests["Job", f"{NAME}-register-1"])
    assert [c["name"] for c in spec["initContainers"]] == ["katello-config"]
    assert plugins(manifests["Job", f"{NAME}-register-1"]) == plugins(manifests["Deployment", FULLNAME])


def test_no_secret_material_in_proxy_config(enabled):
    assert not enabled.of_kind("Secret")
    for content in enabled["ConfigMap", NAME]["data"].values():
        assert "PRIVATE KEY" not in content
        assert "foreman-test" not in content
