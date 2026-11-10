"""Static assertions on the httpd HTTPS front door of the foreman Helm chart."""

import itertools
import re

import pytest
import yaml

import test_render
from test_render import FULLNAME, containers, pod_spec

HTTPD = f"{FULLNAME}-httpd"
ENABLED = {"certificates": {"enabled": True}, "httpd": {"enabled": True}}
DIGEST = re.compile(r"^registry\.access\.redhat\.com/ubi10/httpd-24@sha256:[0-9a-f]{64}$")
CLIENT_HEADERS = ("SSL-CLIENT-S-DN", "SSL-CLIENT-CERT", "SSL-CLIENT-VERIFY")
# Headers Foreman, Katello or Pulp trust from httpd; clients must not set them.
TRUSTED_HEADERS = (
    "REMOTE-USER", "REMOTE-USER-EMAIL", "REMOTE-USER-FIRSTNAME", "REMOTE-USER-LASTNAME",
    "REMOTE-USER-GROUPS", "SSL-CLIENT-S-DN", "SSL-CLIENT-CERT", "SSL-CLIENT-VERIFY",
    "X-RHSM-SSL-CLIENT-CERT", "X-CLIENT-CERT", "X-FORWARDED-FOR", "X-FORWARDED-HOST",
    "X-FORWARDED-PORT", "X-FORWARDED-PROTO", "X-FORWARDED-SCHEME", "X-FORWARDED-SSL",
    "FORWARDED", "CLIENT-IP",
)
PULP_PATHS = ("/pulp/api/v3", "/pulpcore_registry", "/pulp/content", "/pulp/isos",
              "/pulp/container/", "/pulp_ansible/galaxy/", "/pulp/assets/")


def spellings(header):
    """Every combination of "-" and "_" between the parts of a header name."""
    parts = header.split("-")
    return {parts[0] + "".join(sep + part for sep, part in zip(seps, parts[1:]))
            for seps in itertools.product("-_", repeat=len(parts) - 1)}


def enabled(**extra):
    values = yaml.safe_load(yaml.safe_dump(ENABLED))
    values.update(extra)
    return values


def config(manifests, name):
    return manifests["ConfigMap", HTTPD]["data"][name]


def vhosts(manifests):
    return {name: config(manifests, name) for name in ("foreman.conf", "foreman-ssl.conf")}


def settings(manifests):
    return yaml.safe_load(manifests["ConfigMap", f"{FULLNAME}-config"]["data"]["settings.yaml"])


def test_disabled_by_default_renders_nothing_new(manifests):
    names = {name for _, name in manifests}
    assert not {name for name in names if "httpd" in name}
    assert not manifests.of_kind("NetworkPolicy")
    assert settings(manifests)[":require_ssl"] is False
    assert ":trusted_proxies" not in settings(manifests)


def test_requires_certificates(render):
    try:
        render({"httpd": {"enabled": True}})
    except AssertionError as error:
        assert "httpd.enabled requires certificates.enabled" in str(error)
    else:
        raise AssertionError("rendering without certificates must fail")


def test_image_is_pinned_by_digest(render):
    (container,) = pod_spec(render(enabled())["Deployment", HTTPD])["containers"]
    assert DIGEST.match(container["image"]), container["image"]


def test_chart_wide_checks_hold_with_httpd(render):
    manifests = render(enabled(pulp={"enabled": True}))
    assert ("Deployment", HTTPD) in manifests
    test_render.test_containers_use_restricted_security_context(manifests)
    test_render.test_workload_containers_request_cpu_and_memory(manifests)
    test_render.test_resources_have_standard_labels(manifests)
    assert not manifests.of_kind("Secret")
    for content in manifests["ConfigMap", HTTPD]["data"].values():
        assert "PRIVATE KEY" not in content and "BEGIN CERTIFICATE" not in content


def test_pod_is_restricted_with_read_only_mounts(render):
    pod = pod_spec(render(enabled())["Deployment", HTTPD])
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] != 0
    assert pod["securityContext"]["fsGroup"] == pod["securityContext"]["runAsGroup"]
    (container,) = pod["containers"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert {p["containerPort"] for p in container["ports"]} == {8080, 8443}
    for mount in container["volumeMounts"]:
        assert mount.get("readOnly") is True or mount["mountPath"] == "/tmp", mount
    tls = next(v for v in pod["volumes"] if v["name"] == "tls")
    assert tls["projected"]["defaultMode"] == 0o440


def test_mounts_server_and_client_ca_secrets(render):
    pod = pod_spec(render(enabled(certificates={
        "enabled": True, "server": {"secretName": "public-tls"},
    }))["Deployment", HTTPD])
    tls = next(v for v in pod["volumes"] if v["name"] == "tls")
    files = {item["path"]: (source["secret"]["name"], item["key"])
             for source in tls["projected"]["sources"] for item in source["secret"]["items"]}
    assert files == {
        "server.crt": ("public-tls", "tls.crt"),
        "server.key": ("public-tls", "tls.key"),
        "server-ca.crt": ("public-tls", "ca.crt"),
        "client-ca.crt": ("foreman-ca", "ca.crt"),
    }
    pub = next(v for v in pod["volumes"] if v["name"] == "pub")
    assert pub["secret"]["secretName"] == "public-tls"
    assert pub["secret"]["items"] == [{"key": "ca.crt", "path": "katello-server-ca.crt"}]


def test_service_ports_and_type(render):
    service = render(enabled(httpd={**ENABLED["httpd"], "service": {
        "type": "LoadBalancer", "externalTrafficPolicy": "Local",
        "annotations": {"metallb.universe.tf/loadBalancerIPs": "192.0.2.10"},
    }}))["Service", HTTPD]
    assert service["spec"]["type"] == "LoadBalancer"
    assert service["spec"]["externalTrafficPolicy"] == "Local"
    assert service["metadata"]["annotations"] == {"metallb.universe.tf/loadBalancerIPs": "192.0.2.10"}
    ports = {p["port"]: p["targetPort"] for p in service["spec"]["ports"]}
    assert ports == {80: "http", 443: "https"}


def test_selectors_do_not_overlap_with_foreman_web(render):
    manifests = render(enabled())
    httpd_pod = manifests["Deployment", HTTPD]["spec"]["template"]["metadata"]["labels"]
    web_pod = manifests["Deployment", FULLNAME]["spec"]["template"]["metadata"]["labels"]
    web_selector = manifests["Service", FULLNAME]["spec"]["selector"]
    httpd_selector = manifests["Service", HTTPD]["spec"]["selector"]
    assert not web_selector.items() <= httpd_pod.items()
    assert httpd_selector.items() <= httpd_pod.items()
    assert not httpd_selector.items() <= web_pod.items()


def test_client_certificate_is_optional_and_verified(render):
    ssl = config(render(enabled()), "foreman-ssl.conf")
    assert "SSLVerifyClient optional" in ssl
    assert "SSLCACertificateFile /etc/httpd-tls/client-ca.crt" in ssl
    assert "SSLOptions +StdEnvVars +ExportCertData" in ssl
    for header in CLIENT_HEADERS:
        variable = header.replace("-", "_")
        assert f'RequestHeader set {header} "%{{{variable}}}s"' in ssl
    assert 'RequestHeader set X-FORWARDED-PROTO "https"' in ssl


def test_spellings_cover_dash_and_underscore():
    assert spellings("X-RHSM-SSL-CLIENT-CERT") >= {"X-RHSM_SSL-CLIENT-CERT", "X_RHSM-SSL_CLIENT-CERT"}
    assert len(spellings("X-RHSM-SSL-CLIENT-CERT")) == 16
    assert spellings("FORWARDED") == {"FORWARDED"}


@pytest.mark.parametrize("pulp", [False, True])
def test_client_supplied_trusted_headers_are_removed_first(render, pulp):
    manifests = render(enabled(pulp={"enabled": pulp}))
    for name, vhost in vhosts(manifests).items():
        lines = [line.strip() for line in vhost.splitlines()]
        unset = {}
        for index, line in enumerate(lines):
            if line.startswith("RequestHeader unset "):
                unset.setdefault(line.split()[2].upper(), index)
        first_set = min(index for index, line in enumerate(lines)
                        if line.startswith("RequestHeader set "))
        for header in TRUSTED_HEADERS:
            for spelling in spellings(header):
                assert spelling in unset, (name, spelling)
                assert unset[spelling] < first_set, (name, spelling)
    plain = config(manifests, "foreman.conf")
    for header in CLIENT_HEADERS:
        assert f'RequestHeader set {header} ""' in plain
    # Never set to an empty value: Katello would take "" as the certificate.
    for vhost in vhosts(manifests).values():
        assert "RequestHeader set X-RHSM" not in vhost.upper()


def test_foreman_trusts_the_front_door(render):
    values = settings(render(enabled(foreman={"trustedProxies": ["10.244.0.0/16"]})))
    assert values[":require_ssl"] is True
    assert values[":ssl_client_dn_env"] == "HTTP_SSL_CLIENT_S_DN"
    assert values[":ssl_client_verify_env"] == "HTTP_SSL_CLIENT_VERIFY"
    assert values[":ssl_client_cert_env"] == "HTTP_SSL_CLIENT_CERT"
    assert values[":trusted_proxies"] == ["127.0.0.0/8", "::1", "10.244.0.0/16"]
    # The migration Job serves no requests.
    migration = render(enabled())["ConfigMap", f"{FULLNAME}-migration-config"]
    assert yaml.safe_load(migration["data"]["settings.yaml"])[":require_ssl"] is False


def test_everything_else_goes_to_foreman(render):
    for vhost in vhosts(render(enabled())).values():
        assert "ProxyPass / http://foreman-foreman:3000/ retry=0 timeout=900 upgrade=websocket" in vhost
        assert "ProxyPreserveHost On" in vhost
        assert "Alias /pub /var/www/pub" in vhost


def test_pulp_routes_only_with_pulp(render):
    for vhost in vhosts(render(enabled())).values():
        assert "pulp-api" not in vhost and "pulp-content" not in vhost
        assert "katello-rhsm-consumer" not in vhost
    manifests = render(enabled(pulp={"enabled": True}))
    ssl = config(manifests, "foreman-ssl.conf")
    for path in PULP_PATHS:
        assert path in ssl, path
    assert "http://foreman-foreman-pulp-api:24817/pulp/api/v3 timeout=600" in ssl
    assert "http://foreman-foreman-pulp-content:24816/pulp/content disablereuse=on timeout=600" in ssl
    plain = config(manifests, "foreman.conf")
    assert "/pulp/content" in plain and "/pulp/api/v3" not in plain
    for vhost in (ssl, plain):
        assert "RewriteRule ^/pub/katello-rhsm-consumer$" in vhost


def test_pulp_admin_only_for_trusted_client_certificates(render):
    ssl = config(render(enabled(
        pulp={"enabled": True},
        foreman={"fqdn": "foreman.example.test"},
        httpd={**ENABLED["httpd"], "pulpTrustedClientNames": ["proxy.example.test"]},
    )), "foreman-ssl.conf")
    for location in ("/pulp/api/v3", "/pulpcore_registry"):
        block = re.search(rf'<Location "{location}">(.*?)</Location>', ssl, re.S).group(1)
        lines = [line.strip() for line in block.strip().splitlines()]
        assert lines[:2] == ["RequestHeader unset REMOTE_USER", "RequestHeader unset REMOTE-USER"]
        granted = re.findall(r'RequestHeader set REMOTE-USER "admin" "expr=(.*)"', block)
        assert granted == [
            f"%{{SSL_CLIENT_VERIFY}} == 'SUCCESS' && %{{SSL_CLIENT_S_DN_CN}} == '{cn}'"
            for cn in ("foreman.example.test", "proxy.example.test")
        ]
    content = re.search(r'<Location "/pulp/content">(.*?)</Location>', ssl, re.S).group(1)
    assert 'RequestHeader set X-CLIENT-CERT "%{SSL_CLIENT_CERT}s" env=SSL_CLIENT_CERT' in content
    # The vhost removes client-sent copies before the Location sets its own.
    assert ssl.index("RequestHeader unset X_CLIENT-CERT") < ssl.index('<Location "/pulp/content">')


def test_candlepin_is_not_exposed(render):
    manifests = render(enabled(candlepin={"enabled": True}))
    for vhost in vhosts(manifests).values():
        assert "candlepin" not in vhost.lower()
        assert "RewriteRule ^/pub/katello-rhsm-consumer$" in vhost


def test_network_policy_limits_web_to_httpd_and_test_pods(render):
    manifests = render(enabled(pulp={"enabled": True}, dynflow={"enabled": True},
                               candlepin={"enabled": True}))
    policy = manifests["NetworkPolicy", f"{FULLNAME}-web"]
    web_pod = manifests["Deployment", FULLNAME]["spec"]["template"]["metadata"]["labels"]
    assert policy["spec"]["podSelector"]["matchLabels"].items() <= web_pod.items()
    # Only the web pods: other components of the release keep their own ports.
    assert policy["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "web"
    (rule,) = policy["spec"]["ingress"]
    (peer,) = rule["from"]
    assert peer == {"podSelector": {
        "matchLabels": {"app.kubernetes.io/instance": "foreman"},
        "matchExpressions": [{"key": "app.kubernetes.io/component", "operator": "In",
                              "values": ["httpd", "test"]}],
    }}

    def allowed(labels):
        return (labels.get("app.kubernetes.io/instance") == "foreman"
                and labels.get("app.kubernetes.io/component") in ("httpd", "test"))

    allowed_pods = set()
    for workload in manifests.of_kind("Deployment", "Job", "Pod"):
        labels = (workload["metadata"]["labels"] if workload["kind"] == "Pod"
                  else workload["spec"]["template"]["metadata"]["labels"])
        if allowed(labels):
            allowed_pods.add(workload["metadata"]["name"])
    assert allowed_pods == {HTTPD, f"{FULLNAME}-smoke-test"}
    off = render(enabled(httpd={**ENABLED["httpd"], "networkPolicy": {"enabled": False}}))
    assert not off.of_kind("NetworkPolicy")


def test_web_probes_and_smoke_test_send_https_proto(render):
    manifests = render(enabled())
    (web,) = pod_spec(manifests["Deployment", FULLNAME])["containers"]
    headers = {h["name"]: h["value"] for h in web["readinessProbe"]["httpGet"]["httpHeaders"]}
    assert headers["X-Forwarded-Proto"] == "https"
    (smoke,) = containers(manifests["Pod", f"{FULLNAME}-smoke-test"])
    assert "X-Forwarded-Proto: https" in " ".join(smoke["args"])


def test_config_change_rolls_out_httpd(render):
    first = render(enabled())["Deployment", HTTPD]
    second = render(enabled(httpd={**ENABLED["httpd"], "serverAliases": ["foreman.example.org"]}))
    annotation = "checksum/config"
    assert (first["spec"]["template"]["metadata"]["annotations"][annotation]
            != second["Deployment", HTTPD]["spec"]["template"]["metadata"]["annotations"][annotation])


def test_pods_carry_labels_the_pulp_network_policy_allows(render):
    labels = render(enabled())["Deployment", HTTPD]["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/component"] == "httpd"
    assert labels["app.kubernetes.io/instance"] == "foreman"


def test_pod_does_not_mount_service_account_token(render):
    assert pod_spec(render(enabled())["Deployment", HTTPD])["automountServiceAccountToken"] is False


@pytest.mark.parametrize("values, release, httpd", [
    ({"fullnameOverride": "corporate"}, "foreman", "corporate-httpd"),
    # The fullname is shortened so that <fullname>-httpd fits in 63 characters.
    ({}, "r" * 53, "r" * 53 + "-for-httpd"),
])
def test_names_follow_fullname_and_backends_exist(render, values, release, httpd):
    manifests = render(enabled(pulp={"enabled": True}, **values), release=release)
    for kind in ("Deployment", "Service", "ConfigMap"):
        assert (kind, httpd) in manifests, kind
    services = {name for kind, name in manifests if kind == "Service"}
    assert all(len(name) <= 63 for name in services), services
    data = manifests["ConfigMap", httpd]["data"]
    for name in ("foreman.conf", "foreman-ssl.conf"):
        backends = set(re.findall(r"http://([^:/\s]+):\d+", data[name]))
        assert backends and backends <= services, (name, backends - services)
        assert f"ServerAlias {httpd} {httpd}.foreman.svc" in data[name]
