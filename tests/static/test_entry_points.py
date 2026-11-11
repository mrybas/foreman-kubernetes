"""Static assertions on the entry points in front of httpd: NodePort, Ingress,
Gateway API and Traefik, which all pass TLS through to httpd."""

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FOREMAN = {"chart": ROOT / "charts" / "foreman", "release": "foreman", "namespace": "foreman"}
PROXY = {"chart": ROOT / "charts" / "foreman-proxy", "release": "capsule", "namespace": "capsule"}
HTTPD = "foreman-foreman-httpd"
CAPSULE = "capsule-foreman-proxy"
FQDN = "foreman.example.test"
HTTPS_ENABLED = {"foreman": {"fqdn": FQDN}, "certificates": {"enabled": True},
                 "httpd": {"enabled": True}}
PROXY_REQUIRED = {"name": "capsule.example.test", "foreman": {"url": "https://foreman.example.test"}}
GATEWAY_V1 = ("gateway.networking.k8s.io/v1/TLSRoute", "gateway.networking.k8s.io/v1/HTTPRoute",
              "gateway.networking.k8s.io/v1/Gateway")
GATEWAY_V1ALPHA2 = ("gateway.networking.k8s.io/v1alpha2/TLSRoute",
                    "gateway.networking.k8s.io/v1/HTTPRoute", "gateway.networking.k8s.io/v1/Gateway")
TRAEFIK = ("traefik.io/v1alpha1/IngressRouteTCP", "traefik.io/v1alpha1/IngressRoute")
EXISTING = [{"name": "shared", "namespace": "gateways"}]


def merge(base, override):
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def helm_template(chart, values, tmp_path, api_versions=()):
    values_file = tmp_path / "values.yaml"
    values_file.write_text(yaml.safe_dump(values))
    command = ["helm", "template", chart["release"], str(chart["chart"]),
               "--namespace", chart["namespace"], "--values", str(values_file)]
    for api in api_versions:
        command += ["--api-versions", api]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def documents(result):
    assert result.returncode == 0, result.stderr
    manifests = {}
    for doc in yaml.safe_load_all(result.stdout):
        if doc:
            key = (doc["kind"], doc["metadata"]["name"])
            assert key not in manifests, f"duplicate manifest {key}"
            manifests[key] = doc
    return manifests


@pytest.fixture
def foreman(tmp_path):
    def _render(httpd=None, api_versions=(), **values):
        values = merge(HTTPS_ENABLED, values)
        values["httpd"] = merge(values["httpd"], httpd or {})
        return documents(helm_template(FOREMAN, values, tmp_path, api_versions))
    return _render


@pytest.fixture
def proxy(tmp_path):
    def _render(api_versions=(), **values):
        return documents(helm_template(PROXY, merge(PROXY_REQUIRED, values), tmp_path, api_versions))
    return _render


ENTRY_KINDS = ("Ingress", "Gateway", "TLSRoute", "HTTPRoute", "IngressRouteTCP", "IngressRoute")


def entry_kinds(manifests):
    return sorted(kind for kind, _ in manifests if kind in ENTRY_KINDS)


def test_defaults_render_no_entry_objects(foreman, proxy):
    manifests = foreman()
    assert entry_kinds(manifests) == []
    assert manifests["Service", HTTPD]["spec"]["type"] == "ClusterIP"
    assert entry_kinds(proxy()) == []


def test_node_ports(foreman, proxy):
    manifests = foreman(httpd={"service": {"type": "NodePort", "externalTrafficPolicy": "Local",
                                           "nodePorts": {"http": 30080, "https": "30443"}}})
    service = manifests["Service", HTTPD]["spec"]
    assert service["type"] == "NodePort"
    assert service["externalTrafficPolicy"] == "Local"
    assert {port["name"]: port["nodePort"] for port in service["ports"]} == {"http": 30080, "https": 30443}
    # Without fixed ports Kubernetes chooses them.
    service = foreman(httpd={"service": {"type": "NodePort"}})["Service", HTTPD]["spec"]
    assert all("nodePort" not in port for port in service["ports"])
    service = proxy(service={"type": "NodePort", "nodePorts": {"proxyHttps": 30843}})["Service", CAPSULE]["spec"]
    assert {port["name"]: port.get("nodePort") for port in service["ports"]} == {
        "http": None, "https": None, "proxy-https": 30843}


def test_ingress_passes_tls_to_the_https_port(foreman):
    annotations = {"nginx.ingress.kubernetes.io/ssl-passthrough": "true"}
    manifests = foreman(httpd={"serverAliases": ["foreman-alt.example.test"], "ingress": {
        "enabled": True, "className": "nginx", "annotations": annotations}})
    ingress = manifests["Ingress", HTTPD]
    assert ingress["metadata"]["annotations"] == annotations
    assert ingress["spec"]["ingressClassName"] == "nginx"
    assert "tls" not in ingress["spec"]
    rules = ingress["spec"]["rules"]
    assert [rule["host"] for rule in rules] == [FQDN, "foreman-alt.example.test"]
    for rule in rules:
        paths = {path["path"]: path for path in rule["http"]["paths"]}
        assert list(paths) == ["/", "/pub", "/pulp/content", "/pulp/isos"]
        for name, path in paths.items():
            assert path["pathType"] == "Prefix"
            assert path["backend"]["service"] == {
                "name": HTTPD, "port": {"name": "https" if name == "/" else "http"}}


def test_ingress_without_http_paths_or_class(foreman):
    ingress = foreman(httpd={"ingress": {"enabled": True, "httpPaths": []}})["Ingress", HTTPD]
    assert "ingressClassName" not in ingress["spec"]
    assert "annotations" not in ingress["metadata"]
    (rule,) = ingress["spec"]["rules"]
    assert [path["path"] for path in rule["http"]["paths"]] == ["/"]


@pytest.mark.parametrize("api_versions, tls_api", [
    (GATEWAY_V1, "gateway.networking.k8s.io/v1"),
    (GATEWAY_V1ALPHA2, "gateway.networking.k8s.io/v1alpha2"),
    (GATEWAY_V1 + ("gateway.networking.k8s.io/v1alpha2/TLSRoute",), "gateway.networking.k8s.io/v1"),
])
def test_gateway_routes_attach_to_an_existing_gateway(foreman, api_versions, tls_api):
    manifests = foreman(api_versions=api_versions,
                        httpd={"gateway": {"enabled": True, "parentRefs": EXISTING}})
    assert entry_kinds(manifests) == ["HTTPRoute", "TLSRoute"]
    tls = manifests["TLSRoute", HTTPD]
    assert tls["apiVersion"] == tls_api
    assert tls["spec"]["parentRefs"] == [{**EXISTING[0], "sectionName": "https"}]
    assert tls["spec"]["hostnames"] == [FQDN]
    assert tls["spec"]["rules"] == [{"backendRefs": [{"name": HTTPD, "port": 443}]}]
    http = manifests["HTTPRoute", HTTPD]
    assert http["apiVersion"] == "gateway.networking.k8s.io/v1"
    assert http["spec"]["parentRefs"] == [{**EXISTING[0], "sectionName": "http"}]
    assert http["spec"]["rules"] == [{"backendRefs": [{"name": HTTPD, "port": 80}]}]


def test_gateway_without_listener_names_or_http_route(foreman):
    manifests = foreman(api_versions=GATEWAY_V1, httpd={"gateway": {
        "enabled": True, "parentRefs": EXISTING, "http": False, "listeners": {"https": ""}}})
    assert entry_kinds(manifests) == ["TLSRoute"]
    assert manifests["TLSRoute", HTTPD]["spec"]["parentRefs"] == EXISTING


def test_gateway_created_by_the_chart(foreman):
    manifests = foreman(api_versions=GATEWAY_V1, httpd={"gateway": {
        "enabled": True, "create": {"enabled": True, "className": "eg",
                                    "annotations": {"example.test/ip": "192.0.2.1"}}}})
    assert entry_kinds(manifests) == ["Gateway", "HTTPRoute", "TLSRoute"]
    gateway = manifests["Gateway", HTTPD]
    assert gateway["metadata"]["annotations"] == {"example.test/ip": "192.0.2.1"}
    assert gateway["spec"]["gatewayClassName"] == "eg"
    listeners = {listener["name"]: listener for listener in gateway["spec"]["listeners"]}
    assert listeners["https"]["port"] == 443
    assert listeners["https"]["protocol"] == "TLS"
    assert listeners["https"]["tls"] == {"mode": "Passthrough"}
    assert listeners["https"]["allowedRoutes"]["kinds"] == [{"kind": "TLSRoute"}]
    assert listeners["http"]["port"] == 80
    assert listeners["http"]["protocol"] == "HTTP"
    assert "tls" not in listeners["http"]
    for listener in listeners.values():
        assert listener["allowedRoutes"]["namespaces"] == {"from": "Same"}
    assert manifests["TLSRoute", HTTPD]["spec"]["parentRefs"] == [{"name": HTTPD, "sectionName": "https"}]
    assert manifests["HTTPRoute", HTTPD]["spec"]["parentRefs"] == [{"name": HTTPD, "sectionName": "http"}]


def test_traefik_routes(foreman):
    manifests = foreman(api_versions=TRAEFIK, httpd={
        "serverAliases": ["foreman-alt.example.test"], "traefik": {"enabled": True}})
    tcp = manifests["IngressRouteTCP", HTTPD]
    assert tcp["spec"]["entryPoints"] == ["websecure"]
    assert tcp["spec"]["tls"] == {"passthrough": True}
    (route,) = tcp["spec"]["routes"]
    assert route["match"] == f"HostSNI(`{FQDN}`) || HostSNI(`foreman-alt.example.test`)"
    assert route["services"] == [{"name": HTTPD, "port": 443}]
    http = manifests["IngressRoute", HTTPD]
    assert http["spec"]["entryPoints"] == ["web"]
    (route,) = http["spec"]["routes"]
    assert route["match"] == f"Host(`{FQDN}`) || Host(`foreman-alt.example.test`)"
    assert route["services"] == [{"name": HTTPD, "port": 80}]
    manifests = foreman(api_versions=TRAEFIK, httpd={"traefik": {"enabled": True, "httpEntryPoints": []}})
    assert entry_kinds(manifests) == ["IngressRouteTCP"]


def test_entry_objects_have_standard_labels(foreman):
    manifests = foreman(api_versions=GATEWAY_V1 + TRAEFIK, httpd={
        "ingress": {"enabled": True}, "traefik": {"enabled": True},
        "gateway": {"enabled": True, "create": {"enabled": True, "className": "eg"}}})
    assert entry_kinds(manifests) == sorted(ENTRY_KINDS)
    for (kind, name), doc in manifests.items():
        if kind in ENTRY_KINDS:
            labels = doc["metadata"]["labels"]
            assert labels["app.kubernetes.io/instance"] == "foreman", (kind, name)
            assert labels["app.kubernetes.io/component"] == "httpd", (kind, name)


def test_proxy_gateway_routes_both_tls_ports(proxy):
    manifests = proxy(api_versions=GATEWAY_V1, serverAliases=["capsule-alt.example.test"],
                      gateway={"enabled": True, "create": {"enabled": True, "className": "eg"}})
    assert entry_kinds(manifests) == ["Gateway", "HTTPRoute", "TLSRoute", "TLSRoute"]
    listeners = {listener["name"]: (listener["port"], listener["protocol"], listener.get("tls"))
                 for listener in manifests["Gateway", CAPSULE]["spec"]["listeners"]}
    passthrough = {"mode": "Passthrough"}
    assert listeners == {"https": (443, "TLS", passthrough), "proxy-https": (8443, "TLS", passthrough),
                         "http": (80, "HTTP", None)}
    hosts = ["capsule.example.test", "capsule-alt.example.test"]
    for name, listener, port in ((CAPSULE, "https", 443), (f"{CAPSULE}-proxy", "proxy-https", 8443)):
        spec = manifests["TLSRoute", name]["spec"]
        assert spec["parentRefs"] == [{"name": CAPSULE, "sectionName": listener}]
        assert spec["hostnames"] == hosts
        assert spec["rules"] == [{"backendRefs": [{"name": CAPSULE, "port": port}]}]
    spec = manifests["HTTPRoute", CAPSULE]["spec"]
    assert spec["rules"] == [{"backendRefs": [{"name": CAPSULE, "port": 80}]}]


def test_proxy_gateway_follows_the_proxy_port(proxy):
    manifests = proxy(api_versions=GATEWAY_V1, proxy={"port": 9090}, gateway={
        "enabled": True, "parentRefs": EXISTING, "listeners": {"proxyHttps": "smart-proxy"}})
    spec = manifests["TLSRoute", f"{CAPSULE}-proxy"]["spec"]
    assert spec["parentRefs"] == [{**EXISTING[0], "sectionName": "smart-proxy"}]
    assert spec["rules"] == [{"backendRefs": [{"name": CAPSULE, "port": 9090}]}]


GATEWAY_MISSING_TLSROUTE = "needs the Gateway API TLSRoute"


@pytest.mark.parametrize("httpd, api_versions, message", [
    ({"enabled": False, "ingress": {"enabled": True}}, (), "httpd.ingress.enabled needs httpd.enabled"),
    ({"enabled": False, "gateway": {"enabled": True}}, (), "httpd.gateway.enabled needs httpd.enabled"),
    ({"enabled": False, "traefik": {"enabled": True}}, (), "httpd.traefik.enabled needs httpd.enabled"),
    ({"service": {"nodePorts": {"https": 30443}}}, (),
     "httpd.service.nodePorts.https needs httpd.service.type NodePort or LoadBalancer"),
    ({"gateway": {"enabled": True}}, GATEWAY_V1, "needs parentRefs (an existing Gateway) or create.enabled"),
    ({"gateway": {"enabled": True, "create": {"enabled": True}}}, GATEWAY_V1,
     "httpd.gateway.create.className is required"),
    ({"gateway": {"enabled": True, "parentRefs": EXISTING, "create": {"enabled": True, "className": "eg"}}},
     GATEWAY_V1, "set either parentRefs or create.enabled, not both"),
    ({"gateway": {"enabled": True, "parentRefs": [{"name": "shared", "sectionName": "tls"}]}},
     GATEWAY_V1, "set listener names in httpd.gateway.listeners, not sectionName"),
    ({"gateway": {"enabled": True, "parentRefs": EXISTING}}, (), GATEWAY_MISSING_TLSROUTE),
    ({"gateway": {"enabled": True, "parentRefs": EXISTING}},
     ("gateway.networking.k8s.io/v1/HTTPRoute", "gateway.networking.k8s.io/v1/Gateway"),
     GATEWAY_MISSING_TLSROUTE),
    ({"gateway": {"enabled": True, "parentRefs": EXISTING}}, ("gateway.networking.k8s.io/v1/TLSRoute",),
     "httpd.gateway needs the Gateway API HTTPRoute"),
    ({"gateway": {"enabled": True, "create": {"enabled": True, "className": "eg"}}},
     ("gateway.networking.k8s.io/v1/TLSRoute", "gateway.networking.k8s.io/v1/HTTPRoute"),
     "httpd.gateway needs the Gateway API Gateway"),
    ({"traefik": {"enabled": True}}, (), "httpd.traefik.enabled needs the Traefik CRDs"),
])
def test_invalid_foreman_values_fail(tmp_path, httpd, api_versions, message):
    values = merge(HTTPS_ENABLED, {"httpd": httpd})
    result = helm_template(FOREMAN, values, tmp_path, api_versions)
    assert result.returncode != 0
    assert message in result.stderr, result.stderr


def test_gateway_without_http_route_needs_no_httproute_api(foreman):
    manifests = foreman(api_versions=("gateway.networking.k8s.io/v1/TLSRoute",),
                        httpd={"gateway": {"enabled": True, "parentRefs": EXISTING, "http": False}})
    assert entry_kinds(manifests) == ["TLSRoute"]


@pytest.mark.parametrize("values, api_versions, message", [
    ({"service": {"nodePorts": {"proxyHttps": 30843}}}, (),
     "service.nodePorts.proxyHttps needs service.type NodePort or LoadBalancer"),
    ({"gateway": {"enabled": True, "parentRefs": EXISTING, "listeners": {"proxyHttps": ""}}}, GATEWAY_V1,
     "gateway.listeners.https and gateway.listeners.proxyHttps are required"),
    ({"gateway": {"enabled": True}}, GATEWAY_V1, "gateway.enabled needs parentRefs"),
    ({"gateway": {"enabled": True, "parentRefs": EXISTING}}, (), GATEWAY_MISSING_TLSROUTE),
])
def test_invalid_proxy_values_fail(tmp_path, values, api_versions, message):
    result = helm_template(PROXY, merge(PROXY_REQUIRED, values), tmp_path, api_versions)
    assert result.returncode != 0
    assert message in result.stderr, result.stderr
