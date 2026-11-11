"""Run each cron CronJob of the installed release once.

Each run starts as a Job created from its CronJob, the way the CronJob
controller starts it, and must complete. The weekly run must also remove an
expired notification and keep an active one (notifications:clean).
"""

import base64
import json
import subprocess
import uuid
from urllib.parse import urlsplit

import pytest

from test_foreman import namespace, release  # noqa: F401

PERIODS = ("hourly", "daily", "weekly", "monthly")
# Rake tasks of each period that the chart's test installation runs: Foreman,
# foreman-tasks and Katello register them in the image.
TASKS = {
    "hourly": ["ldap:refresh_usergroups"],
    "daily": ["reports:daily", "db:sessions:clear", "reports:expire", "audits:expire",
              "foreman_tasks:cleanup"],
    "weekly": ["reports:weekly", "notifications:clean", "katello:delete_orphaned_content",
               "katello:refresh_alternate_content_sources"],
    "monthly": ["reports:monthly"],
}


def kubectl(kube_cluster, namespace, *args, timeout=120):  # noqa: F811
    result = subprocess.run(
        ["kubectl", "--kubeconfig", kube_cluster.kube_config_path, "--namespace", namespace,
         *args], capture_output=True, text=True, timeout=timeout, check=False)
    assert result.returncode == 0, f"kubectl {' '.join(args)}: {result.stdout}\n{result.stderr}"
    return result.stdout


def cronjobs(kube_cluster, namespace, release):  # noqa: F811
    """Period -> CronJob of the release."""
    items = json.loads(kubectl(
        kube_cluster, namespace, "get", "cronjobs", "--output", "json",
        "--selector", f"app.kubernetes.io/instance={release}"))["items"]
    found = {}
    for item in items:
        component = item["metadata"]["labels"].get("app.kubernetes.io/component", "")
        if component.startswith("cron-"):
            found[component[len("cron-"):]] = item
    return found


def psql(kube_cluster, namespace, sql):  # noqa: F811
    """Run SQL in the Foreman database of the test PostgreSQL (tests/kind)."""
    secret = json.loads(kubectl(kube_cluster, namespace, "get", "secret", "foreman-runtime",
                                "--output", "json"))
    database = urlsplit(base64.b64decode(secret["data"]["DATABASE_URL"]).decode()).path.lstrip("/")
    return kubectl(kube_cluster, namespace, "exec", "deployment/postgresql", "--",
                   "psql", "--username=postgres", f"--dbname={database}", "--no-psqlrc",
                   "--tuples-only", "--no-align", "--set=ON_ERROR_STOP=1", "--command", sql)


def test_one_cronjob_per_period(kube_cluster, namespace, release):  # noqa: F811
    found = cronjobs(kube_cluster, namespace, release)
    assert sorted(found) == sorted(PERIODS)
    for period, item in found.items():
        assert not item["spec"].get("suspend"), period
        assert item["spec"]["concurrencyPolicy"] == "Forbid", period
        (container,) = item["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
        assert container["command"][-1] == f"cron:{period}"


@pytest.fixture(scope="module")
def notifications(kube_cluster, namespace):  # noqa: F811
    """An expired and an active notification of a blueprint made for this test,
    with a recipient each. Yields {message: id}; removes them afterwards."""
    name = f"fk_cron_test_{uuid.uuid4().hex[:8]}"
    output = psql(kube_cluster, namespace, f"""
        WITH blueprint AS (
          INSERT INTO notification_blueprints (name, "group", level, message, expires_in,
                                               created_at, updated_at)
          VALUES ('{name}', 'Testing', 'info', 'Cron test', 86400, now(), now())
          RETURNING id),
        admin AS (SELECT id FROM users WHERE login = 'admin'),
        created AS (
          INSERT INTO notifications (notification_blueprint_id, user_id, audience, message,
                                     expired_at, created_at, updated_at)
          SELECT blueprint.id, admin.id, 'admin', m.message, m.expired_at, now(), now()
          FROM blueprint, admin, (VALUES
            ('expired', (now() AT TIME ZONE 'utc') - interval '1 day'),
            ('active', (now() AT TIME ZONE 'utc') + interval '1 day')) AS m(message, expired_at)
          RETURNING id, user_id, message),
        recipients AS (
          INSERT INTO notification_recipients (notification_id, user_id, created_at, updated_at)
          SELECT id, user_id, now(), now() FROM created)
        SELECT message || '=' || id FROM created;
    """)
    ids = dict(line.split("=") for line in output.split() if "=" in line)
    assert set(ids) == {"expired", "active"}, output
    yield ids
    psql(kube_cluster, namespace, f"""
        DELETE FROM notification_recipients WHERE notification_id IN
          (SELECT n.id FROM notifications n JOIN notification_blueprints b
           ON b.id = n.notification_blueprint_id WHERE b.name = '{name}');
        DELETE FROM notifications WHERE notification_blueprint_id IN
          (SELECT id FROM notification_blueprints WHERE name = '{name}');
        DELETE FROM notification_blueprints WHERE name = '{name}';
    """)


@pytest.fixture(scope="module")
def runs():
    """Period -> logs of its completed run, shared with the later checks."""
    return {}


@pytest.mark.parametrize("period", PERIODS)
def test_cronjob_run_completes(kube_cluster, namespace, release, notifications,  # noqa: F811
                               runs, period):
    cronjob = cronjobs(kube_cluster, namespace, release)[period]["metadata"]["name"]
    job = f"{cronjob[:44]}-{uuid.uuid4().hex[:6]}"
    kubectl(kube_cluster, namespace, "create", "job", job, f"--from=cronjob/{cronjob}")
    try:
        waited = subprocess.run(
            ["kubectl", "--kubeconfig", kube_cluster.kube_config_path, "--namespace", namespace,
             "wait", f"job/{job}", "--for=condition=Complete", "--timeout=900s"],
            capture_output=True, text=True, timeout=960, check=False)
        logs = subprocess.run(
            ["kubectl", "--kubeconfig", kube_cluster.kube_config_path, "--namespace", namespace,
             "logs", f"job/{job}", "--container", "cron", "--tail=-1"],
            capture_output=True, text=True, timeout=120, check=False).stdout
        status = kubectl(kube_cluster, namespace, "get", "job", job, "--output",
                         "jsonpath={.status}")
        assert waited.returncode == 0, f"{waited.stderr}\nstatus: {status}\n{logs[-6000:]}"
    finally:
        kubectl(kube_cluster, namespace, "delete", "job", job, "--wait=false")
    for task in TASKS[period]:
        assert f"Foreman::Cron[{period}]: finished {task}" in logs, logs[-6000:]
    runs[period] = logs


def test_weekly_run_removed_only_the_expired_notification(kube_cluster, namespace,  # noqa: F811
                                                          notifications, runs):
    if "weekly" not in runs:
        pytest.skip("the weekly run did not complete")
    left = psql(kube_cluster, namespace,
                "SELECT id FROM notifications WHERE id IN "
                f"({notifications['expired']}, {notifications['active']})").split()
    assert left == [notifications["active"]]
