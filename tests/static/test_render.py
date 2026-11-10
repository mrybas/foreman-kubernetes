"""Static assertions on the rendered foreman Helm chart."""

FULLNAME = "foreman-foreman"
SECRET_ENV = {
    "DATABASE_URL", "ENCRYPTION_KEY", "SECRET_KEY_BASE",
    "SEED_ADMIN_USER", "SEED_ADMIN_PASSWORD",
}


def pod_spec(manifest):
    """Return the pod spec of a Pod, Deployment or Job."""
    if manifest["kind"] == "Pod":
        return manifest["spec"]
    return manifest["spec"]["template"]["spec"]


def containers(manifest):
    spec = pod_spec(manifest)
    return spec.get("initContainers", []) + spec["containers"]


def test_containers_use_restricted_security_context(manifests):
    workloads = manifests.of_kind("Deployment", "Job", "Pod")
    assert workloads
    for workload in workloads:
        pod = pod_spec(workload).get("securityContext", {})
        for container in containers(workload):
            where = f"{workload['kind']}/{workload['metadata']['name']}/{container['name']}"
            context = container.get("securityContext", {})
            assert context.get("runAsNonRoot", pod.get("runAsNonRoot")) is True, where
            assert context.get("allowPrivilegeEscalation") is False, where
            assert context.get("capabilities", {}).get("drop") == ["ALL"], where
            seccomp = context.get("seccompProfile", pod.get("seccompProfile", {}))
            assert seccomp.get("type") == "RuntimeDefault", where


def test_workload_containers_request_cpu_and_memory(manifests):
    for workload in manifests.of_kind("Deployment", "Job"):
        for container in containers(workload):
            requests = container.get("resources", {}).get("requests", {})
            assert {"cpu", "memory"} <= requests.keys(), container["name"]


def test_resources_have_standard_labels(manifests):
    for (kind, name), manifest in manifests.items():
        labels = manifest["metadata"].get("labels", {})
        assert labels.get("app.kubernetes.io/name") == "foreman", (kind, name)
        assert labels.get("app.kubernetes.io/instance") == "foreman", (kind, name)
        assert labels.get("app.kubernetes.io/managed-by") == "Helm", (kind, name)
        assert labels.get("helm.sh/chart", "").startswith("foreman-"), (kind, name)


def test_selectors_match_web_pod_labels(manifests):
    deployment = manifests["Deployment", FULLNAME]
    pod_labels = deployment["spec"]["template"]["metadata"]["labels"]
    selector = deployment["spec"]["selector"]["matchLabels"]
    assert selector.items() <= pod_labels.items()
    service_selector = manifests["Service", FULLNAME]["spec"]["selector"]
    assert service_selector.items() <= pod_labels.items()


def test_migration_job_runs_before_deployment(manifests):
    job = manifests["Job", f"{FULLNAME}-migrate-1"]
    config = manifests["ConfigMap", f"{FULLNAME}-migration-config"]
    for hook in (job, config):
        annotations = hook["metadata"]["annotations"]
        assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    # The job mounts the hook ConfigMap, so the ConfigMap must be created first.
    assert int(config["metadata"]["annotations"]["helm.sh/hook-weight"]) < int(
        job["metadata"]["annotations"]["helm.sh/hook-weight"])
    mounted = [v["configMap"]["name"] for v in pod_spec(job)["volumes"] if "configMap" in v]
    assert mounted == [config["metadata"]["name"]]
    (migrate,) = pod_spec(job)["containers"]
    assert "bin/rails db:migrate" in " ".join(migrate["command"])
    # The Deployment is a regular release resource, applied after pre-install hooks.
    deployment = manifests["Deployment", FULLNAME]
    assert "helm.sh/hook" not in deployment["metadata"].get("annotations", {})


def test_secrets_come_from_existing_secret(render):
    manifests = render({"foreman": {"existingSecret": "custom-runtime"}})
    found = set()
    for workload in manifests.of_kind("Deployment", "Job"):
        for container in containers(workload):
            for env in container.get("env", []):
                if env["name"] in SECRET_ENV:
                    found.add(env["name"])
                    assert "value" not in env, env["name"]
                    assert env["valueFrom"]["secretKeyRef"]["name"] == "custom-runtime"
    assert found == SECRET_ENV


def test_no_secret_material_is_rendered(manifests):
    assert not manifests.of_kind("Secret")
    for config in manifests.of_kind("ConfigMap"):
        data = config["data"]
        assert "url: <%= ENV.fetch('DATABASE_URL') %>" in data["database.yml"]
        for content in data.values():
            assert "postgresql://" not in content
            assert "password" not in content.lower()


def test_image_is_repository_and_tag(render):
    manifests = render({"image": {"repository": "registry.example.test/foreman", "tag": "3.99"}})
    for workload in manifests.of_kind("Deployment", "Job"):
        for container in containers(workload):
            assert container["image"] == "registry.example.test/foreman:3.99"


def test_service_targets_web_container_port(render):
    manifests = render({"service": {"port": 8080}})
    (port,) = manifests["Service", FULLNAME]["spec"]["ports"]
    assert port["port"] == 8080
    (web,) = pod_spec(manifests["Deployment", FULLNAME])["containers"]
    named = {p["name"]: p["containerPort"] for p in web["ports"]}
    assert port["targetPort"] in named
    assert named[port["targetPort"]] == 3000
