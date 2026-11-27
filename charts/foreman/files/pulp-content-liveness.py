# Liveness check of a Pulp content pod.
# A content app records a heartbeat in core_appstatus every few seconds. After
# a database failover it can stop doing so and keep serving: Pulp then lists no
# content app and Katello reports pulp3_content as failed. Fail when this pod
# has no heartbeat younger than 45 seconds.
# Replicas take turns, so they do not all restart together: while healthy the
# pod remembers the content pods it saw, and once its heartbeat is gone it
# waits 90 seconds for each pod sorted before it.
# An unreachable database passes: restarting this pod would not help.
import json
import os
import socket
import sys
import time

import psycopg

STATE = "/tmp/content-liveness.json"
me = socket.gethostname()
settings = {}
exec(open(os.environ.get("PULP_SETTINGS", "/etc/pulp/settings.py")).read(), settings)
try:
    with psycopg.connect(
        host=settings["DATABASES__default__HOST"],
        port=settings["DATABASES__default__PORT"],
        dbname=settings["DATABASES__default__NAME"],
        user=settings["DATABASES__default__USER"],
        password=os.environ["PULP_DATABASES__default__PASSWORD"],
        sslmode=settings.get("DATABASES__default__OPTIONS__sslmode", "prefer"),
        connect_timeout=3,
    ) as conn:
        alive = sorted({row[0].split("@", 1)[1] for row in conn.execute(
            "select name from core_appstatus where app_type = 'content' "
            "and last_heartbeat > now() - interval '45 seconds'")})
except psycopg.OperationalError:
    sys.exit(0)
try:
    state = json.load(open(STATE))
except (OSError, ValueError):
    state = {}
if me in alive:
    state = {"peers": alive}
    code = 0
else:
    state.setdefault("missing_since", time.time())
    turn = sorted(set(state.get("peers", [])) | {me}).index(me)
    code = 1 if time.time() - state["missing_since"] >= 90 * turn else 0
try:
    json.dump(state, open(STATE, "w"))
except OSError:
    pass
sys.exit(code)
