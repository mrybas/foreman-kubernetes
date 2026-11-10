#!/usr/bin/env bash
# Create the SSH key pair the Smart Proxy uses for Remote Execution and store
# it as a Secret (the foreman chart's default name; the foreman-proxy chart
# takes it in proxy.remoteExecutionSsh.secretName). With SSH_TARGET=1 also deploy
# an SSH server fixture (tests/kind/dependencies-proxy.yaml) that trusts the key.
#
# Usage: proxy-ssh.sh NAMESPACE RELEASE
#
# Secret created or updated in NAMESPACE:
#   <release>-proxy-ssh  Opaque: id_rsa_foreman_proxy, id_rsa_foreman_proxy.pub
#
# The key is kept in CERT_DIR (default artifacts/certificates/<namespace>-<release>)
# and reused on the next run. Set KUBE_CONTEXT to choose a kubectl context.
# This key is for tests only.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  sed -n '7p' "$0" | sed 's/^# //' >&2
  exit 2
fi

namespace=$1
release=$2

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
dir="${CERT_DIR:-${repo_root}/artifacts/certificates/${namespace}-${release}}"
key="${dir}/id_rsa_foreman_proxy"
kubectl_args=(--namespace "${namespace}")
if [[ -n "${KUBE_CONTEXT:-}" ]]; then
  kubectl_args+=(--context "${KUBE_CONTEXT}")
fi

mkdir -p "${dir}"
chmod 700 "${dir}"
umask 077

[[ -f "${key}" ]] || ssh-keygen -q -t rsa -b 4096 -N '' -C "foreman-proxy@${release}" -f "${key}"

apply_secret() {
  kubectl "${kubectl_args[@]}" create secret generic "$@" --dry-run=client --output=yaml |
    kubectl "${kubectl_args[@]}" apply --filename=-
}

apply_secret "${release}-proxy-ssh" \
  --from-file=id_rsa_foreman_proxy="${key}" --from-file=id_rsa_foreman_proxy.pub="${key}.pub"

if [[ "${SSH_TARGET:-0}" == 1 ]]; then
  apply_secret ssh-target --from-file=authorized_keys="${key}.pub"
  kubectl "${kubectl_args[@]}" apply --filename "${repo_root}/tests/kind/dependencies-proxy.yaml"
  kubectl "${kubectl_args[@]}" rollout status deployment/ssh-target --timeout=180s
fi
