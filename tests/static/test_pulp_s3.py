"""Static assertions on Pulp content in S3 (pulp.storage.type s3) and the
extra containers, volumes and environment of the Pulp workloads."""

import ast
import subprocess

import pytest
import yaml

from conftest import CHART, RELEASE, render_chart
from test_pulp import PULP, RUNTIME, deployment, pulp_objects, settings_py
from test_render import pod_spec

S3 = {"bucket": "pulp", "endpointUrl": "https://s3.example.test", "region": "eu-central-1",
      "addressingStyle": "path", "existingSecret": "pulp-s3"}
EXTRAS = {
    "extraInitContainers": [{"name": "install", "image": "example.test/install:1"}],
    "extraVolumes": [{"name": "extra", "emptyDir": {}}],
    "extraVolumeMounts": [{"name": "extra", "mountPath": "/opt/extra", "readOnly": True}],
    "extraEnv": [{"name": "PYTHONPATH", "value": "/opt/extra"}],
}


def s3_values(**s3):
    return {"pulp": {"enabled": True, "storage": {"type": "s3", "s3": {**S3, **s3}}}}


def workloads(manifests):
    """Pod specs of the API, content and worker Deployments and the migration Job."""
    specs = {name: pod_spec(deployment(manifests, name)) for name in RUNTIME}
    specs["migrate"] = manifests["Job", f"{PULP}-migrate-1"]["spec"]["template"]["spec"]
    return specs


def settings(manifests, name=f"{PULP}-config"):
    """settings.py as a dict of literal values; fails if it is not valid Python."""
    tree = ast.parse(settings_py(manifests, name))
    return {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body}


@pytest.fixture(scope="module")
def s3(tmp_path_factory):
    values = tmp_path_factory.mktemp("pulp-s3") / "values.yaml"
    values.write_text(yaml.safe_dump(s3_values()))
    return render_chart(values)


def test_filesystem_is_the_default(manifests, render):
    explicit = render({"pulp": {"enabled": True, "storage": {"type": "filesystem"}}})
    default = render({"pulp": {"enabled": True}})
    assert explicit == default
    assert ("PersistentVolumeClaim", f"{PULP}-storage") in default
    assert "STORAGES" not in settings(default)
    # Without Pulp nothing changes.
    assert not pulp_objects(manifests)


def test_s3_settings_follow_the_pulp_documentation(s3):
    for name in (f"{PULP}-config", f"{PULP}-migration-config"):
        values = settings(s3, name)
        assert values["MEDIA_ROOT"] == ""
        assert values["STORAGES"] == {
            "default": {
                "BACKEND": "storages.backends.s3.S3Storage",
                "OPTIONS": {
                    "bucket_name": "pulp",
                    "endpoint_url": "https://s3.example.test",
                    "region_name": "eu-central-1",
                    "addressing_style": "path",
                    "signature_version": "s3v4",
                },
            },
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        }
        assert values["REDIRECT_TO_OBJECT_STORAGE"] is False
        # The credentials are not in the settings.
        assert "access_key" not in settings_py(s3, name)


def test_s3_has_no_claim_and_no_affinity(s3):
    assert not s3.of_kind("PersistentVolumeClaim")
    for name, spec in workloads(s3).items():
        assert "affinity" not in spec, name
        (storage,) = [v for v in spec["volumes"] if v["name"] == "storage"]
        assert storage == {"name": "storage", "emptyDir": {}}, name


def test_s3_credentials_only_from_the_secret(s3):
    for name, spec in workloads(s3).items():
        (container,) = spec["containers"]
        env = {e["name"]: e for e in container["env"]}
        for variable, key in (("AWS_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"),
                              ("AWS_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")):
            assert env[variable] == {"name": variable, "valueFrom": {
                "secretKeyRef": {"name": "pulp-s3", "key": key}}}, name


def test_s3_without_secret_sets_no_credentials(render):
    manifests = render(s3_values(existingSecret=""))
    for name, spec in workloads(manifests).items():
        names = {e["name"] for e in spec["containers"][0]["env"]}
        assert not names & {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}, name


def test_s3_secret_key_names(render):
    manifests = render(s3_values(accessKeySecretKey="id", secretKeySecretKey="secret"))
    env = {e["name"]: e for e in pod_spec(deployment(manifests, "api"))["containers"][0]["env"]}
    assert env["AWS_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"]["key"] == "id"
    assert env["AWS_SECRET_ACCESS_KEY"]["valueFrom"]["secretKeyRef"]["key"] == "secret"


def test_s3_ca_bundle_and_verify(render):
    manifests = render(s3_values(caSecret={"name": "s3-ca", "key": "bundle.pem"}))
    assert settings(manifests)["STORAGES"]["default"]["OPTIONS"]["verify"] == \
        "/etc/pulp/certs/s3-ca.crt"
    for name, spec in workloads(manifests).items():
        (config,) = [v for v in spec["volumes"] if v["name"] == "pulp-config"]
        assert {"secret": {"name": "s3-ca", "items": [
            {"key": "bundle.pem", "path": "certs/s3-ca.crt"}]}} in config["projected"]["sources"]
    manifests = render(s3_values(verify=False))
    assert settings(manifests)["STORAGES"]["default"]["OPTIONS"]["verify"] is False


def test_s3_options_and_redirect(render):
    manifests = render(s3_values(
        redirectToObjectStorage=True, endpointUrl="", region="", addressingStyle="",
        options={"location": "pulp", "default_acl": None, "file_overwrite": False,
                 "object_parameters": {"CacheControl": "max-age=60"}}))
    values = settings(manifests)
    assert values["REDIRECT_TO_OBJECT_STORAGE"] is True
    assert values["STORAGES"]["default"]["OPTIONS"] == {
        "bucket_name": "pulp", "signature_version": "s3v4", "location": "pulp",
        "default_acl": None, "file_overwrite": False,
        "object_parameters": {"CacheControl": "max-age=60"}}


@pytest.mark.parametrize("storage, message", [
    ({"type": "s3"}, "pulp.storage.s3.bucket is required"),
    ({"type": "nfs"}, "pulp.storage.type must be filesystem or s3"),
    ({"type": "s3", "s3": {"bucket": "b", "verify": False, "caSecret": {"name": "ca"}}},
     "exclude each other"),
])
def test_invalid_storage_fails(tmp_path, storage, message):
    values = tmp_path / "values.yaml"
    values.write_text(yaml.safe_dump({"pulp": {"enabled": True, "storage": storage}}))
    result = subprocess.run(["helm", "template", RELEASE, str(CHART), "--values", str(values)],
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert message in result.stderr


@pytest.mark.parametrize("key", [
    "access_key", "secret_key", "security_token", "session_token", "session_profile"])
def test_credentials_in_s3_options_fail(tmp_path, key):
    """S3 options are rendered into the settings ConfigMap, so credentials must not be there."""
    values = tmp_path / "values.yaml"
    values.write_text(yaml.safe_dump(s3_values(options={key: "s3cr3t"})))
    result = subprocess.run(["helm", "template", RELEASE, str(CHART), "--values", str(values)],
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert f"pulp.storage.s3.options.{key} would be rendered into a ConfigMap" in result.stderr
    assert "pulp.storage.s3.existingSecret" in result.stderr
    assert "s3cr3t" not in result.stdout + result.stderr


@pytest.mark.parametrize("storage", ["filesystem", "s3"])
def test_extras_apply_to_all_pulp_workloads(render, storage):
    values = s3_values() if storage == "s3" else {"pulp": {"enabled": True}}
    values["pulp"].update(EXTRAS)
    for name, spec in workloads(render(values)).items():
        init = [c["name"] for c in spec.get("initContainers", [])]
        assert init[-1] == "install", name
        assert {"name": "extra", "emptyDir": {}} in spec["volumes"], name
        (container,) = spec["containers"]
        assert {"name": "extra", "mountPath": "/opt/extra", "readOnly": True} in \
            container["volumeMounts"], name
        assert container["env"][-1] == {"name": "PYTHONPATH", "value": "/opt/extra"}, name


def test_extras_leave_other_workloads_alone(render):
    values = {"pulp": {"enabled": True, **EXTRAS}}
    for (kind, name), manifest in render(values).items():
        if kind in ("Deployment", "Job") and "pulp" not in name:
            assert "/opt/extra" not in yaml.safe_dump(manifest), name
