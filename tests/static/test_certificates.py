"""Static assertions on the certificate Secrets contract of the foreman Helm chart."""

import yaml

from test_render import FULLNAME, containers, pod_spec

CERTS_DIR = "/etc/foreman/certs"
SSL_SETTINGS = {":ssl_ca_file", ":ssl_certificate", ":ssl_priv_key"}
WEB = ("Deployment", FULLNAME)
MIGRATION = ("Job", f"{FULLNAME}-migrate-1")
CONFIGS = {WEB: f"{FULLNAME}-config", MIGRATION: f"{FULLNAME}-migration-config"}


def volume(manifest, name):
    matches = [v for v in pod_spec(manifest).get("volumes", []) if v["name"] == name]
    return matches[0] if matches else None


def mounted_files(manifest, volume_name):
    """Map file name -> (Secret name, key) for a projected volume of Secrets."""
    files = {}
    for source in volume(manifest, volume_name)["projected"]["sources"]:
        secret = source["secret"]
        for item in secret["items"]:
            files[item["path"]] = (secret["name"], item["key"])
    return files


def settings(manifests, name):
    return yaml.safe_load(manifests["ConfigMap", name]["data"]["settings.yaml"])


def mounted_paths(manifest):
    """Absolute paths of the certificate files the workload's container sees."""
    if volume(manifest, "certificates") is None:
        return set()
    (container,) = pod_spec(manifest)["containers"]
    (mount,) = [m for m in container["volumeMounts"] if m["name"] == "certificates"]
    return {f"{mount['mountPath']}/{path}" for path in mounted_files(manifest, "certificates")}


def test_certificates_are_disabled_by_default(manifests):
    for workload in manifests.of_kind("Deployment", "Job", "CronJob", "Pod"):
        assert volume(workload, "certificates") is None, workload["metadata"]["name"]
        for container in containers(workload):
            paths = [m["mountPath"] for m in container.get("volumeMounts", [])]
            assert not any(p.startswith(CERTS_DIR) for p in paths), container["name"]
    for config in CONFIGS.values():
        assert not SSL_SETTINGS & settings(manifests, config).keys()


def test_enabled_mounts_secrets_read_only(render):
    manifests = render({"certificates": {"enabled": True}})
    web = manifests[WEB]
    assert mounted_files(web, "certificates") == {
        "ca.crt": ("foreman-ca", "ca.crt"),
        "client.crt": ("foreman-client-tls", "tls.crt"),
        "client.key": ("foreman-client-tls", "tls.key"),
    }
    (container,) = pod_spec(web)["containers"]
    (mount,) = [m for m in container["volumeMounts"] if m["name"] == "certificates"]
    assert mount == {"name": "certificates", "mountPath": CERTS_DIR, "readOnly": True}
    # The migration Job makes no outgoing TLS connections and mounts nothing.
    for name in (MIGRATION, ("Pod", f"{FULLNAME}-smoke-test")):
        assert volume(manifests[name], "certificates") is None, name


def test_certificate_files_are_readable_by_the_foreman_group_only(render):
    manifests = render({"certificates": {"enabled": True}})
    # Secret volume files are owned by root and by the pod fsGroup. With 0440
    # and fsGroup equal to the container group, the foreman user can read the
    # client key and nobody else can.
    assert volume(manifests[WEB], "certificates")["projected"]["defaultMode"] == 0o440
    for key in (WEB, MIGRATION):
        pod = pod_spec(manifests[key])
        (container,) = pod["containers"]
        group = container.get("securityContext", {}).get(
            "runAsGroup", pod["securityContext"].get("runAsGroup"))
        assert group is not None, key
        assert pod["securityContext"].get("fsGroup") == group, key


def test_secret_names_and_ca_key_are_configurable(render):
    manifests = render({"certificates": {
        "enabled": True,
        "ca": {"secretName": "corporate-ca", "key": "ca-bundle.crt"},
        "client": {"secretName": "foreman-client"},
    }})
    assert mounted_files(manifests[WEB], "certificates") == {
        "ca.crt": ("corporate-ca", "ca-bundle.crt"),
        "client.crt": ("foreman-client", "tls.crt"),
        "client.key": ("foreman-client", "tls.key"),
    }


def test_settings_point_at_mounted_files(render):
    manifests = render({"certificates": {"enabled": True}})
    assert settings(manifests, CONFIGS[WEB]).keys() >= SSL_SETTINGS
    assert not SSL_SETTINGS & settings(manifests, CONFIGS[MIGRATION]).keys()
    # Each workload's settings may only name files mounted in that workload.
    for key, config in CONFIGS.items():
        values = settings(manifests, config)
        configured = {values[name] for name in SSL_SETTINGS if name in values}
        assert configured <= mounted_paths(manifests[key]), key


def test_no_certificate_material_is_rendered(render):
    manifests = render({"certificates": {"enabled": True}})
    assert not manifests.of_kind("Secret")
    for config in manifests.of_kind("ConfigMap"):
        for content in config["data"].values():
            assert "PRIVATE KEY" not in content
            assert "BEGIN CERTIFICATE" not in content
    for workload in manifests.of_kind("Deployment", "Job"):
        for entry in pod_spec(workload)["volumes"]:
            for source in entry.get("projected", {}).get("sources", []):
                assert "configMap" not in source, workload["metadata"]["name"]
