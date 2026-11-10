"""The container gateway of the Smart Proxy with content.

make test-content-proxy installs the foreman-proxy release with
proxy.containerGateway.enabled and its database in the PostgreSQL fixture.
Hosts reach the gateway at https://<name>/v2/ through the Smart Proxy's
httpd; it fetches from the Smart Proxy's Pulp registry with the client
certificate of the bundle Secret.
"""

import json
from urllib.parse import urlencode

import pytest

from test_content_proxy import CAPSULE_NAME, capsule, capsule_request, installed  # noqa: F401
from test_httpd import httpd, namespace, pki, release, tls_context  # noqa: F401
from test_katello import Api

pytestmark = pytest.mark.content_proxy


@pytest.fixture(scope="module")
def enabled(httpd, pki, installed):  # noqa: F811
    api = Api(httpd, pki)
    found = api.get("/api/v2/smart_proxies?" + urlencode({"search": f'name = "{CAPSULE_NAME}"'}))["results"]
    proxy = api.get(f"/api/v2/smart_proxies/{found[0]['id']}")
    if "Container_Gateway" not in {feature["name"] for feature in proxy["features"]}:
        pytest.skip("the container gateway is not enabled")


def test_registry_asks_anonymous_clients_to_log_in(enabled, capsule, pki):  # noqa: F811
    status, headers, _ = capsule_request(capsule, tls_context(pki), "GET", "/v2/")
    assert status == 401
    challenge = headers["WWW-Authenticate"]
    assert f'realm="https://{CAPSULE_NAME}/v2/token"' in challenge, challenge


def test_registry_reaches_pulp_with_the_client_certificate(enabled, capsule, pki):  # noqa: F811
    """The anonymous token Katello hands out: the gateway pings the Smart
    Proxy's Pulp registry, which accepts only its client certificate."""
    status, headers, body = capsule_request(capsule, tls_context(pki), "GET", "/v2/",
                                            headers={"Authorization": "Bearer unauthorized"})
    assert status == 200, body[:500]
    assert headers["Docker-Distribution-API-Version"] == "registry/2.0"


def test_catalog_comes_from_the_gateway_database(enabled, capsule, pki):  # noqa: F811
    status, _, body = capsule_request(capsule, tls_context(pki), "GET", "/v2/_catalog",
                                      headers={"Authorization": "Bearer unauthorized"})
    assert status == 200, body[:500]
    assert isinstance(json.loads(body)["repositories"], list)
