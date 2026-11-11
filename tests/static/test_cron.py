"""Static assertions on the CronJobs that run Foreman's cron tasks."""

import pytest

from test_checksums import EVERYTHING, merged
from test_render import (
    FULLNAME, pod_spec,
    test_containers_use_restricted_security_context as check_security_context,
    test_resources_have_standard_labels as check_labels,
    test_workload_containers_request_cpu_and_memory as check_requests,
)

# The schedules of /etc/cron.d/foreman in the Foreman image.
SCHEDULES = {
    "hourly": "30 * * * *",
    "daily": "0 2 * * *",
    "weekly": "0 5 * * 0",
    "monthly": "0 3 1 * *",
}
MIGRATION = f"{FULLNAME}-migrate-1"
SEED_ENV = {"SEED_ADMIN_USER", "SEED_ADMIN_PASSWORD"}
DIGEST = "sha256:" + "a" * 64


def cron_jobs(manifests):
    """Period -> CronJob."""
    prefix = f"{FULLNAME}-cron-"
    return {doc["metadata"]["name"][len(prefix):]: doc for doc in manifests.of_kind("CronJob")}


def env(container):
    return {item["name"]: item for item in container["env"]}


def test_one_cronjob_per_period_by_default(manifests):
    jobs = cron_jobs(manifests)
    assert set(jobs) == set(SCHEDULES)
    for period, job in jobs.items():
        spec = job["spec"]
        assert spec["schedule"] == SCHEDULES[period]
        assert "timeZone" not in spec
        assert spec["concurrencyPolicy"] == "Forbid"
        assert spec["startingDeadlineSeconds"] == 1800
        assert spec["successfulJobsHistoryLimit"] == 1
        assert spec["failedJobsHistoryLimit"] == 3
        assert spec["jobTemplate"]["spec"]["backoffLimit"] == 0
        assert spec["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 1800
        pod = pod_spec(job)
        assert pod["restartPolicy"] == "Never"
        (container,) = pod["containers"]
        # One rake process: the pending migration check, then the period's tasks.
        assert container["command"] == [
            "foreman-rake", "db:abort_if_pending_migrations", f"cron:{period}"]
        labels = pod_spec_labels(job)
        assert labels["app.kubernetes.io/component"] == f"cron-{period}"
        assert job["metadata"]["labels"]["app.kubernetes.io/component"] == f"cron-{period}"


def pod_spec_labels(job):
    return job["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]


def test_schedule_settings_come_from_values(render):
    manifests = render({"cron": {
        "timeZone": "Europe/Prague",
        "periods": {"hourly": {"schedule": "5 * * * *"}, "monthly": {"schedule": "0 4 2 * *"}},
        "concurrencyPolicy": "Replace",
        "startingDeadlineSeconds": 600,
        "successfulJobsHistoryLimit": 2,
        "failedJobsHistoryLimit": 5,
        "backoffLimit": 1,
        "activeDeadlineSeconds": 900,
        "resources": {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"memory": "3Gi"}},
    }})
    jobs = cron_jobs(manifests)
    assert {period: job["spec"]["schedule"] for period, job in jobs.items()} == {
        **SCHEDULES, "hourly": "5 * * * *", "monthly": "0 4 2 * *"}
    for job in jobs.values():
        spec = job["spec"]
        assert spec["timeZone"] == "Europe/Prague"
        assert spec["concurrencyPolicy"] == "Replace"
        assert spec["startingDeadlineSeconds"] == 600
        assert spec["successfulJobsHistoryLimit"] == 2
        assert spec["failedJobsHistoryLimit"] == 5
        assert spec["jobTemplate"]["spec"]["backoffLimit"] == 1
        assert spec["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 900
        (container,) = pod_spec(job)["containers"]
        assert container["resources"] == {
            "requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"memory": "3Gi"}}


def test_disabled_renders_no_cronjob(render):
    assert not render({"cron": {"enabled": False}}).of_kind("CronJob")


def test_a_period_can_be_disabled(render):
    manifests = render({"cron": {"periods": {"weekly": {"enabled": False},
                                             "monthly": {"enabled": False}}}})
    assert set(cron_jobs(manifests)) == {"hourly", "daily"}


@pytest.mark.parametrize("values", [{}, EVERYTHING], ids=["default", "everything"])
def test_pods_match_the_migration_job(render, values):
    """Same image, pod and container security context, init containers and
    Foreman environment as the migration Job, without the seed credentials."""
    manifests = render(merged(values, {
        "image": {"digest": DIGEST}, "imagePullSecrets": [{"name": "pull"}]}))
    migration = pod_spec(manifests["Job", MIGRATION])
    (migrate,) = migration["containers"]
    migration_env = {name: item for name, item in env(migrate).items() if name not in SEED_ENV}
    jobs = cron_jobs(manifests)
    assert set(jobs) == set(SCHEDULES)
    for period, job in jobs.items():
        pod = pod_spec(job)
        (cron,) = pod["containers"]
        assert cron["image"] == migrate["image"] == f"quay.io/foreman/foreman@{DIGEST}", period
        assert cron["imagePullPolicy"] == migrate["imagePullPolicy"]
        assert pod["securityContext"] == migration["securityContext"]
        assert pod["imagePullSecrets"] == migration["imagePullSecrets"] == [{"name": "pull"}]
        assert cron["securityContext"] == migrate["securityContext"]
        assert pod.get("initContainers") == migration.get("initContainers")
        assert env(cron) == migration_env, period
        assert not SEED_ENV & env(cron).keys()


@pytest.mark.parametrize("values", [{}, EVERYTHING], ids=["default", "everything"])
def test_pods_read_the_web_configuration(render, values):
    """The migration ConfigMap is a hook resource that Helm deletes, so the
    pods mount the web pods' ConfigMap, certificates and katello.yaml."""
    manifests = render(merged(values, {"foreman": {"hostAliases": [
        {"ip": "192.0.2.10", "hostnames": ["proxy.example.test"]}]}}))
    web = pod_spec(manifests["Deployment", FULLNAME])
    web_container = next(c for c in web["containers"] if c["name"] == "foreman")
    web_mounts = [m for m in web_container["volumeMounts"] if m["name"] != "oauth-config"]
    web_volumes = [v for v in web["volumes"] if v["name"] != "oauth-config"]
    for period, job in cron_jobs(manifests).items():
        pod = pod_spec(job)
        (cron,) = pod["containers"]
        assert cron["volumeMounts"] == web_mounts, period
        assert pod["volumes"] == web_volumes, period
        assert pod["hostAliases"] == web["hostAliases"]
        assert env(cron) == env(web_container), period
        configuration = next(v for v in pod["volumes"] if v["name"] == "configuration")
        assert configuration["configMap"]["name"] == f"{FULLNAME}-config"


def test_katello_pods_mount_katello_yaml_and_certificates(render):
    manifests = render(EVERYTHING)
    for period, job in cron_jobs(manifests).items():
        (init,) = pod_spec(job)["initContainers"]
        assert init["name"] == "katello-config"
        (cron,) = pod_spec(job)["containers"]
        mounts = {m["mountPath"] for m in cron["volumeMounts"]}
        assert "/etc/foreman/plugins/katello.yaml" in mounts, period
        assert "/etc/foreman/certs" in mounts, period
        assert "katello" in env(cron)["FOREMAN_ENABLED_PLUGINS"]["value"].split()


def test_cronjobs_are_restricted_labelled_and_request_resources(render):
    manifests = render(EVERYTHING)
    assert len(manifests.of_kind("CronJob")) == 4
    check_security_context(manifests)
    check_labels(manifests)
    check_requests(manifests)


def test_long_release_names_fit_job_names(render):
    """The CronJob controller appends 11 characters to name its Jobs."""
    release = "r" * 53
    manifests = render({"fullnameOverride": "f" * 63}, release=release)
    names = [job["metadata"]["name"] for job in manifests.of_kind("CronJob")]
    assert len(names) == 4 and len(set(names)) == 4
    for name in names:
        assert len(name) <= 52, name
        assert name.split("-")[-1] in SCHEDULES, name
