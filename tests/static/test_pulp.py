"""Static assertions on the Pulp workloads of the foreman Helm chart."""

import pytest
import yaml

from conftest import render_chart
from test_render import FULLNAME, containers, pod_spec

PULP = f"{FULLNAME}-pulp"
RUNTIME = ("api", "content", "worker")
SECRET_ENV = {
    "PULP_DATABASES__default__PASSWORD": "DATABASE_PASSWORD",
    "PULP_SECRET_KEY": "SECRET_KEY",
    "PULP_REDIS_URL": "REDIS_URL",
}


@pytest.fixture(scope="module")
def pulp(tmp_path_factory):
    """The chart rendered with Pulp enabled and otherwise default values."""
    values = tmp_path_factory.mktemp("pulp") / "values.yaml"
    values.write_text(yaml.safe_dump({"pulp": {"enabled": True}}))
    return render_chart(values)


def deployment(manifests, name):
    return manifests["Deployment", f"{PULP}-{name}"]


def pulp_objects(manifests):
    return {key for key in manifests if "pulp" in key[1]}


def settings_py(manifests, name=f"{PULP}-config"):
    return manifests["ConfigMap", name]["data"]["settings.py"]


def test_disabled_by_default_renders_nothing(manifests, pulp):
    assert not pulp_objects(manifests)
    # Enabling Pulp adds objects but leaves the Foreman objects unchanged.
    for key, manifest in manifests.items():
        assert pulp[key] == manifest, key


def test_enabled_renders_workloads(pulp):
    assert pulp_objects(pulp) == {
        ("ConfigMap", f"{PULP}-config"),
        ("ConfigMap", f"{PULP}-migration-config"),
        ("Job", f"{PULP}-migrate-1"),
        ("Deployment", f"{PULP}-api"),
        ("Deployment", f"{PULP}-content"),
        ("Deployment", f"{PULP}-worker"),
        ("Service", f"{PULP}-api"),
        ("Service", f"{PULP}-content"),
        ("PersistentVolumeClaim", f"{PULP}-storage"),
        ("NetworkPolicy", f"{PULP}-api"),
    }
    for name, command in (("api", "pulp-api"), ("content", "pulp-content"),
                          ("worker", "pulp-worker")):
        (container,) = pod_spec(deployment(pulp, name))["containers"]
        assert container["command"] == [command]
        assert container["image"] == "quay.io/foreman/pulp:foreman-5.0"


def test_migration_hook_runs_before_workloads(pulp):
    job = pulp["Job", f"{PULP}-migrate-1"]
    config = pulp["ConfigMap", f"{PULP}-migration-config"]
    for hook in (job, config):
        annotations = hook["metadata"]["annotations"]
        assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert int(config["metadata"]["annotations"]["helm.sh/hook-weight"]) < int(
        job["metadata"]["annotations"]["helm.sh/hook-weight"])
    script = pod_spec(job)["containers"][0]["command"][-1]
    # Django refuses to migrate until /var/lib/pulp/tmp exists.
    assert script.index("mkdir -p /var/lib/pulp/tmp /var/lib/pulp/assets /var/lib/pulp/media") \
        < script.index("pulpcore-manager migrate --noinput") \
        < script.index("pulpcore-manager reset-admin-password --random >/dev/null")
    projected = [v for v in pod_spec(job)["volumes"] if "projected" in v]
    assert projected[0]["projected"]["sources"][0]["configMap"]["name"] == \
        config["metadata"]["name"]
    for name in RUNTIME:
        assert "helm.sh/hook" not in deployment(pulp, name)["metadata"].get("annotations", {})


def test_runtime_pods_share_the_claim(pulp):
    claim = pulp["PersistentVolumeClaim", f"{PULP}-storage"]
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"
    assert "storageClassName" not in claim["spec"]
    assert claim["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    for name in RUNTIME:
        spec = pod_spec(deployment(pulp, name))
        (storage,) = [v for v in spec["volumes"] if v["name"] == "storage"]
        assert storage["persistentVolumeClaim"]["claimName"] == f"{PULP}-storage"
        for container in containers(deployment(pulp, name)):
            mounts = {m["mountPath"]: m["name"] for m in container["volumeMounts"]}
            assert mounts["/var/lib/pulp"] == "storage"
        (init,) = spec["initContainers"]
        assert "/var/lib/pulp/tmp" in init["command"][-1]


def test_existing_claim_and_storage_class(render):
    manifests = render({"pulp": {"enabled": True, "storage": {"existingClaim": "content"}}})
    assert not manifests.of_kind("PersistentVolumeClaim")
    for name in RUNTIME:
        (storage,) = [v for v in pod_spec(deployment(manifests, name))["volumes"]
                      if v["name"] == "storage"]
        assert storage["persistentVolumeClaim"]["claimName"] == "content"
    manifests = render({"pulp": {"enabled": True, "storage": {
        "storageClass": "fast", "size": "50Gi", "accessMode": "ReadWriteMany"}}})
    claim = manifests["PersistentVolumeClaim", f"{PULP}-storage"]["spec"]
    assert claim["storageClassName"] == "fast"
    assert claim["accessModes"] == ["ReadWriteMany"]
    assert claim["resources"]["requests"]["storage"] == "50Gi"


def test_pods_follow_the_api_pod(pulp, render):
    api_labels = deployment(pulp, "api")["spec"]["template"]["metadata"]["labels"]
    for name in RUNTIME:
        affinity = pod_spec(deployment(pulp, name))["affinity"]["podAffinity"]
        (term,) = affinity["requiredDuringSchedulingIgnoredDuringExecution"]
        assert term["topologyKey"] == "kubernetes.io/hostname"
        assert term["labelSelector"]["matchLabels"].items() <= api_labels.items()
    manifests = render({"pulp": {"enabled": True, "storage": {"colocate": False}}})
    for name in RUNTIME:
        assert "affinity" not in pod_spec(deployment(manifests, name))


def test_security_context_and_resources(pulp):
    workloads = [pulp["Job", f"{PULP}-migrate-1"]] + [deployment(pulp, n) for n in RUNTIME]
    for workload in workloads:
        spec = pod_spec(workload)
        assert spec["automountServiceAccountToken"] is False
        assert spec["securityContext"]["runAsUser"] == 700
        assert spec["securityContext"]["fsGroup"] == 700
        assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        for container in containers(workload):
            context = container["securityContext"]
            assert context["runAsNonRoot"] is True
            assert context["runAsUser"] == 700
            assert context["allowPrivilegeEscalation"] is False
            assert context["capabilities"]["drop"] == ["ALL"]
            assert {"cpu", "memory"} <= container["resources"]["requests"].keys()


def test_secrets_come_from_existing_secret(render):
    manifests = render({"pulp": {"enabled": True, "existingSecret": "custom-pulp"}})
    workloads = [manifests["Job", f"{PULP}-migrate-1"]] + [
        deployment(manifests, n) for n in RUNTIME]
    for workload in workloads:
        (container,) = pod_spec(workload)["containers"]
        env = {e["name"]: e for e in container["env"]}
        for name, key in SECRET_ENV.items():
            assert "value" not in env[name], name
            assert env[name]["valueFrom"]["secretKeyRef"] == {"name": "custom-pulp", "key": key}
        (config,) = [v for v in pod_spec(workload)["volumes"] if v["name"] == "pulp-config"]
        (secret,) = [s["secret"] for s in config["projected"]["sources"] if "secret" in s]
        assert secret["name"] == "custom-pulp"
        assert secret["items"] == [{"key": "database_fields.symmetric.key",
                                    "path": "certs/database_fields.symmetric.key"}]
    assert not manifests.of_kind("Secret")
    for config in manifests.of_kind("ConfigMap"):
        for content in config["data"].values():
            assert "password" not in content.lower()
            assert "redis://" not in content


def test_settings_match_foremanctl(render):
    manifests = render({"foreman": {"fqdn": "foreman.example.org"}, "pulp": {
        "enabled": True, "database": {"host": "db.example.org"}}})
    settings = settings_py(manifests)
    assert settings == settings_py(manifests, f"{PULP}-migration-config")
    # No password-based authentication class is enabled.
    assert "BasicAuthentication" not in settings
    values = {}
    exec(compile(settings, "settings.py", "exec"), values)  # pylint: disable=exec-used
    assert values["DATABASES__default__HOST"] == "db.example.org"
    assert values["CONTENT_ORIGIN"] == "https://foreman.example.org"
    assert values["SMART_PROXY_PULP_URL"] == "https://foreman.example.org"
    assert values["ENABLED_PLUGINS"] == [
        "pulp_certguard", "pulp_file", "pulp_smart_proxy", "pulp_container", "pulp_rpm"]
    assert values["AUTHENTICATION_BACKENDS"] == [
        "pulpcore.app.authentication.PulpNoCreateRemoteUserBackend"]
    assert values["REMOTE_USER_ENVIRON_NAME"] == "HTTP_REMOTE_USER"
    assert values["REST_FRAMEWORK__DEFAULT_AUTHENTICATION_CLASSES"] == [
        "rest_framework.authentication.SessionAuthentication",
        "pulpcore.app.authentication.PulpRemoteUserAuthentication",
    ]


def test_settings_overrides(render):
    manifests = render({"pulp": {"enabled": True, "settings": {
        "contentOrigin": "https://content.example.org",
        "smartProxyPulpUrl": "https://foreman-httpd.foreman.svc",
        "extra": "ALLOWED_EXPORT_PATHS = []"}}})
    lines = settings_py(manifests).splitlines()
    assert 'CONTENT_ORIGIN = "https://content.example.org"' in lines
    assert 'SMART_PROXY_PULP_URL = "https://foreman-httpd.foreman.svc"' in lines
    assert lines[-1] == "ALLOWED_EXPORT_PATHS = []"


def test_services_and_probes(pulp):
    for name, port, path in (("api", 24817, "/pulp/api/v3/status/"),
                             ("content", 24816, "/pulp/content/")):
        service = pulp["Service", f"{PULP}-{name}"]["spec"]
        assert service["type"] == "ClusterIP"
        (service_port,) = service["ports"]
        assert service_port["port"] == port
        pod = deployment(pulp, name)["spec"]["template"]["metadata"]["labels"]
        assert service["selector"].items() <= pod.items()
        (container,) = pod_spec(deployment(pulp, name))["containers"]
        assert {p["name"]: p["containerPort"] for p in container["ports"]} == {"http": port}
        for probe in ("startupProbe", "readinessProbe"):
            assert container[probe]["httpGet"] == {"path": path, "port": "http"}
        assert container["livenessProbe"]["tcpSocket"] == {"port": "http"}


def test_selectors_do_not_overlap(pulp):
    """Each Service and Deployment selects only its own pods."""
    pods = {name: d["spec"]["template"]["metadata"]["labels"]
            for (kind, name), d in pulp.items() if kind == "Deployment"}
    for (kind, name), manifest in pulp.items():
        if kind == "Service":
            selector = manifest["spec"]["selector"]
        elif kind == "Deployment":
            selector = manifest["spec"]["selector"]["matchLabels"]
        else:
            continue
        matched = [pod for pod, labels in pods.items() if selector.items() <= labels.items()]
        assert matched == [name], (kind, name)


def test_api_network_policy(pulp, render):
    policy = pulp["NetworkPolicy", f"{PULP}-api"]["spec"]
    api_labels = deployment(pulp, "api")["spec"]["template"]["metadata"]["labels"]
    assert policy["podSelector"]["matchLabels"].items() <= api_labels.items()
    assert policy["policyTypes"] == ["Ingress"]
    (rule,) = policy["ingress"]
    (peer,) = rule["from"]
    assert peer["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/component": "httpd", "app.kubernetes.io/instance": "foreman"}
    assert rule["ports"] == [{"port": "http", "protocol": "TCP"}]
    manifests = render({"pulp": {"enabled": True, "api": {"networkPolicy": {"enabled": False}}}})
    assert not manifests.of_kind("NetworkPolicy")
