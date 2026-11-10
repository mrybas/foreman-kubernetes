"""Exercise what a Smart Proxy with content (capsule) needs from Foreman: OAuth
for signed API requests, and the public name of the server resolvable from the
Foreman pods, so that the Pulp Smart Proxy can be registered under it.

The Kind deployment sets foreman.oauth.existingSecret=foreman-oauth and
foreman.hostAliases for foreman.test. Tests skip on releases without them.
"""

import base64
import hashlib
import hmac
import json
import secrets
import subprocess
import time
from urllib.parse import quote, urlsplit

import pytest

from test_httpd import ADMIN, FQDN, https, httpd, namespace, pki, release, tls_context  # noqa: F401

OAUTH_SECRET_NAME = "foreman-oauth"


def oauth_header(method, url, key, secret):
    """OAuth 1.0a Authorization header, HMAC-SHA1, no token (RFC 5849)."""
    params = {
        "oauth_consumer_key": key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
    }
    parts = urlsplit(url)
    query = [tuple(pair.split("=", 1)) for pair in parts.query.split("&") if pair]
    encoded = sorted((quote(k, safe="~"), quote(v, safe="~")) for k, v in [*params.items(), *query])
    base = "&".join(quote(part, safe="~") for part in (
        method.upper(), f"{parts.scheme}://{parts.netloc}{parts.path}",
        "&".join(f"{k}={v}" for k, v in encoded)))
    digest = hmac.new(f"{quote(secret, safe='~')}&".encode(), base.encode(), hashlib.sha1).digest()
    params["oauth_signature"] = base64.b64encode(digest).decode()
    return "OAuth " + ", ".join(f'{k}="{quote(v, safe="~")}"' for k, v in params.items())


@pytest.fixture(scope="module")
def oauth(kube_cluster, namespace, release):  # noqa: F811
    """Consumer key and secret, if the release reads them."""
    deployment = kube_cluster.kubectl(f"get deployment {release}-foreman", namespace=namespace)
    inits = deployment["spec"]["template"]["spec"].get("initContainers", [])
    if "oauth-config" not in [container["name"] for container in inits]:
        pytest.skip("foreman.oauth.existingSecret is not set")
    data = kube_cluster.kubectl(f"get secret {OAUTH_SECRET_NAME}", namespace=namespace)["data"]
    return {name: base64.b64decode(data[key]).decode()
            for name, key in (("key", "OAUTH_CONSUMER_KEY"), ("secret", "OAUTH_CONSUMER_SECRET"))}


def admin_get(httpd, pki, path):  # noqa: F811
    status, _, body = https(httpd, tls_context(pki), "GET", path,
                            headers={"Authorization": ADMIN, "Accept": "application/json"})
    assert status == 200, body[:1000]
    return json.loads(body)


def test_settings_show_oauth_from_the_secret_read_only(httpd, pki, oauth):  # noqa: F811
    settings = {name: admin_get(httpd, pki, f"/api/v2/settings/{name}") for name in (
        "oauth_active", "oauth_map_users", "oauth_consumer_key", "oauth_consumer_secret")}
    assert settings["oauth_active"]["value"] is True
    assert settings["oauth_map_users"]["value"] is False
    for setting in settings.values():
        assert setting["readonly"] is True, setting
    # Foreman hides both encrypted values; the secret never appears.
    for name in ("oauth_consumer_key", "oauth_consumer_secret"):
        assert settings[name]["encrypted"] is True
        assert settings[name]["value"] not in (oauth["key"], oauth["secret"]), name


def test_signed_requests_authenticate(httpd, pki, oauth):  # noqa: F811
    url = f"https://{FQDN}/api/v2/smart_proxies?per_page=1"

    def get(authorization):
        headers = {"Accept": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        status, _, body = https(httpd, tls_context(pki), "GET", "/api/v2/smart_proxies?per_page=1",
                                headers=headers)
        return status, body

    status, body = get(oauth_header("GET", url, oauth["key"], oauth["secret"]))
    assert status == 200, body[:1000]
    assert "results" in json.loads(body)
    for label, authorization in (
        ("unsigned", None),
        ("wrong secret", oauth_header("GET", url, oauth["key"], "not-the-secret")),
        ("wrong key", oauth_header("GET", url, "not-the-key", oauth["secret"])),
    ):
        status, body = get(authorization)
        assert status == 401, (label, status, body[:500])


def test_web_pod_logs_do_not_contain_the_secret(kube_cluster, namespace, release, oauth):  # noqa: F811
    logs = subprocess.run(
        ["kubectl", "--kubeconfig", kube_cluster.kube_config_path, "--namespace", namespace,
         "logs", f"deployment/{release}-foreman", "--all-containers", "--tail=-1"],
        capture_output=True, text=True, check=True).stdout
    assert oauth["secret"] not in logs


def resolve_in(kube_cluster, namespace, deployment, name):  # noqa: F811
    result = subprocess.run(
        ["kubectl", "--kubeconfig", kube_cluster.kube_config_path, "--namespace", namespace,
         "exec", f"deployment/{deployment}", "--", "getent", "hosts", name],
        capture_output=True, text=True, check=False)
    return result.stdout.split()[0] if result.returncode == 0 else None


@pytest.fixture(scope="module")
def public_name(kube_cluster, namespace, release):  # noqa: F811
    """Address of FQDN in the Foreman pods, if the release maps it with hostAliases."""
    deployment = kube_cluster.kubectl(f"get deployment {release}-foreman", namespace=namespace)
    aliases = deployment["spec"]["template"]["spec"].get("hostAliases", [])
    addresses = [alias["ip"] for alias in aliases if FQDN in alias["hostnames"]]
    if not addresses:
        pytest.skip(f"foreman.hostAliases does not map {FQDN}")
    service = kube_cluster.kubectl(f"get service {release}-foreman-httpd", namespace=namespace)
    assert addresses == [service["spec"]["clusterIP"]]
    return addresses[0]


def test_foreman_pods_resolve_the_public_name_to_httpd(kube_cluster, namespace, release,
                                                       public_name):  # noqa: F811
    for deployment in ("foreman", "foreman-dynflow-orchestrator", "foreman-dynflow-worker"):
        assert resolve_in(kube_cluster, namespace, f"{release}-{deployment}", FQDN) == public_name


def test_pulp_smart_proxy_can_use_the_public_name(httpd, pki, public_name):  # noqa: F811
    """A capsule outside the cluster syncs from the Pulp URL of the Pulp Smart
    Proxy, so that URL must be the public name. Saving a Smart Proxy makes
    Foreman fetch its features from the URL, through httpd by that name."""
    (proxy,) = admin_get(httpd, pki, "/api/v2/smart_proxies?search="
                         + quote("feature = Pulpcore"))["results"]
    public = f"https://{FQDN}/pulp/api/v3/smart_proxy"

    def update(url):
        status, _, body = https(
            httpd, tls_context(pki), "PUT", f"/api/v2/smart_proxies/{proxy['id']}",
            headers={"Authorization": ADMIN, "Content-Type": "application/json"},
            body=json.dumps({"smart_proxy": {"url": url}}))
        assert status == 200, body[:2000]
        status, _, body = https(
            httpd, tls_context(pki), "PUT", f"/api/v2/smart_proxies/{proxy['id']}/refresh",
            headers={"Authorization": ADMIN, "Content-Type": "application/json"}, body="{}")
        assert status == 200, body[:2000]
        return admin_get(httpd, pki, f"/api/v2/smart_proxies/{proxy['id']}")

    try:
        detail = update(public)
        assert detail["url"] == public
        assert "Pulpcore" in [feature["name"] for feature in detail["features"]]
    finally:
        update(proxy["url"])
