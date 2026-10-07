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
    helm upgrade --install "${release}" "${repo_root}/charts/foreman" \
      --kube-context "${context}" --namespace "${namespace}" \
      --set-string foreman.fqdn=foreman.test --set-string foreman.externalUrl=http://foreman.test \
      --wait --wait-for-jobs --timeout 20m
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
