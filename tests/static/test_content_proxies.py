"""Static assertions on what the foreman Helm chart offers Smart Proxies with
content (capsules): trusted forwarding headers in httpd, the OAuth consumer,
host aliases and a fixed httpd Service address."""

import re

import pytest
import yaml

import test_render
from test_httpd import HTTPD, TRUSTED_HEADERS, spellings, vhosts
from test_render import FULLNAME, pod_spec

ALL_ON = {
    "foreman": {"fqdn": "foreman.test", "externalUrl": "https://foreman.test"},
    **{name: {"enabled": True} for name in (
        "certificates", "httpd", "dynflow", "pulp", "candlepin", "katello", "proxy")},
}
CAPSULES = ["capsule.example.test", "capsule2.example.test"]
TRUSTED = ("%{SSL_CLIENT_VERIFY} == 'SUCCESS' && "
           "%{SSL_CLIENT_S_DN_CN} in {'capsule.example.test', 'capsule2.example.test'}")
OAUTH_FILE = "/usr/share/foreman/config/settings.d/oauth.yaml"
ALIASES = [{"ip": "192.0.2.10", "hostnames": ["foreman.example.test", "capsule.example.test"]}]


def values(**overrides):
    result = yaml.safe_load(yaml.safe_dump(ALL_ON))
    for key, value in overrides.items():
        result.setdefault(key, {}).update(value)
    return result


def unset_lines(vhost):
    return [line.strip() for line in vhost.splitlines()
            if line.strip().startswith("RequestHeader unset ")]


def env(container):
    return {item["name"]: item for item in container.get("env", [])}


def test_default_render_has_none_of_it(manifests, render):
    for rendered in (manifests, render(values())):
        for vhost in vhosts(rendered).values() if ("ConfigMap", HTTPD) in rendered else ():
            assert "expr=!" not in vhost and "RequestHeader edit" not in vhost
            assert all(len(line.split()) == 3 for line in unset_lines(vhost))
        for workload in rendered.of_kind("Deployment", "Job", "Pod"):
            pod = pod_spec(workload)
            assert "hostAliases" not in pod, workload["metadata"]["name"]
            names = [c["name"] for c in pod.get("initContainers", [])]
            assert "oauth-config" not in names, workload["metadata"]["name"]
        if ("Service", HTTPD) in rendered:
            assert "clusterIP" not in rendered["Service", HTTPD]["spec"]


def test_trusted_proxies_keep_one_certificate_header_and_the_forwarded_host(render):
    manifests = render(values(httpd={"trustedProxyClientNames": CAPSULES}))
    ssl = vhosts(manifests)["foreman-ssl.conf"]
    lines = unset_lines(ssl)
    conditional = [line for line in lines if "expr=" in line]
    assert conditional == [
        f'RequestHeader unset X_RHSM_SSL_CLIENT_CERT "expr=!({TRUSTED})"',
        f'RequestHeader unset X-FORWARDED-HOST "expr=!({TRUSTED})"',
    ]
    # Every other spelling, and every other trusted header, is still removed
    # for everyone, before httpd sets its own values.
    removed = {line.split()[2] for line in lines if "expr=" not in line}
    for header in TRUSTED_HEADERS:
        for spelling in spellings(header):
            if spelling not in ("X_RHSM_SSL_CLIENT_CERT", "X-FORWARDED-HOST"):
                assert spelling in removed, spelling
    edit = f'RequestHeader edit X-FORWARDED-HOST "^.*,[ \\t]*" "" "expr={TRUSTED}"'
    body = [line.strip() for line in ssl.splitlines()]
    assert edit in body
    assert body.index(edit) > max(body.index(line) for line in lines)
    assert body.index(edit) < min(i for i, line in enumerate(body) if line.startswith("RequestHeader set "))
    # Port 80 has no client certificates and trusts nobody.
    plain = vhosts(manifests)["foreman.conf"]
    assert "SSL_CLIENT_S_DN_CN" not in plain and "RequestHeader edit" not in plain
    assert plain == vhosts(render(values()))["foreman.conf"]


# The ap_expr comparison is literal: "*.example.test" would trust only a
# certificate with that exact CN, typically a widely shared wildcard certificate.
@pytest.mark.parametrize("name", ["capsule.test' || true || '", "a b", "", "capsule.test}",
                                  "*.example.test", "capsule_1.test", "-capsule.test"])
def test_trusted_proxy_names_must_be_host_names(render, name):
    with pytest.raises(AssertionError, match="is not a host name"):
        render(values(httpd={"trustedProxyClientNames": [name]}))


def test_oauth_consumer_comes_from_a_secret_in_the_web_pods_only(render):
    manifests = render(values(foreman={"oauth": {
        "existingSecret": "foreman-oauth", "consumerKeySecretKey": "key",
        "consumerSecretSecretKey": "secret"}}))
    web = pod_spec(manifests["Deployment", FULLNAME])
    inits = {c["name"]: c for c in web["initContainers"]}
    assert list(inits) == ["katello-config", "oauth-config"]
    init = inits["oauth-config"]
    assert init["command"][:3] == ["ruby", "-ryaml", "-e"]
    assert "oauth_active: true" in init["command"][3]
    assert "oauth_map_users: false" in init["command"][3]
    assert env(init)["FOREMAN_OAUTH_CONSUMER_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "foreman-oauth", "key": "key"}
    assert env(init)["FOREMAN_OAUTH_CONSUMER_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": "foreman-oauth", "key": "secret"}
    (main,) = web["containers"]
    assert not {"FOREMAN_OAUTH_CONSUMER_KEY", "FOREMAN_OAUTH_CONSUMER_SECRET"} & env(main).keys()
    (mount,) = [m for m in main["volumeMounts"] if m["name"] == "oauth-config"]
    assert mount == {"name": "oauth-config", "mountPath": OAUTH_FILE,
                     "subPath": "oauth.yaml", "readOnly": True}
    (volume,) = [v for v in web["volumes"] if v["name"] == "oauth-config"]
    assert volume["emptyDir"]["medium"] == "Memory"
    # No other pod gets the secret, and no ConfigMap carries OAuth settings.
    for workload in manifests.of_kind("Deployment", "Job", "Pod"):
        if workload["metadata"]["name"] != FULLNAME:
            assert "foreman-oauth" not in yaml.safe_dump(workload), workload["metadata"]["name"]
    for config_map in manifests.of_kind("ConfigMap"):
        assert "oauth_consumer" not in yaml.safe_dump(config_map["data"])
    test_render.test_containers_use_restricted_security_context(manifests)
    test_render.test_workload_containers_request_cpu_and_memory(manifests)


def test_oauth_without_katello(render):
    manifests = render({"foreman": {"oauth": {"existingSecret": "foreman-oauth"}}})
    (init,) = pod_spec(manifests["Deployment", FULLNAME])["initContainers"]
    assert init["name"] == "oauth-config"


def test_host_aliases_reach_every_pod_that_calls_smart_proxies(render):
    manifests = render(values(foreman={"hostAliases": ALIASES}))
    with_aliases = {doc["metadata"]["name"]
                    for doc in manifests.of_kind("Deployment", "Job", "CronJob", "Pod")
                    if pod_spec(doc).get("hostAliases") == ALIASES}
    assert with_aliases == {
        FULLNAME, f"{FULLNAME}-pulp-smart-proxy-1", f"{FULLNAME}-proxy-register-1",
        *(f"{FULLNAME}-dynflow-{name}" for name in ("orchestrator", "worker", "worker-hosts-queue")),
        *(f"{FULLNAME}-cron-{period}" for period in ("hourly", "daily", "weekly", "monthly")),
    }


def test_httpd_service_address_can_be_fixed(render):
    service = render(values(httpd={"service": {"clusterIP": "10.96.0.80"}}))["Service", HTTPD]
    assert service["spec"]["clusterIP"] == "10.96.0.80"


def test_public_pulp_url_with_host_aliases(render):
    """Katello takes the URL capsules sync from (pulp_url) from the Pulp Smart
    Proxy it also uses itself, so both must be the public name, and the
    Foreman pods resolve it with host aliases."""
    manifests = render(values(
        foreman={"hostAliases": [{"ip": "10.96.0.80", "hostnames": ["foreman.test"]}]},
        httpd={"service": {"clusterIP": "10.96.0.80"}},
        pulp={"settings": {"smartProxyPulpUrl": "https://foreman.test"}},
        katello={"pulpSmartProxy": {"url": "https://foreman.test/pulp/api/v3/smart_proxy"}}))
    job = pod_spec(manifests["Job", f"{FULLNAME}-pulp-smart-proxy-1"])
    assert env(job["containers"][0])["PULP_SMART_PROXY_URL"]["value"] == \
        "https://foreman.test/pulp/api/v3/smart_proxy"
    assert job["hostAliases"] == [{"ip": "10.96.0.80", "hostnames": ["foreman.test"]}]
    pulp_settings = [doc for doc in manifests.of_kind("Secret", "ConfigMap")
                     if "SMART_PROXY_PULP_URL" in yaml.safe_dump(doc)]
    assert pulp_settings and all(
        re.search(r"SMART_PROXY_PULP_URL\W+https://foreman\.test\b", yaml.safe_dump(doc))
        for doc in pulp_settings)
