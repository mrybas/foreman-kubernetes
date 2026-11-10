"""Static assertions on the foreman-proxy Helm chart (Smart Proxy with content)."""

import ast
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import Manifests
from test_render import containers, pod_spec

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "foreman-proxy"
RELEASE = "capsule"
NAMESPACE = "capsule"
FULLNAME = "capsule-foreman-proxy"
PULP = f"{FULLNAME}-pulp"
BUNDLE = f"{RELEASE}-bundle"
NAME = "capsule.example.test"
FOREMAN = "foreman.example.test"
REQUIRED = {"name": NAME, "foreman": {"url": f"https://{FOREMAN}"}}
REX = {"proxy": {"remoteExecutionSsh": {"enabled": True}}}
S3 = {"type": "s3", "s3": {"bucket": "pulp", "endpointUrl": "https://s3.example.test",
                           "existingSecret": "pulp-s3"}}


def deep_merge(base, override):
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def helm_template(values, tmp_path):
    values_file = tmp_path / "values.yaml"
    values_file.write_text(yaml.safe_dump(values))
    return subprocess.run(["helm", "template", RELEASE, str(CHART), "--namespace", NAMESPACE,
                           "--values", str(values_file)], capture_output=True, text=True, check=False)


@pytest.fixture
def render(tmp_path):
    """Render the chart with the required values plus overrides."""

    def _render(values=None):
        result = helm_template(deep_merge(REQUIRED, values or {}), tmp_path)
        assert result.returncode == 0, result.stderr
        manifests = Manifests()
        for doc in yaml.safe_load_all(result.stdout):
            if doc:
                key = (doc["kind"], doc["metadata"]["name"])
                assert key not in manifests, f"duplicate manifest {key}"
                manifests[key] = doc
        return manifests

    return _render


@pytest.fixture
def manifests(render):
    return render()


def workloads(manifests):
    return manifests.of_kind("Deployment", "Job")


def config(manifests, name):
    return manifests["ConfigMap", name]["data"]


def vhosts(manifests):
    data = config(manifests, f"{FULLNAME}-httpd")
    return {"http": data["content.conf"], "https": data["content-ssl.conf"]}


def env(container):
    return {item["name"]: item for item in container.get("env", [])}


def settings(manifests, name=f"{PULP}-config"):
    tree = ast.parse(config(manifests, name)["settings.py"])
    return {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body}


@pytest.mark.parametrize("values, message", [
    ({"name": ""}, "name is required"),
    ({"name": "capsule example"}, "is not a host name"),
    ({"name": "capsule.test' || true"}, "is not a host name"),
    ({"serverAliases": ["ok.test", "bad name"]}, "serverAliases"),
    ({"foreman": {"url": ""}}, "foreman.url is required"),
    ({"foreman": {"url": "http://foreman.example.test"}}, "must start with https://"),
    ({"foreman": {"clientNames": ["foreman'.test"]}}, "foreman.clientNames"),
])
def test_invalid_values_fail(tmp_path, values, message):
    result = helm_template(deep_merge(REQUIRED, values), tmp_path)
    assert result.returncode != 0
    assert message in result.stderr


def test_renders_the_smart_proxy(manifests):
    assert set(manifests) == {
        ("ConfigMap", f"{FULLNAME}-httpd"), ("Deployment", f"{FULLNAME}-httpd"),
        ("ConfigMap", f"{FULLNAME}-proxy"), ("Deployment", f"{FULLNAME}-proxy"),
        ("Service", FULLNAME),
        ("ConfigMap", f"{FULLNAME}-registration"), ("Job", f"{FULLNAME}-registration-1"),
        ("ConfigMap", f"{PULP}-config"), ("ConfigMap", f"{PULP}-migration-config"),
        ("Job", f"{PULP}-migrate-1"), ("Deployment", f"{PULP}-api"),
        ("Deployment", f"{PULP}-content"), ("Deployment", f"{PULP}-worker"),
        ("Service", f"{PULP}-api"), ("Service", f"{PULP}-content"),
        ("PersistentVolumeClaim", f"{PULP}-storage"), ("NetworkPolicy", f"{PULP}-api"),
    }


def test_containers_use_restricted_security_context(render):
    for values in ({}, {"pulp": {"storage": S3}}, REX):
        for workload in workloads(render(values)):
            pod = pod_spec(workload)
            assert pod["automountServiceAccountToken"] is False
            context = pod.get("securityContext", {})
            assert context.get("runAsNonRoot") is True
            assert context.get("seccompProfile", {}).get("type") == "RuntimeDefault"
            for container in containers(workload):
                where = f"{workload['metadata']['name']}/{container['name']}"
                security = container["securityContext"]
                assert security.get("allowPrivilegeEscalation") is False, where
                assert security.get("capabilities", {}).get("drop") == ["ALL"], where
                assert security.get("runAsNonRoot", context.get("runAsNonRoot")) is True, where
                assert {"cpu", "memory"} <= container["resources"]["requests"].keys(), where
                assert "memory" in container["resources"]["limits"], where


def test_resources_have_standard_labels(manifests):
    for (kind, name), manifest in manifests.items():
        labels = manifest["metadata"]["labels"]
        assert labels["app.kubernetes.io/name"] == "foreman-proxy", (kind, name)
        assert labels["app.kubernetes.io/instance"] == RELEASE, (kind, name)
        assert labels["helm.sh/chart"].startswith("foreman-proxy-"), (kind, name)


@pytest.mark.parametrize("storage", [{}, S3], ids=["filesystem", "s3"])
def test_secrets_come_only_from_secret_references(render, storage):
    manifests = render({"pulp": {"storage": storage}})
    assert not manifests.of_kind("Secret")
    secret_env = {"PULP_DATABASES__default__PASSWORD", "PULP_SECRET_KEY", "PULP_REDIS_URL",
                  "OAUTH_CONSUMER_KEY", "OAUTH_CONSUMER_SECRET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
    found = set()
    for workload in workloads(manifests):
        for container in containers(workload):
            for name, item in env(container).items():
                if name in secret_env:
                    found.add(name)
                    assert set(item) == {"name", "valueFrom"}, name
                    assert item["valueFrom"]["secretKeyRef"]["name"] in (BUNDLE, "pulp-runtime", "pulp-s3")
    expected = secret_env if storage else secret_env - {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
    assert found == expected
    for data in (manifest["data"] for manifest in manifests.of_kind("ConfigMap")):
        for content in data.values():
            assert "PRIVATE KEY" not in content
            assert "password" not in content.lower()
            assert "redis://" not in content


def secret_volumes(manifests):
    """{workload: {secret name: set of keys}} of every Secret a pod mounts."""
    mounted = {}
    for workload in workloads(manifests):
        for volume in pod_spec(workload).get("volumes", []):
            sources = [volume] if "secret" in volume else [
                source for source in volume.get("projected", {}).get("sources", []) if "secret" in source]
            for source in sources:
                secret = source["secret"]
                name = secret.get("secretName", secret.get("name"))
                keys = {item["key"] for item in secret["items"]}
                mounted.setdefault(workload["metadata"]["name"], {}).setdefault(name, set()).update(keys)
    return mounted


def test_bundle_keys_reach_only_the_pods_that_need_them(render):
    manifests = render({"bundle": {"secretName": "custom-bundle"}})
    mounted = secret_volumes(manifests)
    assert mounted[f"{FULLNAME}-httpd"] == {"custom-bundle": {
        "tls.crt", "tls.key", "server-ca.crt", "ca.crt", "client.crt", "client.key"}}
    assert mounted[f"{FULLNAME}-proxy"] == {"custom-bundle": {
        "tls.crt", "tls.key", "server-ca.crt", "ca.crt", "client.crt", "client.key"}}
    assert mounted[f"{FULLNAME}-registration-1"] == {"custom-bundle": {"server-ca.crt"}}
    for name in ("api", "content", "worker"):
        assert mounted[f"{PULP}-{name}"] == {"pulp-runtime": {"database_fields.symmetric.key"}}
    # The OAuth consumer goes to the registration Job only.
    for workload in workloads(manifests):
        for container in containers(workload):
            uses_oauth = any(item.get("valueFrom", {}).get("secretKeyRef", {}).get("key", "").startswith("oauth")
                             for item in container.get("env", []))
            assert uses_oauth == (workload["metadata"]["name"] == f"{FULLNAME}-registration-1"), container["name"]


def test_pulp_runs_in_mirror_mode(manifests):
    for name in (f"{PULP}-config", f"{PULP}-migration-config"):
        values = settings(manifests, name)
        assert values["SMART_PROXY_MIRROR"] is True
        assert values["CONTENT_ORIGIN"] == f"https://{NAME}"
        assert values["SMART_PROXY_PULP_URL"] == f"https://{NAME}"
        assert values["SMART_PROXY_RHSM_URL"] == f"https://{NAME}/rhsm"
        assert values["ALLOWED_IMPORT_PATHS"] == []
        assert values["ALLOWED_EXPORT_PATHS"] == []
        assert values["ENABLED_PLUGINS"] == [
            "pulp_certguard", "pulp_file", "pulp_smart_proxy", "pulp_container", "pulp_rpm"]


def test_pulp_uses_valkey(manifests):
    """Pulp reports its Valkey connection in its status, and Katello treats
    a Pulp without one as unavailable."""
    assert settings(manifests)["CACHE_ENABLED"] is True
    for name in ("api", "content", "worker"):
        (container,) = pod_spec(manifests["Deployment", f"{PULP}-{name}"])["containers"]
        assert env(container)["PULP_REDIS_URL"]["valueFrom"]["secretKeyRef"] == {
            "name": "pulp-runtime", "key": "REDIS_URL"}


def test_s3_storage_replaces_the_volume(render):
    manifests = render({"pulp": {"storage": S3}})
    assert ("PersistentVolumeClaim", f"{PULP}-storage") not in manifests
    values = settings(manifests)
    assert values["STORAGES"]["default"]["BACKEND"] == "storages.backends.s3.S3Storage"
    assert values["STORAGES"]["default"]["OPTIONS"]["bucket_name"] == "pulp"
    for name in ("api", "content", "worker"):
        spec = pod_spec(manifests["Deployment", f"{PULP}-{name}"])
        (storage,) = [volume for volume in spec["volumes"] if volume["name"] == "storage"]
        assert storage == {"name": "storage", "emptyDir": {}}
        assert "affinity" not in spec
    # The content app serves files from the bucket unless redirects are asked for.
    assert values["REDIRECT_TO_OBJECT_STORAGE"] is False
    redirect = deep_merge(S3, {"s3": {"redirectToObjectStorage": True}})
    assert settings(render({"pulp": {"storage": redirect}}))["REDIRECT_TO_OBJECT_STORAGE"] is True


@pytest.mark.parametrize("key", [
    "access_key", "secret_key", "security_token", "session_token", "session_profile"])
def test_credentials_in_s3_options_fail(tmp_path, key):
    storage = deep_merge(S3, {"s3": {"options": {key: "s3cr3t"}}})
    result = helm_template(deep_merge(REQUIRED, {"pulp": {"storage": storage}}), tmp_path)
    assert result.returncode != 0
    assert f"pulp.storage.s3.options.{key} would be rendered into a ConfigMap" in result.stderr
    assert "s3cr3t" not in result.stdout + result.stderr


def test_pulp_templates_are_a_copy_of_the_foreman_chart():
    """Until both charts use a shared library chart, the Pulp templates differ
    only by the chart's helper prefix and by the foreman chart's pulp.enabled
    switch."""
    for name in ("pulp-config.yaml", "pulp-deployments.yaml", "pulp-migration.yaml",
                 "pulp-services.yaml", "pulp-storage.yaml"):
        copy = (CHART / "templates" / name).read_text()
        original = (ROOT / "charts" / "foreman" / "templates" / name).read_text()
        original = original.replace("foreman.configChecksum", "foreman-proxy.configChecksum")
        original = original.replace("foreman.topologySpread", "foreman-proxy.topologySpread")
        original = original.replace("{{- if and .Values.pulp.enabled (", "{{- if and (")
        if original.startswith("{{- if .Values.pulp.enabled }}\n"):
            original = original.removeprefix("{{- if .Values.pulp.enabled }}\n")
            original = original[:original.rstrip().rfind("{{- end }}")].rstrip() + "\n"
        assert copy.rstrip() == original.rstrip(), name


TEMPLATE_ACTION = re.compile(r"\{\{-?\s*(.*?)\s*-?\}\}", re.S)


def defines(chart):
    """Name -> body of every named template in the .tpl files of a chart. The
    foreman-proxy prefix is renamed to foreman, so both charts compare."""
    found = {}
    for path in sorted((chart / "templates").glob("*.tpl")):
        text = path.read_text().replace('"foreman-proxy.', '"foreman.')
        depth, name, start = 0, None, 0
        for action in TEMPLATE_ACTION.finditer(text):
            words = action.group(1).split()
            word = words[0] if words else ""
            if word == "define" and depth == 0:
                name, start = words[1].strip('"'), action.end()
            if word in ("if", "range", "with", "define", "block"):
                depth += 1
            elif word == "end":
                depth -= 1
                if depth == 0 and name:
                    found[name] = text[start:action.start()]
                    name = None
    return found


# Helpers both charts define on purpose differently, and why.
INTENTIONAL_DIFFERENCES = {
    "httpd.pub": "the foreman chart serves /pub from the Foreman CA bundle Secret",
    "httpd.pulpRemoteUser": "the proxy trusts the Foreman client certificate names",
    "httpd.serverNames": "the proxy is named by .Values.name",
    "httpd.unsetClientHeaders": "the foreman chart keeps two headers from trusted proxies; "
                                "the header lists are compared below",
    "proxy.features": "the proxy chart adds the content features",
    "proxy.fullname": "different component names",
    "proxy.sshSecretName": "the whole release is the proxy, so the default is <release>-ssh",
    "proxy.url": "the proxy registers under its external name",
    "pulp.labels": "different chart labels",
    "pulp.selectorLabels": "different chart labels",
    "pulp.podSpec": "the proxy adds hostAliases",
    "pulp.settings": "mirror mode, RHSM URL, no import or export paths",
    "pulp.storageDirectories": "no import or export directories",
}


def test_shared_helpers_match_the_foreman_chart():
    """Helpers copied from the foreman chart (Pulp, httpd) must stay copies, so
    a fix in one chart cannot miss the other."""
    original = defines(ROOT / "charts" / "foreman")
    copy = defines(CHART)
    shared = sorted(set(original) & set(copy))
    assert {"pulp.env", "pulp.storageType", "httpd.unsetEverySpelling"} <= set(shared)
    assert set(INTENTIONAL_DIFFERENCES) <= set(shared), "stale entries in the allow-list"
    differing = [name for name in shared if original[name] != copy[name]]
    assert sorted(differing) == sorted(INTENTIONAL_DIFFERENCES)


def unset_header_list(body):
    (names,) = re.findall(r"\{\{-\s*range list(.*?)\}\}", body, re.S)
    return re.findall(r'"([^"]+)"', names)


def test_both_charts_remove_the_same_client_headers():
    original = defines(ROOT / "charts" / "foreman")["httpd.unsetClientHeaders"]
    copy = defines(CHART)["httpd.unsetClientHeaders"]
    assert unset_header_list(copy) == unset_header_list(original)
    assert "X-RHSM-SSL-CLIENT-CERT" in unset_header_list(copy)


UNSET_HEADERS = ("X-RHSM-SSL-CLIENT-CERT", "X-FORWARDED-HOST", "X-FORWARDED-FOR", "REMOTE-USER",
                 "SSL-CLIENT-CERT", "SSL-CLIENT-VERIFY", "X-CLIENT-CERT", "FORWARDED")


def spellings(header):
    parts = header.split("-")
    names = [parts[0]]
    for part in parts[1:]:
        names = [f"{name}{sep}{part}" for name in names for sep in "-_"]
    return names


def vhost_lines(vhost):
    """Lines of a VirtualHost outside any Location block."""
    lines, depth = [], 0
    for line in (line.strip() for line in vhost.splitlines()):
        if line.startswith("<Location") or line.startswith("<LocationMatch"):
            depth += 1
        elif line.startswith("</Location"):
            depth -= 1
        elif depth == 0:
            lines.append(line)
    return lines


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_client_headers_are_removed_before_the_smart_proxy_sets_its_own(manifests, scheme):
    lines = vhost_lines(vhosts(manifests)[scheme])
    unsets = [line.removeprefix("RequestHeader unset ") for line in lines
              if line.startswith("RequestHeader unset ")]
    for header in UNSET_HEADERS:
        for spelling in spellings(header):
            assert spelling in unsets, spelling
    # Nothing is unset conditionally, and every unset comes before the first set.
    assert all(" " not in name for name in unsets)
    first_set = next(i for i, line in enumerate(lines) if line.startswith("RequestHeader set "))
    last_unset = max(i for i, line in enumerate(lines) if line.startswith("RequestHeader unset "))
    assert last_unset < first_set
    sets = [line for line in lines if line.startswith("RequestHeader set ")]
    expected = [f'RequestHeader set X-FORWARDED-PROTO "{scheme}"']
    if scheme == "https":
        expected.append('RequestHeader set X_RHSM_SSL_CLIENT_CERT "%{SSL_CLIENT_CERT}s"')
    expected.append(f'RequestHeader set X-FORWARDED-HOST "{NAME}"')
    assert sets == expected
    # mod_proxy adds no X-Forwarded-* of its own (it would append the Host
    # the client chose) and sends the Foreman server's own name as Host.
    assert "ProxyAddHeaders Off" in lines
    assert "ProxyPreserveHost Off" in lines


def test_foreman_paths_go_to_the_foreman_server_with_the_client_certificate(manifests):
    lines = vhost_lines(vhosts(manifests)["https"])
    assert "SSLProxyMachineCertificateFile /run/httpd-client/client.pem" in lines
    assert "SSLProxyCACertificateFile /etc/httpd-tls/server-ca.crt" in lines
    assert "SSLVerifyClient optional" in lines
    passes = {line.split()[1]: line.split()[2] for line in lines if line.startswith("ProxyPass ")}
    for path in ("/rhsm", "/redhat_access", "/api/lightspeed", "/api/registration_commands",
                 "/katello/api/v2/repositories/"):
        assert passes[path] == f"https://{FOREMAN}{path}", path
    assert f"ProxyPass /rhsm https://{FOREMAN}/rhsm disablereuse=on retry=0 timeout=180" in lines
    assert (f"RewriteRule ^/pub/katello-rhsm-consumer$ https://{FOREMAN}/unattended/public/"
            f"katello-rhsm-consumer?url=https://{NAME} [P]") in lines
    # Nothing else reaches the Foreman server.
    foreman = [path for path, target in passes.items() if FOREMAN in target]
    assert sorted(foreman) == sorted(["/rhsm", "/redhat_access", "/api/lightspeed",
                                      "/api/registration_commands", "/katello/api/v2/repositories/"])
    http = vhost_lines(vhosts(manifests)["http"])
    assert not [line for line in http if line.startswith("ProxyPass ") and FOREMAN in line]


def test_only_the_foreman_server_is_pulp_administrator(render):
    manifests = render({"foreman": {"clientNames": [FOREMAN, "foreman-alias.example.test"]}})
    https = vhosts(manifests)["https"]
    for location in ("/pulp/api/v3", "/pulpcore_registry"):
        block = re.search(rf'<Location "{re.escape(location)}">(.*?)</Location>', https, re.S).group(1)
        lines = [line.strip() for line in block.strip().splitlines()]
        assert lines[:2] == ["RequestHeader unset REMOTE_USER", "RequestHeader unset REMOTE-USER"]
        assert [line for line in lines if line.startswith("RequestHeader set REMOTE-USER")] == [
            f"RequestHeader set REMOTE-USER \"admin\" \"expr=%{{SSL_CLIENT_VERIFY}} == 'SUCCESS' && "
            f"%{{SSL_CLIENT_S_DN_CN}} == '{name}'\"" for name in (FOREMAN, "foreman-alias.example.test")]
    assert "REMOTE-USER" not in vhosts(manifests)["http"].replace("RequestHeader unset REMOTE", "")


def test_service_carries_the_three_ports_to_the_right_pods(render):
    manifests = render({"service": {"type": "LoadBalancer", "clusterIP": "10.96.0.81",
                                    "externalTrafficPolicy": "Local",
                                    "annotations": {"metallb.universe.tf/loadBalancerIPs": "192.0.2.10"}}})
    service = manifests["Service", FULLNAME]
    assert service["spec"]["type"] == "LoadBalancer"
    assert service["spec"]["clusterIP"] == "10.96.0.81"
    assert service["spec"]["externalTrafficPolicy"] == "Local"
    assert service["metadata"]["annotations"] == {"metallb.universe.tf/loadBalancerIPs": "192.0.2.10"}
    ports = {port["port"]: port["targetPort"] for port in service["spec"]["ports"]}
    assert ports == {80: "httpd-http", 443: "httpd-https", 8443: "proxy-https"}
    selector = service["spec"]["selector"]
    owners = {}
    for deployment in manifests.of_kind("Deployment"):
        template = deployment["spec"]["template"]
        assert selector.items() <= template["metadata"]["labels"].items()
        for container in template["spec"]["containers"]:
            for port in container.get("ports", []):
                owners.setdefault(port["name"], set()).add(deployment["metadata"]["name"])
    assert owners["httpd-http"] == owners["httpd-https"] == {f"{FULLNAME}-httpd"}
    assert owners["proxy-https"] == {f"{FULLNAME}-proxy"}
    # Pods of other components have none of the target port names.
    assert owners["http"] == {f"{PULP}-api", f"{PULP}-content"}


def test_foreman_proxy_settings(manifests):
    data = config(manifests, f"{FULLNAME}-proxy")
    main = yaml.safe_load(data["settings.yml"])
    assert main[":foreman_url"] == f"https://{FOREMAN}"
    assert main[":trusted_hosts"] == [FOREMAN]
    assert main[":permitted_hosts"] == [NAME]
    assert main[":https_port"] == 8443
    assert main[":foreman_ssl_ca"].endswith("/server-ca.crt")
    assert main[":foreman_ssl_cert"].endswith("/client.crt")
    assert main[":ssl_ca_file"].endswith("/ca.crt")
    modules = {name.removesuffix(".yml"): yaml.safe_load(body)
               for name, body in data.items() if name != "settings.yml"}
    assert {name: module[":enabled"] for name, module in modules.items()} == {
        "logs": "https", "dynflow": "https", "registration": "https", "templates": "https"}
    assert modules["registration"][":registration_url"] == f"https://{NAME}:8443"
    assert modules["templates"][":template_url"] == f"https://{NAME}:8443"
    (container,) = pod_spec(manifests["Deployment", f"{FULLNAME}-proxy"])["containers"]
    assert env(container)["FOREMAN_PROXY_ENABLED_PLUGINS"]["value"] == ""
    mounted = {mount["mountPath"] for mount in container["volumeMounts"]}
    assert {f"/etc/foreman-proxy/settings.d/{name}.yml" for name in modules} <= mounted


def test_registration_job(render):
    manifests = render({"registration": {"organizations": ["Default Organization"],
                                         "lifecycleEnvironments": ["Library"],
                                         "downloadPolicy": "on_demand"}})
    job = manifests["Job", f"{FULLNAME}-registration-1"]
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "post-install,post-upgrade"
    (container,) = pod_spec(job)["containers"]
    assert container["image"] == "quay.io/foreman/foreman-proxy:nightly"
    values = {name: item.get("value") for name, item in env(container).items()}
    assert values["FOREMAN_URL"] == f"https://{FOREMAN}"
    assert values["PROXY_NAME"] == NAME
    assert values["PROXY_URL"] == f"https://{NAME}:8443"
    assert values["PULP_NAME"] == f"{NAME}-pulp"
    assert values["PULP_URL"] == f"https://{NAME}/pulp/api/v3/smart_proxy"
    assert values["PROXY_FEATURES"].split() == ["Dynflow", "Logs", "Registration", "Templates"]
    assert json.loads(values["ORGANIZATIONS"]) == ["Default Organization"]
    assert json.loads(values["LOCATIONS"]) == []
    assert json.loads(values["LIFECYCLE_ENVIRONMENTS"]) == ["Library"]
    assert values["DOWNLOAD_POLICY"] == "on_demand"
    script = config(manifests, f"{FULLNAME}-registration")["register.rb"]
    assert script.strip() == (CHART / "files" / "register.rb").read_text().strip()


def test_host_aliases_reach_every_pod_that_connects_out(render):
    aliases = [{"ip": "10.96.0.80", "hostnames": [FOREMAN]}]
    manifests = render({"hostAliases": aliases})
    with_aliases = {workload["metadata"]["name"] for workload in workloads(manifests)
                    if pod_spec(workload).get("hostAliases") == aliases}
    assert with_aliases == {f"{FULLNAME}-httpd", f"{FULLNAME}-proxy", f"{FULLNAME}-registration-1",
                            f"{PULP}-api", f"{PULP}-content", f"{PULP}-worker"}
    for workload in workloads(render()):
        assert "hostAliases" not in pod_spec(workload)


def test_pulp_api_admits_only_this_releases_httpd(manifests):
    policy = manifests["NetworkPolicy", f"{PULP}-api"]
    (rule,) = policy["spec"]["ingress"]
    (peer,) = rule["from"]
    labels = peer["podSelector"]["matchLabels"]
    assert labels == {"app.kubernetes.io/component": "httpd", "app.kubernetes.io/instance": RELEASE}
    httpd = manifests["Deployment", f"{FULLNAME}-httpd"]["spec"]["template"]["metadata"]["labels"]
    assert labels.items() <= httpd.items()


@pytest.mark.parametrize("change, restarted", [
    ({"httpd": {"rhsmTimeout": 300}}, {f"{FULLNAME}-httpd"}),
    ({"proxy": {"logLevel": "DEBUG"}}, {f"{FULLNAME}-proxy"}),
    ({"pulp": {"settings": {"logLevel": "debug"}}}, {f"{PULP}-api", f"{PULP}-content", f"{PULP}-worker"}),
])
def test_config_changes_restart_the_pods_that_read_it(render, change, restarted):
    def checksums(manifests):
        return {deployment["metadata"]["name"]:
                deployment["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
                for deployment in manifests.of_kind("Deployment")}

    before, after = checksums(render()), checksums(render(change))
    assert {name for name in before if before[name] != after[name]} == restarted


def proxy_modules(manifests):
    data = config(manifests, f"{FULLNAME}-proxy")
    return {name.removesuffix(".yml"): yaml.safe_load(body) for name, body in data.items()
            if name.endswith(".yml") and name != "settings.yml"}


def test_remote_execution_is_off_by_default(manifests):
    assert set(proxy_modules(manifests)) == {"logs", "dynflow", "registration", "templates"}
    assert "ansible.env" not in config(manifests, f"{FULLNAME}-proxy")
    assert f"{RELEASE}-ssh" not in secret_volumes(manifests)[f"{FULLNAME}-proxy"]


@pytest.mark.parametrize("ansible", [True, False])
def test_remote_execution_and_ansible(render, ansible):
    manifests = render(deep_merge(REX, {"proxy": {"ansible": {"enabled": ansible},
                                                  "remoteExecutionSsh": {"secretName": "capsule-key"}}}))
    modules = proxy_modules(manifests)
    rex = modules["remote_execution_ssh"]
    assert rex[":enabled"] == "https"
    assert rex[":mode"] == "ssh"
    assert rex[":ssh_identity_key_file"] == "/etc/foreman-proxy/ssh/id_rsa_foreman_proxy"
    data = config(manifests, f"{FULLNAME}-proxy")
    (container,) = pod_spec(manifests["Deployment", f"{FULLNAME}-proxy"])["containers"]
    mounts = {mount["mountPath"]: mount for mount in container["volumeMounts"]}
    assert {f"/etc/foreman-proxy/settings.d/{name}.yml" for name in modules} <= mounts.keys()
    assert mounts["/etc/foreman-proxy/ssh"] == {"name": "ssh-key", "mountPath": "/etc/foreman-proxy/ssh",
                                                "readOnly": True}
    (volume,) = [v for v in pod_spec(manifests["Deployment", f"{FULLNAME}-proxy"])["volumes"]
                 if v["name"] == "ssh-key"]
    assert volume["secret"]["secretName"] == "capsule-key"
    assert volume["secret"]["defaultMode"] == 0o440
    # The key reaches the foreman-proxy pod only.
    assert {name for name, secrets in secret_volumes(manifests).items() if "capsule-key" in secrets} == {
        f"{FULLNAME}-proxy"}
    job = manifests["Job", f"{FULLNAME}-registration-1"]
    (register,) = pod_spec(job)["containers"]
    features = env(register)["PROXY_FEATURES"]["value"].split()
    if ansible:
        assert modules["ansible"][":working_dir"] == "/var/tmp"
        assert "/etc/foreman-proxy/ansible.env" in mounts
        ansible_env = data["ansible.env"]
        assert f'export FOREMAN_URL="https://{FOREMAN}"' in ansible_env
        assert 'export FOREMAN_SSL_CERT="/etc/foreman-proxy/tls/client.crt"' in ansible_env
        assert 'export FOREMAN_SSL_VERIFY="/etc/foreman-proxy/tls/server-ca.crt"' in ansible_env
        assert env(container)["FOREMAN_PROXY_ENABLED_PLUGINS"]["value"] == "remote_execution_ssh ansible"
        assert features == ["Ansible", "Dynflow", "Logs", "Registration", "Script", "Templates"]
    else:
        assert "ansible" not in modules and "ansible.env" not in data
        assert "/etc/foreman-proxy/ansible.env" not in mounts
        assert env(container)["FOREMAN_PROXY_ENABLED_PLUGINS"]["value"] == "remote_execution_ssh"
        assert features == ["Dynflow", "Logs", "Registration", "Script", "Templates"]


def test_ansible_needs_remote_execution(render):
    manifests = render({"proxy": {"ansible": {"enabled": True}}})
    assert "ansible" not in proxy_modules(manifests)


def test_remote_execution_adds_no_secret_and_no_other_object(render):
    manifests = render(REX)
    assert set(manifests) == set(render())
    assert not manifests.of_kind("Secret")
    for content in config(manifests, f"{FULLNAME}-proxy").values():
        assert "PRIVATE KEY" not in content


def test_default_ssh_secret_name(render):
    assert f"{RELEASE}-ssh" in secret_volumes(render(REX))[f"{FULLNAME}-proxy"]


def test_registration_waits_for_the_ssh_public_key():
    script = (CHART / "files" / "register.rb").read_text()
    assert "remote_execution_pubkey" in script
