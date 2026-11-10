#!/usr/bin/env bash
# Generate a test CA, a Foreman client certificate, a Candlepin server
# certificate and optionally an HTTPS server certificate and Smart Proxy
# certificates with openssl, and store them as the Secrets the foreman Helm
# chart reads.
#
# Usage: certificates.sh NAMESPACE RELEASE CLIENT_CN [SERVER_SAN...]
#
# Secrets created or updated in NAMESPACE:
#   <release>-ca                Opaque: ca.crt
#   <release>-client-tls        kubernetes.io/tls: tls.crt, tls.key, ca.crt
#   <release>-candlepin-ca      kubernetes.io/tls: the CA itself (tls.crt, tls.key)
#   <release>-candlepin-tls     kubernetes.io/tls: tls.crt, tls.key, ca.crt; SANs
#                               the Candlepin Service name, its in-cluster
#                               DNS names and localhost
#   <release>-server-tls        kubernetes.io/tls: tls.crt, tls.key, ca.crt (only
#                               with SERVER_SAN; the first SAN is also the CN)
#   <release>-proxy-tls         kubernetes.io/tls: tls.crt, tls.key, ca.crt;
#   <release>-proxy-client-tls  Smart Proxy server and client certificates,
#                               only when PROXY_SANS is set (space separated
#                               DNS names, the first is the CN)
#   <release>-capsule-client-tls  kubernetes.io/tls: client certificate of a
#                               Smart Proxy with content (capsule), only when
#                               CAPSULE_NAMES is set (space separated DNS
#                               names, the first is the CN). Its server
#                               certificate is issued into CERT_DIR as
#                               capsule.crt and capsule.key and not applied:
#                               tools/foreman-proxy-bundle.sh takes both pairs
#                               from the files
#
# The Candlepin Service is named <fullname>-candlepin, as the chart names it.
# FULLNAME defaults to the chart's <release>-foreman; set it when the release
# uses fullnameOverride.
#
# Files are kept in CERT_DIR (default artifacts/certificates/<namespace>-<release>).
# Re-running reuses the CA and keys, re-issues a certificate only when its
# names changed, and re-applies the Secrets. Set KUBE_CONTEXT to choose a
# kubectl context. These certificates are for tests only.
set -euo pipefail

if [[ $# -lt 3 ]]; then
  sed -n '6p' "$0" | sed 's/^# //' >&2
  exit 2
fi

namespace=$1
release=$2
client_cn=$3
shift 3
server_sans=("$@")

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
    echo 'subjectKeyIdentifier = hash'
    echo 'authorityKeyIdentifier = keyid'
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
  rm -f "${dir}"/client.names "${dir}"/candlepin.names "${dir}"/server.names \
    "${dir}"/proxy.names "${dir}"/proxy-client.names "${dir}"/capsule-client.names \
    "${dir}"/capsule.names
fi

issue client "${client_cn}" clientAuth "${client_cn}"
# The chart shortens the fullname so that <fullname>-candlepin fits in 63
# characters.
fullname="${FULLNAME:-${release}-foreman}"
candlepin="${fullname:0:53}"
candlepin="${candlepin%-}-candlepin"
issue candlepin "${candlepin}" serverAuth "${candlepin}" "${candlepin}.${namespace}.svc" \
  "${candlepin}.${namespace}.svc.cluster.local" localhost
if [[ ${#server_sans[@]} -gt 0 ]]; then
  issue server "${server_sans[0]}" serverAuth "${server_sans[@]}"
fi

apply_secret() {
  kubectl "${kubectl_args[@]}" create secret generic "$@" --dry-run=client --output=yaml |
    kubectl "${kubectl_args[@]}" apply --filename=-
}

apply_secret "${release}-ca" --from-file=ca.crt="${dir}/ca.crt"
apply_secret "${release}-client-tls" --type=kubernetes.io/tls \
  --from-file=tls.crt="${dir}/client.crt" --from-file=tls.key="${dir}/client.key" \
  --from-file=ca.crt="${dir}/ca.crt"
apply_secret "${release}-candlepin-ca" --type=kubernetes.io/tls \
  --from-file=tls.crt="${dir}/ca.crt" --from-file=tls.key="${dir}/ca.key"
apply_secret "${release}-candlepin-tls" --type=kubernetes.io/tls \
  --from-file=tls.crt="${dir}/candlepin.crt" --from-file=tls.key="${dir}/candlepin.key" \
  --from-file=ca.crt="${dir}/ca.crt"
if [[ ${#server_sans[@]} -gt 0 ]]; then
  apply_secret "${release}-server-tls" --type=kubernetes.io/tls \
    --from-file=tls.crt="${dir}/server.crt" --from-file=tls.key="${dir}/server.key" \
    --from-file=ca.crt="${dir}/ca.crt"
fi

if [[ -n "${PROXY_SANS:-}" ]]; then
  read -r -a proxy_sans <<<"${PROXY_SANS}"
  issue proxy "${proxy_sans[0]}" serverAuth "${proxy_sans[@]}"
  issue proxy-client "${proxy_sans[0]}" clientAuth "${proxy_sans[@]}"
  apply_secret "${release}-proxy-tls" --type=kubernetes.io/tls \
    --from-file=tls.crt="${dir}/proxy.crt" --from-file=tls.key="${dir}/proxy.key" \
    --from-file=ca.crt="${dir}/ca.crt"
  apply_secret "${release}-proxy-client-tls" --type=kubernetes.io/tls \
    --from-file=tls.crt="${dir}/proxy-client.crt" --from-file=tls.key="${dir}/proxy-client.key" \
    --from-file=ca.crt="${dir}/ca.crt"
fi

if [[ -n "${CAPSULE_NAMES:-}" ]]; then
  read -r -a capsule_names <<<"${CAPSULE_NAMES}"
  issue capsule-client "${capsule_names[0]}" clientAuth "${capsule_names[@]}"
  issue capsule "${capsule_names[0]}" serverAuth "${capsule_names[@]}"
  apply_secret "${release}-capsule-client-tls" --type=kubernetes.io/tls \
    --from-file=tls.crt="${dir}/capsule-client.crt" --from-file=tls.key="${dir}/capsule-client.key" \
    --from-file=ca.crt="${dir}/ca.crt"
fi
