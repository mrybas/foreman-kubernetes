#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cluster_name="${KIND_CLUSTER_NAME:-foreman-minimal}"
node_image="${KIND_NODE_IMAGE:-kindest/node:v1.34.11@sha256:44e222ee2132dab25ff87301682f89eb82c7880ea3a1bf543bfe9708fd08d67d}"
namespace="${KIND_NAMESPACE:-foreman}"
release="${HELM_RELEASE:-foreman}"
context="kind-${cluster_name}"
export KIND_EXPERIMENTAL_PROVIDER="${KIND_EXPERIMENTAL_PROVIDER:-podman}"
export KUBECONFIG="${repo_root}/artifacts/kubeconfig"
ownership_file="${repo_root}/artifacts/kind-cluster"

require_cluster() {
  if [[ ! -f "${ownership_file}" ]] || [[ "$(cat "${ownership_file}")" != "${cluster_name}" ]]; then
    echo 'Run make setup-kind first; refusing to modify an unowned cluster.' >&2
    exit 1
  fi
}

case "${1:-}" in
  setup)
    mkdir -p "${repo_root}/artifacts"
    if kind get clusters | grep -Fxq "${cluster_name}"; then
      echo "Cluster ${cluster_name} already exists. Choose another KIND_CLUSTER_NAME or tear down the test cluster first." >&2
      exit 1
    fi
    kind create cluster --name "${cluster_name}" --image "${node_image}" \
      --kubeconfig "${KUBECONFIG}" --wait 120s
    printf '%s\n' "${cluster_name}" > "${ownership_file}"
    ;;
  deploy)
    require_cluster
    kubectl --context "${context}" create namespace "${namespace}" \
      --dry-run=client --output=yaml | kubectl --context "${context}" apply --filename=-
    kubectl --context "${context}" --namespace "${namespace}" apply \
      --filename "${repo_root}/tests/kind/dependencies.yaml"
    kubectl --context "${context}" --namespace "${namespace}" rollout status deployment/postgresql --timeout=180s
    kubectl --context "${context}" --namespace "${namespace}" create secret generic foreman-runtime \
      --from-literal=DATABASE_URL='postgresql://foreman:foreman-test@postgresql:5432/foreman' \
      --from-literal=ENCRYPTION_KEY='0123456789abcdef0123456789abcdef' \
      --from-literal=SECRET_KEY_BASE='0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef' \
      --from-literal=SEED_ADMIN_USER=admin --from-literal=SEED_ADMIN_PASSWORD=foreman-test \
      --dry-run=client --output=yaml | kubectl --context "${context}" apply --filename=-
    # Test CA, Foreman client certificate (CN is the FQDN), Candlepin server
    # certificate, the httpd server certificate for the FQDN and the
    # in-cluster Service names, and the Smart Proxy server and client
    # certificates for the proxy pod's DNS name.
    PROXY_SANS="proxy.${release}-foreman-proxy.${namespace}.svc.cluster.local" \
      KUBE_CONTEXT="${context}" "${repo_root}/tests/kind/certificates.sh" \
      "${namespace}" "${release}" foreman.test foreman.test "${release}-foreman-httpd" \
      "${release}-foreman-httpd.${namespace}.svc" "${release}-foreman-httpd.${namespace}.svc.cluster.local"
    # Smart Proxy SSH key and an SSH server fixture that trusts it.
    SSH_TARGET=1 KUBE_CONTEXT="${context}" "${repo_root}/tests/kind/proxy-ssh.sh" \
      "${namespace}" "${release}"
    # Valkey shared by Dynflow, the Rails cache and Pulp.
    kubectl --context "${context}" --namespace "${namespace}" apply \
      --filename "${repo_root}/tests/kind/dependencies-valkey.yaml"
    kubectl --context "${context}" --namespace "${namespace}" rollout status deployment/valkey --timeout=180s
    # Dynflow: Valkey URL in the runtime Secret.
    kubectl --context "${context}" --namespace "${namespace}" patch secret foreman-runtime \
      --type merge --patch '{"stringData":{"REDIS_URL":"redis://valkey:6379"}}'
    # Pulp: the pulp database and test credentials.
    kubectl --context "${context}" --namespace "${namespace}" apply \
      --filename "${repo_root}/tests/kind/dependencies-pulp.yaml"
    kubectl --context "${context}" --namespace "${namespace}" wait job/pulp-database --for=condition=complete --timeout=180s
    kubectl --context "${context}" --namespace "${namespace}" create secret generic pulp-runtime \
      --from-literal=DATABASE_PASSWORD=pulp-test --from-literal=REDIS_URL=redis://valkey:6379/8 \
      --from-literal=SECRET_KEY='0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef' \
      --from-literal=database_fields.symmetric.key='NrWyrMPhzbM0U78AQbiwfU1Lde28CSK_-Eu3i0A-TUo=' \
      --dry-run=client --output=yaml | kubectl --context "${context}" apply --filename=-
    # Candlepin database, credentials and OAuth secret for Katello.
    kubectl --context "${context}" --namespace "${namespace}" apply \
      --filename "${repo_root}/tests/kind/dependencies-candlepin.yaml"
    kubectl --context "${context}" --namespace "${namespace}" wait job/candlepin-database \
      --for=condition=complete --timeout=180s
    kubectl --context "${context}" --namespace "${namespace}" create secret generic candlepin-runtime \
      --from-literal=DATABASE_URL='jdbc:postgresql://postgresql:5432/candlepin?sslmode=disable' \
      --from-literal=DATABASE_USERNAME=candlepin --from-literal=DATABASE_PASSWORD=candlepin-test \
      --from-literal=OAUTH_SECRET=candlepin-oauth-test \
      --dry-run=client --output=yaml | kubectl --context "${context}" apply --filename=-
    # Yum repository the Katello tests sync, served inside the cluster.
    KUBE_CONTEXT="${context}" "${repo_root}/tests/kind/rpm-repository.sh" "${namespace}"
    helm upgrade --install "${release}" "${repo_root}/charts/foreman" \
      --kube-context "${context}" --namespace "${namespace}" \
      --set-string foreman.fqdn=foreman.test --set-string foreman.externalUrl=https://foreman.test \
      --set certificates.enabled=true --set httpd.enabled=true \
      --set 'foreman.trustedProxies={10.244.0.0/16}' \
      --set dynflow.enabled=true --set pulp.enabled=true --set candlepin.enabled=true \
      --set katello.enabled=true --set proxy.enabled=true \
      --set-string pulp.settings.smartProxyPulpUrl="https://${release}-foreman-httpd.${namespace}.svc" \
      --set-string pulp.settings.contentOrigin=https://foreman.test \
      --wait --wait-for-jobs --timeout 30m
    ;;
  diagnostics)
    require_cluster
    kubectl --context "${context}" --namespace "${namespace}" get deployments,pods,services,jobs
    kubectl --context "${context}" --namespace "${namespace}" get events --sort-by=.lastTimestamp
    kubectl --context "${context}" --namespace "${namespace}" logs \
      --selector "app.kubernetes.io/instance=${release}" --all-containers --tail=100 --prefix
    ;;
  teardown)
    if [[ -f "${ownership_file}" ]]; then
      require_cluster
      kind delete cluster --name "${cluster_name}"
      rm -f "${ownership_file}" "${KUBECONFIG}"
    fi
    ;;
  *)
    echo 'Usage: run-minimal.sh {setup|deploy|diagnostics|teardown}' >&2
    exit 2
    ;;
esac
