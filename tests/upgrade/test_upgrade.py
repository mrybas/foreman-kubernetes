"""Upgrade the installed release to another Foreman image, through failed migrations.

The release must already be installed; `make test-upgrade` installs it with the
Foreman image UPGRADE_FROM_IMAGE_DIGEST. The target image is
UPGRADE_TO_IMAGE_DIGEST, or the digest that UPGRADE_TO_IMAGE_TAG (default
nightly) points to when the tests start, read from the registry. The tests then:

1. For each enabled migration Job (Foreman, Candlepin, Pulp), change the
   database password in the existing Secret that the Job reads. Running pods
   keep the credentials they started with, so only the new Job is affected.
   `helm upgrade` to the target image must fail on that Job, no
   Deployment or CronJob may change, and the running pods must keep answering the
   Foreman ping (and the Katello ping when Katello is enabled) during and
   after the attempt. The Secret is restored afterwards.
2. Run the same upgrade again. It must succeed, every migration Job must
   complete before the first new ReplicaSet is created, and the Foreman pods
   and the cron CronJobs must run the new image.
3. Upgrade with only a Foreman setting changed (foreman.externalUrl, rendered
   into settings.yaml). The Foreman web and Dynflow pods must be replaced and
   use the new setting; every other pod must stay. A second upgrade restores
   the installed values.

The values of the installed release are read with `helm get values` and
reused, so the tests work for any installation of the chart. Helm output of
every upgrade, and both image digests, are written to UPGRADE_LOG_DIR (default
artifacts/upgrade).
"""

import base64
import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import pytest
import yaml

# Fixtures and helpers shared with the integration tests.
from test_foreman import ADMIN_AUTH, WEB_HEADERS
from test_httpd import pki, port_forward  # noqa: F401
from test_katello import Api

pytestmark = pytest.mark.upgrade

NAMESPACE = os.environ.get("KIND_NAMESPACE", "foreman")
RELEASE = os.environ.get("HELM_RELEASE", "foreman")
TO_DIGEST = os.environ.get("UPGRADE_TO_IMAGE_DIGEST", "")
TO_TAG = os.environ.get("UPGRADE_TO_IMAGE_TAG", "nightly")
FROM_DIGEST = os.environ.get("UPGRADE_FROM_IMAGE_DIGEST", "")
TIMEOUT = os.environ.get("UPGRADE_HELM_TIMEOUT", "20m")
ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "foreman"
LOGS = Path(os.environ.get("UPGRADE_LOG_DIR", ROOT / "artifacts" / "upgrade"))
WRONG_PASSWORD = "wrong-password-from-upgrade-test"
FOREMAN_DEPLOYMENTS = (
    "foreman", "foreman-dynflow-orchestrator", "foreman-dynflow-worker",
    "foreman-dynflow-worker-hosts-queue")

@pytest.fixture(scope="module")
def namespace():
    return NAMESPACE


@pytest.fixture(scope="module")
def release():
    return RELEASE


def kubectl(*args):
    result = subprocess.run(["kubectl", "--namespace", NAMESPACE, *args, "--output", "json"],
                            capture_output=True, text=True, check=False, timeout=120)
    assert result.returncode == 0, f"kubectl {' '.join(args)}: {result.stderr}"
    return json.loads(result.stdout)


def helm(*args):
    result = subprocess.run(["helm", *args, "--namespace", NAMESPACE],
                            capture_output=True, text=True, check=False, timeout=120)
    assert result.returncode == 0, f"helm {' '.join(args)}: {result.stderr}"
    return result.stdout


def history():
    return json.loads(helm("history", RELEASE, "--output", "json"))


def upgrade(values_file, label):
    """helm upgrade to the target image with the installed values; log the result."""
    command = ["helm", "upgrade", RELEASE, str(CHART), "--namespace", NAMESPACE,
               "--values", str(values_file), "--set-string", f"image.digest={TO_DIGEST}",
               "--wait", "--wait-for-jobs", "--timeout", TIMEOUT]
    started = time.monotonic()
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    seconds = round(time.monotonic() - started)
    LOGS.mkdir(parents=True, exist_ok=True)
    report = (f"$ {' '.join(command)}\nrc={result.returncode} seconds={seconds}\n"
              f"--- stdout\n{result.stdout}\n--- stderr\n{result.stderr}\n"
              f"--- helm history\n{helm('history', RELEASE)}")
    (LOGS / f"{label}.log").write_text(report)
    print(report)
    return result


@pytest.fixture(scope="module")
def values_file(tmp_path_factory):
    """The values the release was installed with, before any upgrade."""
    path = tmp_path_factory.mktemp("upgrade") / "values.yaml"
    path.write_text(helm("get", "values", RELEASE, "--output", "yaml"))
    return path


@pytest.fixture(scope="module")
def computed():
    """All values of the installed release, chart defaults included."""
    return yaml.safe_load(helm("get", "values", RELEASE, "--all", "--output", "yaml"))


MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json")


def registry_digest(repository, tag):
    """Digest that a tag points to now, read from the registry without a pull."""
    registry, _, path = repository.partition("/")
    url = f"https://{registry}/v2/{path}/manifests/{tag}"
    headers = {"Accept": ", ".join(MANIFEST_TYPES)}
    try:
        response = urlopen(Request(url, method="HEAD", headers=headers), timeout=30)
    except HTTPError as error:
        if error.code != 401:
            raise
        # Anonymous token, as a container runtime gets one for a public image.
        challenge = dict(re.findall(r'(\w+)="([^"]*)"', error.headers["WWW-Authenticate"]))
        query = urlencode({key: challenge[key] for key in ("service", "scope") if key in challenge})
        with urlopen(f"{challenge['realm']}?{query}", timeout=30) as token_response:
            token = json.load(token_response)
        headers["Authorization"] = f"Bearer {token.get('token') or token['access_token']}"
        response = urlopen(Request(url, method="HEAD", headers=headers), timeout=30)
    with response:
        digest = response.headers["Docker-Content-Digest"]
    assert digest and digest.startswith("sha256:"), f"{url}: no digest ({digest!r})"
    return digest


@pytest.fixture(scope="module", autouse=True)
def target_image(computed):
    """Resolve the Foreman image to upgrade to once, and record both images."""
    global TO_DIGEST  # noqa: PLW0603 - read by upgrade() and the tests below
    repository = computed["image"]["repository"]
    source = "UPGRADE_TO_IMAGE_DIGEST"
    if not TO_DIGEST:
        TO_DIGEST = registry_digest(repository, TO_TAG)
        source = f"tag {TO_TAG}"
    assert TO_DIGEST != FROM_DIGEST, "the upgrade would not change the Foreman image"
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / "images.txt").write_text(
        f"from {repository}@{FROM_DIGEST or '(installed values)'}\n"
        f"to   {repository}@{TO_DIGEST} ({source})\n")
    return TO_DIGEST


def foreman_image(computed, digest):
    return f"{computed['image']['repository']}@{digest}"


def snapshot():
    """Deployments, CronJobs, ReplicaSets and running pods of the release."""
    selector = f"app.kubernetes.io/instance={RELEASE}"
    deployments = {
        item["metadata"]["name"]: (
            item["metadata"]["generation"],
            [container["image"] for container in item["spec"]["template"]["spec"]["containers"]])
        for item in kubectl("get", "deployments", "--selector", selector)["items"]}
    cron_jobs = {
        item["metadata"]["name"]: (
            item["metadata"]["generation"],
            [container["image"] for container
             in item["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]])
        for item in kubectl("get", "cronjobs", "--selector", selector)["items"]}
    replica_sets = {item["metadata"]["name"]: item["metadata"]["creationTimestamp"]
                    for item in kubectl("get", "replicasets", "--selector", selector)["items"]}
    pods = {
        item["metadata"]["uid"]: (
            item["metadata"]["name"],
            sum(status["restartCount"] for status in item["status"].get("containerStatuses", [])))
        for item in kubectl("get", "pods", "--selector", selector)["items"]
        if any(owner["kind"] == "ReplicaSet" for owner in item["metadata"].get("ownerReferences", []))}
    return {"deployments": deployments, "cronJobs": cron_jobs, "replicaSets": replica_sets,
            "pods": pods}


class Pinger(threading.Thread):
    """Run the checks every few seconds until stopped; record each outcome."""

    def __init__(self, checks, interval=3):
        super().__init__(daemon=True)
        self.checks = checks
        self.interval = interval
        self.results = []
        self.done = threading.Event()

    def run(self):
        while True:
            for name, check in self.checks.items():
                try:
                    check()
                    self.results.append((name, True, ""))
                except Exception as error:  # noqa: BLE001 - every failure counts
                    self.results.append((name, False, repr(error)))
            if self.done.wait(self.interval):
                return

    def stop(self):
        self.done.set()
        self.join(timeout=120)


# Port forwards bind to one pod, so each test opens its own after a rollout.
@pytest.fixture
def web_url(kube_cluster):
    with port_forward(kube_cluster, NAMESPACE, f"service/{RELEASE}-foreman", 3000) as (port,):
        yield f"http://127.0.0.1:{port}"


@pytest.fixture
def checks(request, computed, web_url):
    """Health checks the running release must pass while an upgrade fails."""

    def foreman_ping():
        with urlopen(Request(f"{web_url}/api/v2/ping", headers=WEB_HEADERS), timeout=20) as response:
            payload = json.load(response)
        assert payload["results"]["foreman"]["database"]["active"] is True, payload

    result = {"foreman ping": foreman_ping}
    if computed["katello"]["enabled"]:
        ports = request.getfixturevalue("front_door")
        api = Api(("127.0.0.1", *ports), request.getfixturevalue("pki"))

        def katello_ping():
            ping = api.get("/katello/api/ping")
            assert ping["status"] == "ok", ping

        result["katello ping"] = katello_ping
    return result


@pytest.fixture
def front_door(kube_cluster):
    with port_forward(kube_cluster, NAMESPACE, f"service/{RELEASE}-foreman-httpd", 80, 443) as ports:
        yield ports


def foreman_api(web_url, method, path, payload=None):
    request = Request(f"{web_url}{path}", method=method,
                      data=None if payload is None else json.dumps(payload).encode(),
                      headers={**WEB_HEADERS, "Authorization": ADMIN_AUTH,
                               "Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else None


# A Foreman object created before the upgrade; it must survive it.
RECORD = {}


def with_password(url, password):
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parts._replace(netloc=f"{parts.username}:{password}@{host}"))


# Component -> (enabled, Secret and key with the database credential, value
# with a wrong password).
MIGRATIONS = {
    "foreman": lambda v: (
        True, v["foreman"]["existingSecret"], v["foreman"]["databaseUrlSecretKey"],
        lambda url: with_password(url, WRONG_PASSWORD)),
    "candlepin": lambda v: (
        v["candlepin"]["enabled"], v["candlepin"]["existingSecret"],
        v["candlepin"]["databasePasswordSecretKey"], lambda _: WRONG_PASSWORD),
    "pulp": lambda v: (
        v["pulp"]["enabled"], v["pulp"]["existingSecret"], v["pulp"]["databasePasswordSecretKey"],
        lambda _: WRONG_PASSWORD),
}


def component_of(job):
    """Component of a migration Job, from its name (<...>-migrate-<revision>)."""
    for component in ("candlepin", "pulp"):
        if f"{component}-migrate-" in job:
            return component
    return "foreman"


def migration_jobs(revision):
    """Names of the migration Jobs of a revision that still exist, by component."""
    jobs = kubectl("get", "jobs", "--selector", f"app.kubernetes.io/instance={RELEASE}")["items"]
    names = [job["metadata"]["name"] for job in jobs
             if job["metadata"]["name"].endswith(f"-migrate-{revision}")]
    return {component_of(name): name for name in names}


def patch_secret(name, key, encoded):
    kubectl("patch", "secret", name, "--type", "merge", "--patch",
            json.dumps({"data": {key: encoded}}))


def test_installed_release_runs_the_old_image(computed, web_url):
    assert history()[-1]["status"] == "deployed"
    deployments = snapshot()["deployments"]
    expected = foreman_image(computed, FROM_DIGEST) if FROM_DIGEST else None
    for name in FOREMAN_DEPLOYMENTS:
        name = f"{RELEASE}-{name}"
        if name in deployments:
            (image,) = deployments[name][1]
            assert image != foreman_image(computed, TO_DIGEST), "already at the target image"
            if expected:
                assert image == expected
    name = f"upgrade-{uuid.uuid4().hex[:8]}"
    RECORD.update(foreman_api(web_url, "POST", "/api/v2/architectures",
                              {"architecture": {"name": name}}))
    assert RECORD["name"] == name


# The wrong password is written to the Secret the running pods also read. A
# Foreman, Dynflow, Candlepin or Pulp pod that restarts while it is there starts
# with the wrong password and fails; that is a flake of this test, not of the chart.
@pytest.mark.parametrize("component", list(MIGRATIONS))
def test_failed_migration_keeps_the_running_release(component, computed, values_file, checks):
    enabled, secret, key, broken = MIGRATIONS[component](computed)
    if not enabled:
        pytest.skip(f"{component} is not enabled")
    original = kubectl("get", "secret", secret)["data"][key]
    wrong = base64.b64encode(broken(base64.b64decode(original).decode()).encode()).decode()
    before = snapshot()
    pinger = Pinger(checks)
    pinger.start()
    patch_secret(secret, key, wrong)
    try:
        result = upgrade(values_file, f"failed-{component}-migration")
    finally:
        patch_secret(secret, key, original)
        time.sleep(10)
        pinger.stop()

    output = result.stdout + result.stderr
    revision = history()[-1]
    assert result.returncode != 0, output
    job = migration_jobs(revision["revision"]).get(component)
    assert job, f"no {component} migration Job left for revision {revision['revision']}"
    assert job in output, output
    assert revision["status"] == "failed", revision
    conditions = kubectl("get", "job", job)["status"].get("conditions", [])
    assert any(c["type"] == "Failed" and c["status"] == "True" for c in conditions), conditions

    assert snapshot() == before, "the failed upgrade changed Deployments, CronJobs or pods"
    failures = [entry for entry in pinger.results if not entry[1]]
    assert not failures, failures
    for name in checks:
        assert sum(1 for entry in pinger.results if entry[0] == name) >= 3, pinger.results
    for check in checks.values():
        check()


def migration_completions(revision):
    """Component -> time its migration Job of the revision completed.

    Helm deletes succeeded hook Jobs, so the Job controller events are read."""
    events = kubectl("get", "events", "--field-selector", "involvedObject.kind=Job,reason=Completed")
    times = {}
    for event in events["items"]:
        name = event["involvedObject"]["name"]
        if name.endswith(f"-migrate-{revision}"):
            stamp = event.get("lastTimestamp") or event.get("eventTime") or event["firstTimestamp"]
            times[component_of(name)] = parse_time(stamp)
    return times


def parse_time(stamp):
    """Kubernetes UTC timestamp, to the second."""
    return datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")


def test_upgrade_rolls_forward_after_the_fix(computed, values_file):
    before = snapshot()
    result = upgrade(values_file, "roll-forward")
    assert result.returncode == 0, result.stdout + result.stderr
    revision = history()[-1]
    assert revision["status"] == "deployed", revision

    after = snapshot()
    expected = foreman_image(computed, TO_DIGEST)
    for name in FOREMAN_DEPLOYMENTS:
        name = f"{RELEASE}-{name}"
        if name in after["deployments"]:
            assert after["deployments"][name][1] == [expected], name
    # The cron runs use the new image only after the migrations.
    for name, (_, images) in after["cronJobs"].items():
        assert images == [expected], name

    enabled = sorted(name for name, entry in MIGRATIONS.items() if entry(computed)[0])
    completed = migration_completions(revision["revision"])
    assert sorted(completed) == enabled, completed
    new_replica_sets = {name: parse_time(created) for name, created in after["replicaSets"].items()
                        if name not in before["replicaSets"]}
    assert any(name.startswith(f"{RELEASE}-foreman-") for name in new_replica_sets), new_replica_sets
    last_migration = max(completed.values())
    for name, created in new_replica_sets.items():
        assert created >= last_migration, (name, created, completed)


def test_release_answers_after_the_upgrade(checks):
    for check in checks.values():
        check()


def test_data_survives_the_upgrade(web_url):
    if not RECORD:
        pytest.skip("no record was created before the upgrade")
    found = foreman_api(web_url, "GET", f"/api/v2/architectures/{RECORD['id']}")
    assert found["name"] == RECORD["name"]
    foreman_api(web_url, "DELETE", f"/api/v2/architectures/{RECORD['id']}")


def deployment_state():
    """Deployment -> (generation, checksum/config, (UID, name) of its pods not terminating)."""
    selector = f"app.kubernetes.io/instance={RELEASE}"
    owners = {item["metadata"]["name"]: ref["name"]
              for item in kubectl("get", "replicasets", "--selector", selector)["items"]
              for ref in item["metadata"].get("ownerReferences", []) if ref["kind"] == "Deployment"}
    pods = {}
    for item in kubectl("get", "pods", "--selector", selector)["items"]:
        if item["metadata"].get("deletionTimestamp"):
            continue
        for ref in item["metadata"].get("ownerReferences", []):
            if ref["kind"] == "ReplicaSet" and ref["name"] in owners:
                pods.setdefault(owners[ref["name"]], set()).add(
                    (item["metadata"]["uid"], item["metadata"]["name"]))
    return {
        item["metadata"]["name"]: (
            item["metadata"]["generation"],
            (item["spec"]["template"]["metadata"].get("annotations") or {}).get("checksum/config"),
            pods.get(item["metadata"]["name"], set()))
        for item in kubectl("get", "deployments", "--selector", selector)["items"]}


def foreman_url_setting(kube_cluster):
    """The foreman_url setting as the running Foreman web reports it."""
    with port_forward(kube_cluster, NAMESPACE, f"service/{RELEASE}-foreman", 3000) as (port,):
        found = foreman_api(f"http://127.0.0.1:{port}", "GET",
                            "/api/v2/settings?search=name%3Dforeman_url")
    (setting,) = found["results"]
    return setting["value"]


def foreman_url_in_pod(pod):
    """foreman_url from settings.yaml in the pod."""
    result = subprocess.run(
        ["kubectl", "--namespace", NAMESPACE, "exec", pod, "--",
         "grep", "^:foreman_url:", "/etc/foreman/settings.yaml"],
        capture_output=True, text=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr
    return yaml.safe_load(result.stdout)[":foreman_url"]


def test_config_change_restarts_only_the_pods_that_read_it(computed, values_file, kube_cluster,
                                                          tmp_path):
    """helm upgrade with a changed settings.yaml replaces the Foreman pods, and only them.

    foreman.externalUrl is rendered into settings.yaml as :foreman_url:, which
    Foreman reports as the read-only foreman_url setting. Only the port changes,
    so the URL stays valid for every check of the chart."""
    installed = computed["foreman"]["externalUrl"]
    parts = urlsplit(installed)
    changed = urlunsplit(parts._replace(netloc=f"{parts.hostname}:8443"))
    assert changed != installed
    assert foreman_url_setting(kube_cluster) == installed
    changed_values = tmp_path / "values.yaml"
    user_values = yaml.safe_load(values_file.read_text()) or {}
    user_values.setdefault("foreman", {})["externalUrl"] = changed
    changed_values.write_text(yaml.safe_dump(user_values))
    affected = {f"{RELEASE}-{name}" for name in FOREMAN_DEPLOYMENTS}

    # Change the setting, then restore the installed values for the tests after this one.
    for label, values, url in (("config-change", changed_values, changed),
                               ("config-restore", values_file, installed)):
        before = deployment_state()
        result = upgrade(values, label)
        assert result.returncode == 0, result.stdout + result.stderr
        after = deployment_state()
        assert set(after) == set(before)
        for name, (generation, checksum, pods) in after.items():
            if name in affected:
                assert checksum != before[name][1], (label, name)
                assert pods and not pods & before[name][2], (label, name, before[name], after[name])
                for _, pod in pods:
                    assert foreman_url_in_pod(pod) == url, (label, pod)
            else:
                assert (generation, checksum, pods) == before[name], (label, name)
        assert foreman_url_setting(kube_cluster) == url, label


def test_failed_revisions_stay_in_history():
    statuses = [entry["status"] for entry in history()]
    assert statuses[-1] == "deployed"
    assert "failed" in statuses
