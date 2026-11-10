#!/usr/bin/env bash
# Build the bundle Secret a release of the foreman-proxy Helm chart reads: the
# identity of one Smart Proxy with content, the Kubernetes form of foremanctl's
# auth bundle.
#
# Usage: tools/foreman-proxy-bundle.sh [OPTIONS] NAME
#
# NAME is the host name of the Smart Proxy (the foreman-proxy chart's name).
# The script reads the CA bundle, the server CA and the OAuth consumer of a
# foreman chart release, takes the Smart Proxy certificates from files, checks
# that they fit together, and writes one Secret with the keys ca.crt,
# server-ca.crt, tls.crt, tls.key, client.crt, client.key, oauth-consumer-key
# and oauth-consumer-secret.
#
# Options:
#   -n, --namespace NAME   namespace of the foreman release (default foreman)
#   -r, --release NAME     foreman release name (default foreman)
#   --context NAME         kubeconfig context of the foreman release
#   --server-cert FILE     server certificate of the Smart Proxy; NAME must be
#                          one of its DNS names and the server CA must sign it
#   --server-key FILE      its private key
#   --client-cert FILE     client certificate; its CN must be NAME and the CA
#                          bundle of the foreman release must sign it
#   --client-key FILE      its private key
#   --secret NAME          name of the Secret, required (the foreman-proxy
#                          chart reads <release>-bundle by default)
#   --apply NAMESPACE      create or update the Secret in this namespace
#   --apply-context NAME   kubeconfig context for --apply (default --context)
#   --output FILE          write the Secret manifest to FILE (mode 0600)
#                          instead of applying it
#
# One of --apply and --output is required. The script never prints the
# Secret: the bundle holds private keys and an OAuth consumer that acts as a
# Foreman administrator. The foreman release must set
# foreman.oauth.existingSecret.
set -euo pipefail

usage() {
  sed -n '6,36p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

die() {
  echo "foreman-proxy-bundle: $*" >&2
  exit 1
}

namespace=foreman
release=foreman
context=
apply_namespace=
apply_context=
output=
secret=
name=
server_cert=
server_key=
client_cert=
client_key=
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n | --namespace) namespace=$2; shift 2 ;;
    -r | --release) release=$2; shift 2 ;;
    --context) context=$2; shift 2 ;;
    --server-cert) server_cert=$2; shift 2 ;;
    --server-key) server_key=$2; shift 2 ;;
    --client-cert) client_cert=$2; shift 2 ;;
    --client-key) client_key=$2; shift 2 ;;
    --secret) secret=$2; shift 2 ;;
    --apply) apply_namespace=$2; shift 2 ;;
    --apply-context) apply_context=$2; shift 2 ;;
    --output) output=$2; shift 2 ;;
    -h | --help) usage ;;
    -*) usage ;;
    *) [[ -z "${name}" ]] || usage; name=$1; shift ;;
  esac
done
[[ -n "${name}" && -n "${secret}" ]] || usage
[[ -n "${apply_namespace}" || -n "${output}" ]] || die "one of --apply and --output is required"
[[ -z "${apply_namespace}" || -z "${output}" ]] || die "--apply and --output exclude each other"
for file in "${server_cert}" "${server_key}" "${client_cert}" "${client_key}"; do
  [[ -n "${file}" ]] || die "--server-cert, --server-key, --client-cert and --client-key are required"
  [[ -r "${file}" ]] || die "cannot read ${file}"
done
for command in kubectl helm jq openssl; do
  command -v "${command}" >/dev/null || die "${command} not found"
done

source_args=(--namespace "${namespace}")
[[ -z "${context}" ]] || source_args+=(--context "${context}")
target_args=(--namespace "${apply_namespace:-default}")
apply_context=${apply_context:-${context}}
[[ -z "${apply_context}" ]] || target_args+=(--context "${apply_context}")
helm_args=(--namespace "${namespace}")
[[ -z "${context}" ]] || helm_args+=(--kube-context "${context}")

umask 077
work=$(mktemp -d)
trap 'rm -rf "${work}"' EXIT

values=$(helm get values "${release}" "${helm_args[@]}" --all --output json) ||
  die "release ${release} not found in namespace ${namespace}"
setting() { jq -r "$1 // empty" <<<"${values}"; }
oauth_secret=$(setting .foreman.oauth.existingSecret)
[[ -n "${oauth_secret}" ]] || die "the foreman release does not set foreman.oauth.existingSecret"

# secret_key SECRET KEY FILE: copy one key of a Secret of the foreman release
# into FILE, never through stdout.
secret_key() {
  kubectl get secret "$1" "${source_args[@]}" --output json |
    jq -r --arg key "$2" '.data[$key] // empty' | base64 --decode >"$3"
  [[ -s "$3" ]] || die "Secret $1 in namespace ${namespace} has no key $2"
}
ca_secret=$(setting .certificates.ca.secretName)
secret_key "${ca_secret:-${release}-ca}" "$(setting .certificates.ca.key)" "${work}/ca.crt"
server_secret=$(setting .certificates.server.secretName)
secret_key "${server_secret:-${release}-server-tls}" ca.crt "${work}/server-ca.crt"
secret_key "${oauth_secret}" "$(setting .foreman.oauth.consumerKeySecretKey)" "${work}/oauth-consumer-key"
secret_key "${oauth_secret}" "$(setting .foreman.oauth.consumerSecretSecretKey)" "${work}/oauth-consumer-secret"
cp "${server_cert}" "${work}/tls.crt"
cp "${server_key}" "${work}/tls.key"
cp "${client_cert}" "${work}/client.crt"
cp "${client_key}" "${work}/client.key"

# Checks: each key belongs to its certificate, the server certificate names
# NAME and chains to the server CA that hosts receive, the client certificate
# has CN NAME and chains to the CA bundle the Foreman server verifies clients
# with.
public_key() { openssl x509 -noout -pubkey -in "$1"; }
for pair in tls client; do
  [[ "$(public_key "${work}/${pair}.crt")" == "$(openssl pkey -pubout -in "${work}/${pair}.key")" ]] ||
    die "${pair}.key does not belong to ${pair}.crt"
done
openssl x509 -noout -text -in "${work}/tls.crt" | grep -Eq "DNS:${name//./\\.}(,|$)" ||
  die "the server certificate has no DNS name ${name}"
openssl verify -CAfile "${work}/server-ca.crt" "${work}/tls.crt" >/dev/null ||
  die "the server CA of ${release} does not sign the server certificate"
client_cn=$(openssl x509 -noout -subject -in "${work}/client.crt" | sed -E 's/.*CN ?= ?([^,/]*).*/\1/')
[[ "${client_cn}" == "${name}" ]] || die "the client certificate CN is ${client_cn}, not ${name}"
openssl verify -CAfile "${work}/ca.crt" "${work}/client.crt" >/dev/null ||
  die "the CA bundle of ${release} does not sign the client certificate"

manifest=${work}/secret.yaml
kubectl create secret generic "${secret}" --dry-run=client --output yaml \
  --from-file="${work}/ca.crt" --from-file="${work}/server-ca.crt" \
  --from-file="${work}/tls.crt" --from-file="${work}/tls.key" \
  --from-file="${work}/client.crt" --from-file="${work}/client.key" \
  --from-file="${work}/oauth-consumer-key" --from-file="${work}/oauth-consumer-secret" >"${manifest}"
if [[ -n "${output}" ]]; then
  install -m 0600 "${manifest}" "${output}"
  echo "Wrote Secret ${secret} for ${name} to ${output}"
else
  # Server-side apply keeps the keys out of the last-applied-configuration
  # annotation, which kubectl describe would print.
  kubectl apply "${target_args[@]}" --server-side --field-manager=foreman-proxy-bundle \
    --force-conflicts --filename "${manifest}" >/dev/null
  echo "Applied Secret ${secret} for ${name} in namespace ${apply_namespace}"
fi
