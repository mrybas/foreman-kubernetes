#!/usr/bin/env bash
# Install the proxies that make test-entry puts in front of httpd, both with
# TLS passthrough: ingress-nginx started with --enable-ssl-passthrough
# (IngressClass nginx) and Envoy Gateway with the Gateway API CRDs it bundles
# (GatewayClass eg). Both expose node ports, so the tests reach them on the
# Kind node address without a load balancer.
#
# Usage: entry-points.sh
# Set KUBE_CONTEXT to choose a kubectl context.
set -euo pipefail

# ingress-nginx is archived upstream; its last chart is still the reference
# for the ssl-passthrough annotation. Envoy Gateway v1.10.0 bundles Gateway API
# v1.6.1, which serves TLSRoute as gateway.networking.k8s.io/v1.
ingress_nginx_chart_version="${INGRESS_NGINX_CHART_VERSION:-4.15.1}"
envoy_gateway_chart_version="${ENVOY_GATEWAY_CHART_VERSION:-v1.10.0}"
kubectl_args=()
helm_args=()
if [[ -n "${KUBE_CONTEXT:-}" ]]; then
  kubectl_args+=(--context "${KUBE_CONTEXT}")
  helm_args+=(--kube-context "${KUBE_CONTEXT}")
fi

helm upgrade --install ingress-nginx ingress-nginx \
  --repo https://kubernetes.github.io/ingress-nginx --version "${ingress_nginx_chart_version}" \
  ${helm_args[@]+"${helm_args[@]}"} --namespace ingress-nginx --create-namespace \
  --set controller.extraArgs.enable-ssl-passthrough=true \
  --set controller.service.type=NodePort \
  --set controller.admissionWebhooks.enabled=false \
  --set controller.ingressClassResource.name=nginx \
  --wait --timeout 10m

helm upgrade --install envoy-gateway oci://docker.io/envoyproxy/gateway-helm \
  --version "${envoy_gateway_chart_version}" \
  ${helm_args[@]+"${helm_args[@]}"} --namespace envoy-gateway-system --create-namespace \
  --wait --timeout 10m

kubectl ${kubectl_args[@]+"${kubectl_args[@]}"} apply --filename=- <<'YAML'
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: eg
spec:
  controllerName: gateway.envoyproxy.io/gatewayclass-controller
YAML
kubectl ${kubectl_args[@]+"${kubectl_args[@]}"} wait gatewayclass/eg --for=condition=Accepted --timeout=180s
