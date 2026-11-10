#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cluster_name="${KIND_CLUSTER_NAME:-foreman-minimal}"
node_image="${KIND_NODE_IMAGE:-kindest/node:v1.34.11@sha256:44e222ee2132dab25ff87301682f89eb82c7880ea3a1bf543bfe9708fd08d67d}"
namespace="${KIND_NAMESPACE:-foreman}"
release="${HELM_RELEASE:-foreman}"
# Smart Proxy with content (deploy-content-proxy): its namespace, release,
# host name and the fixed address of its Service in Kind's Service range.
capsule_namespace="${CAPSULE_NAMESPACE:-capsule}"
capsule_release="${CAPSULE_RELEASE:-capsule}"
capsule_name="${CAPSULE_NAME:-capsule.test}"
capsule_cluster_ip="${CAPSULE_CLUSTER_IP:-10.96.0.81}"
context="kind-${cluster_name}"
export KIND_EXPERIMENTAL_PROVIDER="${KIND_EXPERIMENTAL_PROVIDER:-podman}"
export KUBECONFIG="${repo_root}/artifacts/kubeconfig"
ownership_file="${repo_root}/artifacts/kind-cluster"

# In a cluster with worker nodes (KIND_CONFIG), keep the PostgreSQL and Valkey
# fixtures on the control-plane node, so that draining a worker does not lose
# their data. Their first pod is replaced before anything uses them.
pin_fixture() {
  if [[ -n "$(kubectl --context "${context}" get nodes --selector '!node-role.kubernetes.io/control-plane' --output name)" ]]; then
    kubectl --context "${context}" --namespace "${namespace}" patch "deployment/$1" --type merge --patch \
      '{"spec":{"template":{"spec":{"nodeSelector":{"node-role.kubernetes.io/control-plane":""},"tolerations":[{"key":"node-role.kubernetes.io/control-plane","effect":"NoSchedule"}]}}}}'
  fi
}

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
      ${KIND_CONFIG:+--config "${KIND_CONFIG}"} --kubeconfig "${KUBECONFIG}" --wait 120s
    printf '%s\n' "${cluster_name}" > "${ownership_file}"
    ;;
  deploy)
    require_cluster
    kubectl --context "${context}" create namespace "${namespace}" \
      --dry-run=client --output=yaml | kubectl --context "${context}" apply --filename=-
    # Every pod of the release and of the fixtures must meet the restricted
    # Pod Security profile.
    kubectl --context "${context}" label namespace "${namespace}" --overwrite \
      pod-security.kubernetes.io/enforce=restricted pod-security.kubernetes.io/warn=restricted
    kubectl --context "${context}" --namespace "${namespace}" apply \
      --filename "${repo_root}/tests/kind/dependencies.yaml"
    pin_fixture postgresql
    kubectl --context "${context}" --namespace "${namespace}" rollout status deployment/postgresql --timeout=180s
    kubectl --context "${context}" --namespace "${namespace}" create secret generic foreman-runtime \
      --from-literal=DATABASE_URL='postgresql://foreman:foreman-test@postgresql:5432/foreman' \
      --from-literal=ENCRYPTION_KEY='0123456789abcdef0123456789abcdef' \
      --from-literal=SECRET_KEY_BASE='0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef' \
      --from-literal=SEED_ADMIN_USER=admin --from-literal=SEED_ADMIN_PASSWORD=foreman-test \
      --dry-run=client --output=yaml | kubectl --context "${context}" apply --filename=-
    # Test CA, Foreman client certificate (CN is the FQDN), Candlepin server
    # certificate, the httpd server certificate for the FQDN and the
    # in-cluster Service names, the Smart Proxy server and client
    # certificates for the proxy pod's DNS name, and the client certificate
    # of a Smart Proxy with content (capsule.test) that httpd trusts, and its
    # server certificate (files only, for deploy-content-proxy).
    PROXY_SANS="proxy.${release}-foreman-proxy.${namespace}.svc.cluster.local" \
      CAPSULE_NAMES="${capsule_name}" \
      KUBE_CONTEXT="${context}" "${repo_root}/tests/kind/certificates.sh" \
      "${namespace}" "${release}" foreman.test foreman.test "${release}-foreman-httpd" \
      "${release}-foreman-httpd.${namespace}.svc" "${release}-foreman-httpd.${namespace}.svc.cluster.local"
    # Smart Proxy SSH key and an SSH server fixture that trusts it.
    SSH_TARGET=1 KUBE_CONTEXT="${context}" "${repo_root}/tests/kind/proxy-ssh.sh" \
      "${namespace}" "${release}"
    # Valkey shared by Dynflow, the Rails cache and Pulp.
    kubectl --context "${context}" --namespace "${namespace}" apply \
      --filename "${repo_root}/tests/kind/dependencies-valkey.yaml"
    pin_fixture valkey
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
    # OAuth consumer for signed API requests.
    kubectl --context "${context}" --namespace "${namespace}" create secret generic foreman-oauth \
      --from-literal=OAUTH_CONSUMER_KEY=foreman-oauth-test-key \
      --from-literal=OAUTH_CONSUMER_SECRET=foreman-oauth-test-secret \
      --dry-run=client --output=yaml | kubectl --context "${context}" apply --filename=-
    # Yum repository the Katello tests sync, served inside the cluster.
    KUBE_CONTEXT="${context}" "${repo_root}/tests/kind/rpm-repository.sh" "${namespace}"
    # A fixed httpd Service address in Kind's Service range (10.96.0.0/16),
    # so the Foreman pods resolve the public name foreman.test to httpd, and
    # the Smart Proxy with content to its Service.
    httpd_cluster_ip="${HTTPD_CLUSTER_IP:-10.96.0.80}"
    # PULP_STORAGE=s3: Pulp content in a MinIO bucket instead of a volume.
    storage_values=()
    if [[ "${PULP_STORAGE:-filesystem}" == s3 ]]; then
      kubectl --context "${context}" --namespace "${namespace}" create secret generic pulp-s3 \
        --from-literal=AWS_ACCESS_KEY_ID=pulp-test --from-literal=AWS_SECRET_ACCESS_KEY=pulp-s3-test-secret \
        --dry-run=client --output=yaml | kubectl --context "${context}" apply --filename=-
      kubectl --context "${context}" --namespace "${namespace}" apply \
        --filename "${repo_root}/tests/kind/dependencies-minio.yaml"
      kubectl --context "${context}" --namespace "${namespace}" wait job/minio-bucket \
        --for=condition=complete --timeout=180s
      storage_values=(--values "${repo_root}/tests/kind/values-pulp-s3.yaml"
        --values "${repo_root}/tests/kind/values-pulp-s3-workaround.yaml")
    fi
    helm upgrade --install "${release}" "${repo_root}/charts/foreman" \
      --kube-context "${context}" --namespace "${namespace}" \
      --set-string foreman.fqdn=foreman.test --set-string foreman.externalUrl=https://foreman.test \
      --set certificates.enabled=true --set httpd.enabled=true \
      --set 'foreman.trustedProxies={10.244.0.0/16}' \
      --set dynflow.enabled=true --set pulp.enabled=true --set candlepin.enabled=true \
      --set katello.enabled=true --set proxy.enabled=true \
      --set "httpd.trustedProxyClientNames={${capsule_name}}" --set foreman.oauth.existingSecret=foreman-oauth \
      --set-string httpd.service.clusterIP="${httpd_cluster_ip}" \
      --set-string "foreman.hostAliases[0].ip=${httpd_cluster_ip}" --set 'foreman.hostAliases[0].hostnames={foreman.test}' \
      --set-string "foreman.hostAliases[1].ip=${capsule_cluster_ip}" \
      --set "foreman.hostAliases[1].hostnames={${capsule_name}}" \
      --set-string pulp.settings.smartProxyPulpUrl="https://${release}-foreman-httpd.${namespace}.svc" \
      --set-string pulp.settings.contentOrigin=https://foreman.test \
      ${storage_values[@]+"${storage_values[@]}"} --wait --wait-for-jobs --timeout 30m "${@:2}"
    ;;
  deploy-content-proxy)
    # A Smart Proxy with content (foreman-proxy chart) for the release that
    # deploy installed, in its own namespace, named capsule_name.
    require_cluster
    kubectl --context "${context}" create namespace "${capsule_namespace}" \
      --dry-run=client --output=yaml | kubectl --context "${context}" apply --filename=-
    kubectl --context "${context}" label namespace "${capsule_namespace}" --overwrite \
      pod-security.kubernetes.io/enforce=restricted pod-security.kubernetes.io/warn=restricted
    kubectl --context "${context}" --namespace "${capsule_namespace}" apply \
      --filename "${repo_root}/tests/kind/dependencies-content-proxy.yaml" \
      --filename "${repo_root}/tests/kind/dependencies-valkey.yaml"
    kubectl --context "${context}" --namespace "${capsule_namespace}" create secret generic pulp-runtime \
      --from-literal=DATABASE_PASSWORD=pulp-test --from-literal=REDIS_URL=redis://valkey:6379/8 \
      --from-literal=SECRET_KEY='fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210' \
      --from-literal=database_fields.symmetric.key='Yk2vC4m8ZXH0g7Qb3xN1rT6uW9aL5sD2fJ8hK0pE4cI=' \
      --dry-run=client --output=yaml | kubectl --context "${context}" apply --filename=-
    # The identity: CA bundle, server CA and OAuth consumer of the foreman
    # release, and the certificates deploy issued for capsule_name.
    certificates="${CERT_DIR:-${repo_root}/artifacts/certificates/${namespace}-${release}}"
    "${repo_root}/tools/foreman-proxy-bundle.sh" --context "${context}" \
      --namespace "${namespace}" --release "${release}" \
      --server-cert "${certificates}/capsule.crt" --server-key "${certificates}/capsule.key" \
      --client-cert "${certificates}/capsule-client.crt" --client-key "${certificates}/capsule-client.key" \
      --secret "${capsule_release}-bundle" --apply "${capsule_namespace}" "${capsule_name}"
    # The Smart Proxy's own SSH key (not the foreman release's) and an SSH
    # server fixture in its namespace that trusts only that key.
    CERT_DIR="${repo_root}/artifacts/certificates/${capsule_namespace}-${capsule_release}" \
      SSH_TARGET=1 KUBE_CONTEXT="${context}" "${repo_root}/tests/kind/proxy-ssh.sh" \
      "${capsule_namespace}" "${capsule_release}"
    kubectl --context "${context}" --namespace "${capsule_namespace}" rollout status deployment/postgresql --timeout=180s
    kubectl --context "${context}" --namespace "${capsule_namespace}" rollout status deployment/valkey --timeout=180s
    helm upgrade --install "${capsule_release}" "${repo_root}/charts/foreman-proxy" \
      --kube-context "${context}" --namespace "${capsule_namespace}" \
      --set-string name="${capsule_name}" --set-string foreman.url=https://foreman.test \
      --set-string service.clusterIP="${capsule_cluster_ip}" \
      --set-string "hostAliases[0].ip=${HTTPD_CLUSTER_IP:-10.96.0.80}" --set 'hostAliases[0].hostnames={foreman.test}' \
      --set 'registration.lifecycleEnvironments={Library}' \
      --set proxy.remoteExecutionSsh.enabled=true \
      --set-string proxy.remoteExecutionSsh.secretName="${capsule_release}-proxy-ssh" \
      --wait --wait-for-jobs --timeout 20m "${@:2}"
    ;;
  diagnostics)
    require_cluster
    kubectl --context "${context}" --namespace "${namespace}" get deployments,pods,services,jobs
    kubectl --context "${context}" --namespace "${namespace}" get events --sort-by=.lastTimestamp
    kubectl --context "${context}" --namespace "${namespace}" logs \
      --selector "app.kubernetes.io/instance=${release}" --all-containers --tail=100 --prefix
    if kubectl --context "${context}" get namespace "${capsule_namespace}" >/dev/null 2>&1; then
      kubectl --context "${context}" --namespace "${capsule_namespace}" get deployments,pods,services,jobs
      kubectl --context "${context}" --namespace "${capsule_namespace}" get events --sort-by=.lastTimestamp
      kubectl --context "${context}" --namespace "${capsule_namespace}" logs \
        --selector "app.kubernetes.io/instance=${capsule_release}" --all-containers --tail=100 --prefix
    fi
    ;;
  teardown)
    if [[ -f "${ownership_file}" ]]; then
      require_cluster
      kind delete cluster --name "${cluster_name}"
      rm -f "${ownership_file}" "${KUBECONFIG}"
    fi
    ;;
  *)
    echo 'Usage: run-minimal.sh {setup|deploy [helm option ...]|deploy-content-proxy [helm option ...]|diagnostics|teardown}' >&2
    exit 2
    ;;
esac
