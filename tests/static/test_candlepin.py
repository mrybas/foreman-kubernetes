"""Static assertions on the Candlepin workload of the foreman Helm chart."""

import xml.etree.ElementTree as ET

import pytest

from conftest import render_chart
from test_render import FULLNAME, containers, pod_spec

CANDLEPIN = f"{FULLNAME}-candlepin"
DEPLOYMENT = ("Deployment", CANDLEPIN)
SERVICE = ("Service", CANDLEPIN)
CONFIG = ("ConfigMap", f"{CANDLEPIN}-config")
MIGRATION = ("Job", f"{CANDLEPIN}-migrate-1")
CERTS_DIR = "/etc/candlepin/tls"
SECRET_ENV = {"JDBC_URL", "JDBC_USERNAME", "JDBC_PASSWORD", "KATELLO_OAUTH_SECRET"}


@pytest.fixture(scope="module")
def enabled(tmp_path_factory):
    values = tmp_path_factory.mktemp("candlepin") / "values.yaml"
    values.write_text("candlepin:\n  enabled: true\n")
    return render_chart(values)


def candlepin_container(manifests):
    (container,) = pod_spec(manifests[DEPLOYMENT])["containers"]
    return container


def properties(text):
    """Parse candlepin.conf (Java properties without continuation lines)."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def test_disabled_by_default_renders_nothing_new(manifests, enabled):
    assert not [key for key in manifests if "candlepin" in key[1]]
    for doc in manifests.values():
        assert "candlepin" not in str(doc)
    # Enabling Candlepin adds its own objects and leaves the rest unchanged.
    added = set(enabled) - set(manifests)
    assert added == {DEPLOYMENT, SERVICE, CONFIG, MIGRATION}
    for key, doc in manifests.items():
        assert enabled[key] == doc, key


def test_single_replica_recreated(enabled):
    spec = enabled[DEPLOYMENT]["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"] == {"type": "Recreate"}


def test_selectors_do_not_overlap_with_foreman(enabled):
    candlepin_pod = enabled[DEPLOYMENT]["spec"]["template"]["metadata"]["labels"]
    assert candlepin_pod["app.kubernetes.io/name"] == "foreman"
    assert candlepin_pod["app.kubernetes.io/component"] == "candlepin"
    selector = enabled[DEPLOYMENT]["spec"]["selector"]["matchLabels"]
    assert selector.items() <= candlepin_pod.items()
    assert enabled[SERVICE]["spec"]["selector"].items() <= candlepin_pod.items()
    web_pod = enabled["Deployment", FULLNAME]["spec"]["template"]["metadata"]["labels"]
    for foreman_selector in (
        enabled["Deployment", FULLNAME]["spec"]["selector"]["matchLabels"],
        enabled["Service", FULLNAME]["spec"]["selector"],
    ):
        assert not foreman_selector.items() <= candlepin_pod.items()
        assert foreman_selector.items() <= web_pod.items()
    # Migration pods (no TLS listener) must not become Service endpoints.
    job_pod = enabled[MIGRATION]["spec"]["template"]["metadata"]["labels"]
    assert not enabled[SERVICE]["spec"]["selector"].items() <= job_pod.items()
    assert not selector.items() <= job_pod.items()


def test_restricted_security_context_with_tomcat_ids(enabled):
    for key in (DEPLOYMENT, MIGRATION):
        pod = pod_spec(enabled[key])
        assert pod["securityContext"]["runAsUser"] == 53
        assert pod["securityContext"]["runAsGroup"] == 53
        assert pod["securityContext"]["fsGroup"] == 53
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
        for container in containers(enabled[key]):
            context = container["securityContext"]
            assert context["allowPrivilegeEscalation"] is False
            assert context["capabilities"]["drop"] == ["ALL"]
            assert context["runAsUser"] == 53
            assert container["resources"]["requests"]["memory"]
            assert container["resources"]["limits"]["memory"]


def test_service_container_and_tomcat_agree_on_port(enabled):
    (port,) = enabled[SERVICE]["spec"]["ports"]
    assert port["port"] == 23443
    named = {p["name"]: p["containerPort"] for p in candlepin_container(enabled)["ports"]}
    assert named[port["targetPort"]] == 23443
    server = ET.fromstring(enabled[CONFIG]["data"]["server.xml"])
    (connector,) = server.iter("Connector")
    assert connector.get("port") == "23443"
    assert connector.get("SSLEnabled") == "true"


def test_probes_use_status_over_https(enabled):
    container = candlepin_container(enabled)
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        check = container[probe]["httpGet"]
        assert check == {"path": "/candlepin/status", "port": "https", "scheme": "HTTPS"}, probe


def test_configuration_files_are_mounted_read_only(enabled):
    mounts = {m["mountPath"]: m for m in candlepin_container(enabled)["volumeMounts"]}
    expected = {
        "/etc/candlepin/candlepin.conf": "candlepin.conf",
        "/etc/tomcat/server.xml": "server.xml",
        "/etc/tomcat/tomcat.conf": "tomcat.conf",
        "/etc/tomcat/logging.properties": "logging.properties",
        "/var/lib/tomcat/webapps/candlepin/WEB-INF/classes/logback.xml": "logback.xml",
    }
    for path, key in expected.items():
        assert mounts[path] == {
            "name": "configuration", "mountPath": path, "subPath": key, "readOnly": True}
        assert key in enabled[CONFIG]["data"]
    assert mounts[CERTS_DIR]["readOnly"] is True
    # Only the Artemis journal is writable.
    writable = {p for p, m in mounts.items() if not m.get("readOnly")}
    assert writable == {"/var/lib/candlepin"}


def test_certificates_come_from_secrets(enabled):
    volumes = {v["name"]: v for v in pod_spec(enabled[DEPLOYMENT])["volumes"]}
    projected = volumes["certificates"]["projected"]
    assert projected["defaultMode"] == 0o440
    files = {
        item["path"]: (source["secret"]["name"], item["key"])
        for source in projected["sources"] for item in source["secret"]["items"]
    }
    assert files == {
        "ca.crt": ("foreman-candlepin-ca", "tls.crt"),
        "ca.key": ("foreman-candlepin-ca", "tls.key"),
        "tls.crt": ("foreman-candlepin-tls", "tls.crt"),
        "tls.key": ("foreman-candlepin-tls", "tls.key"),
    }
    conf = properties(enabled[CONFIG]["data"]["candlepin.conf"])
    assert conf["candlepin.ca_cert"] == f"{CERTS_DIR}/ca.crt"
    assert conf["candlepin.ca_key"] == f"{CERTS_DIR}/ca.key"
    server = ET.fromstring(enabled[CONFIG]["data"]["server.xml"])
    (host,) = server.iter("SSLHostConfig")
    assert host.get("caCertificateFile") == f"{CERTS_DIR}/ca.crt"
    (certificate,) = host.iter("Certificate")
    assert certificate.get("certificateFile") == f"{CERTS_DIR}/tls.crt"
    assert certificate.get("certificateKeyFile") == f"{CERTS_DIR}/tls.key"


def test_certificate_secret_names_are_configurable(render):
    manifests = render({
        "candlepin": {"enabled": True},
        "certificates": {"candlepin": {
            "ca": {"secretName": "corporate-candlepin-ca"},
            "server": {"secretName": "candlepin-server"},
        }},
    })
    volumes = {v["name"]: v for v in pod_spec(manifests[DEPLOYMENT])["volumes"]}
    names = [s["secret"]["name"] for s in volumes["certificates"]["projected"]["sources"]]
    assert names == ["corporate-candlepin-ca", "candlepin-server"]


def test_credentials_come_from_existing_secret(render):
    manifests = render({"candlepin": {
        "enabled": True, "existingSecret": "cp-secret", "databasePasswordSecretKey": "pw"}})
    found = {}
    for key in (DEPLOYMENT, MIGRATION):
        for container in containers(manifests[key]):
            for env in container["env"]:
                if env["name"] in SECRET_ENV:
                    assert "value" not in env, env["name"]
                    ref = env["valueFrom"]["secretKeyRef"]
                    assert ref["name"] == "cp-secret"
                    found[env["name"]] = ref["key"]
    assert found == {
        "JDBC_URL": "DATABASE_URL", "JDBC_USERNAME": "DATABASE_USERNAME",
        "JDBC_PASSWORD": "pw", "KATELLO_OAUTH_SECRET": "OAUTH_SECRET"}
    assert not manifests.of_kind("Secret")
    conf = manifests[CONFIG]["data"]["candlepin.conf"]
    values = properties(conf)
    assert values["jpa.config.hibernate.connection.password"] == "${JDBC_PASSWORD}"
    assert values["org.quartz.dataSource.myDS.password"] == "${JDBC_PASSWORD}"
    assert values["candlepin.auth.oauth.consumer.katello.secret"] == "${KATELLO_OAUTH_SECRET}"
    assert "postgresql://" not in conf


def test_migration_job_runs_before_foreman_and_halts_on_pending_changes(enabled):
    job = enabled[MIGRATION]
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert "hook-succeeded" in annotations["helm.sh/hook-delete-policy"]
    foreman = enabled["Job", f"{FULLNAME}-migrate-1"]["metadata"]["annotations"]
    foreman_config = enabled["ConfigMap", f"{FULLNAME}-migration-config"]["metadata"]["annotations"]
    weight = int(annotations["helm.sh/hook-weight"])
    assert weight < int(foreman["helm.sh/hook-weight"])
    assert weight < int(foreman_config["helm.sh/hook-weight"])
    # The Job needs no ConfigMap (hooks run before release resources exist).
    assert not [v for v in pod_spec(job).get("volumes", []) if "configMap" in v]
    (migrate,) = pod_spec(job)["containers"]
    script = " ".join(migrate["command"])
    assert "/usr/share/candlepin/liquibase.sh" in script
    assert "changelog-update.xml" in script and script.rstrip().endswith("update")
    assert "--password" not in script
    env = {e["name"]: e for e in migrate["env"]}
    assert env["LIQUIBASE_COMMAND_PASSWORD"]["value"] == "$(JDBC_PASSWORD)"
    # Candlepin itself does not migrate; it refuses to start on a stale schema.
    conf = properties(enabled[CONFIG]["data"]["candlepin.conf"])
    assert conf["candlepin.db.database_manage_on_startup"] == "Halt"
    deployment = enabled[DEPLOYMENT]
    assert "helm.sh/hook" not in deployment["metadata"].get("annotations", {})


def test_image_and_java_options_are_configurable(render):
    manifests = render({"candlepin": {
        "enabled": True,
        "image": {"repository": "registry.example.test/candlepin", "tag": "5.0"},
        "javaOpts": "-Xms1g -Xmx3g",
    }})
    for key in (DEPLOYMENT, MIGRATION):
        for container in containers(manifests[key]):
            assert container["image"] == "registry.example.test/candlepin:5.0"
    tomcat = manifests[CONFIG]["data"]["tomcat.conf"]
    assert 'JAVA_OPTS="-Xms1g -Xmx3g ' in tomcat


def test_resources_have_standard_labels(enabled):
    for (kind, name), manifest in enabled.items():
        if "candlepin" not in name:
            continue
        labels = manifest["metadata"]["labels"]
        assert labels["app.kubernetes.io/instance"] == "foreman", (kind, name)
        assert labels["app.kubernetes.io/component"] == "candlepin", (kind, name)
        assert labels["app.kubernetes.io/managed-by"] == "Helm", (kind, name)


@pytest.mark.parametrize("values, release, prefix", [
    ({"fullnameOverride": "corporate"}, "foreman", "corporate-candlepin"),
    ({}, "r" * 53, "r" * 53 + "-candlepin"),
])
def test_names_follow_fullname(render, values, release, prefix):
    manifests = render({"candlepin": {"enabled": True}, **values}, release=release)
    assert ("Deployment", prefix) in manifests
    assert ("Service", prefix) in manifests
    assert ("ConfigMap", f"{prefix}-config") in manifests
    (job,) = [name for kind, name in manifests if kind == "Job" and "candlepin" in name]
    assert len(job) <= 63, job
    assert job.endswith("-candlepin-migrate-1"), job


def test_pods_do_not_mount_service_account_token(enabled):
    for key in (DEPLOYMENT, MIGRATION):
        assert pod_spec(enabled[key])["automountServiceAccountToken"] is False, key
