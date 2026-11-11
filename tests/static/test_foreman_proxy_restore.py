"""Static checks for restoring a foreman-proxy release under a new name: the
name rewrite in tools/backup-common.sh, the registration Job's previousName
value, and register.rb renaming the Smart Proxy records, run against a fake
Foreman API."""

import json
import re
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from test_foreman_proxy import deep_merge, render  # noqa: F401
from test_render import pod_spec

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "tools" / "backup-common.sh"
REGISTER = ROOT / "charts" / "foreman-proxy" / "files" / "register.rb"


def renamed(name, old, new):
    result = subprocess.run(["bash", "-c", '. "$1"; proxy_name_for_namespace "$2" "$3" "$4"', "sh",
                             str(COMMON), name, old, new], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.mark.parametrize("name, expected", [
    ("capsule-foreman-proxy.capsule.svc.cluster.local", "capsule-foreman-proxy.capsule-restore.svc.cluster.local"),
    ("capsule-foreman-proxy.capsule.svc", "capsule-foreman-proxy.capsule-restore.svc"),
    ("capsule.example.test", ""),
    ("capsule.test", ""),
    # Another namespace, or a name that only ends like one.
    ("capsule-foreman-proxy.other.svc.cluster.local", ""),
    ("a.b.capsule.svc.cluster.local", ""),
])
def test_in_cluster_names_follow_the_namespace(name, expected):
    assert renamed(name, "capsule", "capsule-restore") == expected


def registration_env(manifests):
    (job,) = [job for job in manifests.of_kind("Job") if "registration" in job["metadata"]["name"]]
    return {item["name"]: item.get("value") for item in pod_spec(job)["containers"][0]["env"]}


def test_previous_name_reaches_the_registration_job(render):  # noqa: F811
    assert registration_env(render())["PREVIOUS_NAME"] == ""
    env = registration_env(render({"registration": {"previousName": "old.example.test"}}))
    assert env["PREVIOUS_NAME"] == "old.example.test"


def test_invalid_previous_name_fails(tmp_path):
    from test_foreman_proxy import REQUIRED, helm_template
    result = helm_template(deep_merge(REQUIRED, {"registration": {"previousName": "bad name"}}), tmp_path)
    assert result.returncode != 0
    assert "registration.previousName" in result.stderr


class FakeForeman(BaseHTTPRequestHandler):
    """The Foreman API calls register.rb makes, with Smart Proxies in
    self.server.proxies (id -> record)."""

    def log_message(self, *args):
        pass

    def reply(self, body, code=200):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def proxy(self, record):
        features = ["Pulpcore"] if record["name"].endswith("-pulp") else ["Registration"]
        return {**record, "features": [{"name": name} for name in features],
                "organizations": [{"id": 1, "name": "Default Organization"}],
                "locations": [{"id": 2, "name": "Default Location"}]}

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self):  # noqa: N802
        url = urlsplit(self.path)
        proxies = self.server.proxies
        if url.path == "/api/organizations":
            return self.reply({"results": [{"id": 1, "name": "Default Organization"}]})
        if url.path == "/api/locations":
            return self.reply({"results": [{"id": 2, "name": "Default Location"}]})
        if url.path == "/api/smart_proxies":
            (wanted,) = re.findall(r'name = "(.*)"', parse_qs(url.query)["search"][0])
            return self.reply({"results": [self.proxy(p) for p in proxies.values() if p["name"] == wanted]})
        match = re.fullmatch(r"/api/smart_proxies/(\d+)", url.path)
        if match:
            return self.reply(self.proxy(proxies[int(match.group(1))]))
        return self.reply({}, 404)

    def do_PUT(self):  # noqa: N802
        url = urlsplit(self.path)
        if re.fullmatch(r"/api/smart_proxies/\d+/refresh", url.path):
            return self.reply({})
        identifier = int(re.fullmatch(r"/api/smart_proxies/(\d+)", url.path).group(1))
        attributes = self.body()["smart_proxy"]
        record = self.server.proxies[identifier]
        record.update({key: attributes[key] for key in ("name", "url") if key in attributes})
        self.server.calls.append(("PUT", identifier, attributes.get("name")))
        return self.reply(self.proxy(record))

    def do_POST(self):  # noqa: N802
        attributes = self.body()["smart_proxy"]
        identifier = max(self.server.proxies, default=0) + 1
        self.server.proxies[identifier] = {"id": identifier, "name": attributes["name"], "url": attributes["url"]}
        self.server.calls.append(("POST", identifier, attributes["name"]))
        return self.reply(self.proxy(self.server.proxies[identifier]))


@pytest.fixture
def foreman(tmp_path):
    if not shutil.which("ruby") or not shutil.which("openssl"):
        pytest.skip("needs ruby and openssl")
    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeForeman)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.proxies, server.calls, server.ca = {}, [], cert
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def register(foreman, name, previous=""):
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "FOREMAN_URL": f"https://localhost:{foreman.server_address[1]}",
        "FOREMAN_CA_FILE": str(foreman.ca),
        "OAUTH_CONSUMER_KEY": "key", "OAUTH_CONSUMER_SECRET": "secret",
        "PULP_NAME": f"{name}-pulp", "PULP_URL": f"https://{name}/pulp/api/v3/smart_proxy",
        "PROXY_NAME": name, "PROXY_URL": f"https://{name}:8443", "PROXY_FEATURES": "Registration",
        "PREVIOUS_NAME": previous, "TIMEOUT_SECONDS": "5",
    }
    result = subprocess.run([shutil.which("ruby"), str(REGISTER)], env=env, capture_output=True,
                            text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def names(foreman):
    return {identifier: (record["name"], record["url"]) for identifier, record in foreman.proxies.items()}


def test_registration_renames_the_records_of_the_previous_name(foreman):
    register(foreman, "old.example.test")
    before = names(foreman)
    output = register(foreman, "new.example.test", previous="old.example.test")
    assert "Renamed from old.example.test-pulp:" in output
    assert set(names(foreman)) == set(before), "records were added"
    assert sorted(names(foreman).values()) == [
        ("new.example.test", "https://new.example.test:8443"),
        ("new.example.test-pulp", "https://new.example.test/pulp/api/v3/smart_proxy")]


def test_registration_without_previous_name_creates_new_records(foreman):
    register(foreman, "old.example.test")
    register(foreman, "new.example.test")
    assert len(foreman.proxies) == 4


def test_existing_records_of_the_new_name_win_over_the_previous_name(foreman):
    register(foreman, "old.example.test")
    register(foreman, "new.example.test")
    foreman.calls.clear()
    register(foreman, "new.example.test", previous="old.example.test")
    assert [call[0] for call in foreman.calls] == ["PUT", "PUT"]
    assert {"old.example.test", "old.example.test-pulp"} <= {name for name, _ in names(foreman).values()}
