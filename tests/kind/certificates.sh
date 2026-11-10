#!/usr/bin/env bash
# Generate a test CA and a client certificate with openssl, and store them as
# the Secrets the foreman Helm chart reads.
#
# Usage: certificates.sh NAMESPACE RELEASE CLIENT_CN
#
# Secrets created or updated in NAMESPACE:
#   <release>-ca          Opaque: ca.crt
#   <release>-client-tls  kubernetes.io/tls: tls.crt, tls.key, ca.crt
#
# Files are kept in CERT_DIR (default artifacts/certificates/<namespace>-<release>).
# Re-running reuses the CA and key, re-issues the certificate only when its
# name changed, and re-applies the Secrets. Set KUBE_CONTEXT to choose a
# kubectl context. These certificates are for tests only.
set -euo pipefail

if [[ $# -ne 3 ]]; then
  sed -n '5p' "$0" | sed 's/^# //' >&2
  exit 2
fi

namespace=$1
release=$2
client_cn=$3

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
dir="${CERT_DIR:-${repo_root}/artifacts/certificates/${namespace}-${release}}"
days=365
kubectl_args=(--namespace "${namespace}")
if [[ -n "${KUBE_CONTEXT:-}" ]]; then
  kubectl_args+=(--context "${KUBE_CONTEXT}")
fi

mkdir -p "${dir}"
chmod 700 "${dir}"
umask 077

new_key() {
  [[ -f "$1" ]] || openssl genrsa -out "$1" 4096 2>/dev/null
}

# issue NAME CN EXTENDED_KEY_USAGE SAN...: sign NAME.crt with the CA unless the
# existing certificate already has the same CN and SANs.
issue() {
  local name=$1 cn=$2 usage=$3 san wanted
  shift 3
  wanted="${cn} ${usage} $*"
  if [[ -f "${dir}/${name}.crt" ]] && [[ "$(cat "${dir}/${name}.names" 2>/dev/null)" == "${wanted}" ]]; then
    return
  fi
  new_key "${dir}/${name}.key"
  {
    echo 'basicConstraints = critical, CA:FALSE'
    echo 'keyUsage = critical, digitalSignature, keyEncipherment'
    echo "extendedKeyUsage = ${usage}"
    printf 'subjectAltName = '
    local sep=''
    for san in "$@"; do
      printf '%sDNS:%s' "${sep}" "${san}"
      sep=','
    done
    echo
  } >"${dir}/${name}.ext"
  openssl req -new -key "${dir}/${name}.key" -subj "/CN=${cn}" -out "${dir}/${name}.csr"
  openssl x509 -req -in "${dir}/${name}.csr" -CA "${dir}/ca.crt" -CAkey "${dir}/ca.key" \
    -CAcreateserial -days "${days}" -sha256 -extfile "${dir}/${name}.ext" \
    -out "${dir}/${name}.crt" 2>/dev/null
  printf '%s' "${wanted}" >"${dir}/${name}.names"
}

if [[ ! -f "${dir}/ca.crt" ]]; then
  new_key "${dir}/ca.key"
  cat >"${dir}/ca.cnf" <<'EOF'
[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[dn]
CN = Foreman test CA
[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign, digitalSignature
subjectKeyIdentifier = hash
EOF
  openssl req -x509 -new -key "${dir}/ca.key" -config "${dir}/ca.cnf" -days "${days}" \
    -sha256 -out "${dir}/ca.crt"
  # A new CA invalidates every certificate it did not sign.
  rm -f "${dir}"/client.names
fi

issue client "${client_cn}" clientAuth "${client_cn}"

apply_secret() {
  kubectl "${kubectl_args[@]}" create secret generic "$@" --dry-run=client --output=yaml |
    kubectl "${kubectl_args[@]}" apply --filename=-
}

apply_secret "${release}-ca" --from-file=ca.crt="${dir}/ca.crt"
apply_secret "${release}-client-tls" --type=kubernetes.io/tls \
  --from-file=tls.crt="${dir}/client.crt" --from-file=tls.key="${dir}/client.key" \
  --from-file=ca.crt="${dir}/ca.crt"
