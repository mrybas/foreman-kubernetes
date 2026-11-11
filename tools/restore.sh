#!/usr/bin/env bash
# Restore a backup made by tools/backup.sh into a namespace without the release.
#
# Usage: tools/restore.sh [OPTIONS] BACKUP_DIR [-- HELM_INSTALL_ARGS...]
#
# Before running it, create the namespace and give it empty databases and the
# Valkey or Redis server the backed-up Secrets point at (the same users and
# passwords as before, or edit the Secrets in BACKUP_DIR/secrets first).
#
# Steps: create the backed-up Secrets that do not exist yet in the namespace,
# restore the databases, create the Pulp content claim and unpack the content
# into it, then helm install with the backed-up values and HELM_INSTALL_ARGS
# (for example --set values that name the new namespace). The chart's
# migration Jobs then find the restored schemas.
#
# foreman-proxy releases: when name is a Service name in the old namespace
# (<service>.<namespace>.svc[.cluster.local]) and the target namespace
# differs, the release is installed as <service>.<new namespace>... and its
# registration Job renames the two Smart Proxy records in Foreman. The same
# rename happens when HELM_INSTALL_ARGS set another name. Then a bundle Secret
# with certificates for the new name must exist in the namespace beforehand.
#
# Options:
#   -n, --namespace NAME  target namespace (default: the one in the backup)
#   -r, --release NAME    Helm release name (default: the one in the backup)
#   --context NAME        kubeconfig context for kubectl and helm
#   --chart CHART         chart to install (default: charts/foreman or
#                         charts/foreman-proxy of this repository, as in the
#                         backup); use the version that made the backup
#   --image IMAGE         helper image with pg_restore and tar; its PostgreSQL
#                         major version must match the server
#                         (default docker.io/library/postgres:16-alpine,
#                         pinned by digest)
#   --force               restore over databases that already have tables
#                         and over existing Pulp content
#
# Secrets that already exist in the namespace are kept, so certificates that
# name the new namespace can be created before the restore.

# Single-quoted scripts below expand their variables in the helper pod.
# shellcheck disable=SC2016
set -euo pipefail

# shellcheck source=tools/backup-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/backup-common.sh"

usage() {
  sed -n '4,38p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
chart=
force=false
backup_dir=
namespace=
release=
helm_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n | --namespace) namespace=$2; shift 2 ;;
    -r | --release) release=$2; shift 2 ;;
    --context) set_context "$2"; shift 2 ;;
    --chart) chart=$2; shift 2 ;;
    --image) helper_image=$2; shift 2 ;;
    --force) force=true; shift ;;
    --) shift; helm_args=("$@"); break ;;
    -h | --help) usage ;;
    -*) usage ;;
    *) [[ -z "${backup_dir}" ]] || usage; backup_dir=$1; shift ;;
  esac
done
[[ -n "${backup_dir}" ]] || usage
require_commands kubectl helm jq

metadata="${backup_dir}/metadata.json"
[[ -f "${metadata}" ]] || die "${metadata} not found; is ${backup_dir} a backup directory?"
[[ -f "${backup_dir}/values.yaml" ]] || die "${backup_dir}/values.yaml not found"
[[ -n "${namespace}" ]] || namespace=$(jq -r .namespace "${metadata}")
[[ -n "${release}" ]] || release=$(jq -r .release "${metadata}")
chart_name=$(jq -r '.chartName // "foreman"' "${metadata}")
[[ -n "${chart}" ]] || chart="${repo_root}/charts/${chart_name}"
databases=()
while IFS= read -r database; do
  [[ -f "${backup_dir}/${database}.dump" ]] || die "${backup_dir}/${database}.dump not found"
  databases+=("${database}")
done < <(jq -r '.databases[]' "${metadata}")
secrets=()
while IFS= read -r secret; do
  [[ -f "${backup_dir}/secrets/${secret}.json" ]] || die "${backup_dir}/secrets/${secret}.json not found"
  secrets+=("${secret}")
done < <(jq -r '.secrets[]' "${metadata}")
pulp_content=$(jq -r .pulpContent "${metadata}")
[[ "${pulp_content}" != true || -f "${backup_dir}/pulp-content.tar.gz" ]] ||
  die "${backup_dir}/pulp-content.tar.gz not found"

kc get serviceaccount default >/dev/null || die "namespace ${namespace} not found; create it first"
if hm status "${release}" >/dev/null 2>&1; then
  die "release ${release} already exists in namespace ${namespace}; restore into a namespace without it"
fi
release_args=("${release}" "${chart}" --values "${backup_dir}/values.yaml")
log "Restoring ${backup_dir} as release ${release} in namespace ${namespace}"

# Values the chart will run with: chart defaults, backed-up values and the
# extra arguments.
computed_values() {
  hm install "${release_args[@]}" ${helm_args[@]+"${helm_args[@]}"} --dry-run=client --output json |
    jq -c '.chart.values * .config'
}
values=$(computed_values)

# A Smart Proxy with content is known to Foreman and to its hosts by its name.
if [[ "${chart_name}" == foreman-proxy ]]; then
  old_name=$(jq -r '.proxyName // ""' "${metadata}")
  old_namespace=$(jq -r .namespace "${metadata}")
  name=$(jq -r .name <<<"${values}")
  if [[ "${name}" == "${old_name}" && "${namespace}" != "${old_namespace}" ]]; then
    new_name=$(proxy_name_for_namespace "${old_name}" "${old_namespace}" "${namespace}")
    if [[ -n "${new_name}" ]]; then
      log "name ${old_name} is a Service name in namespace ${old_namespace}; installing as ${new_name}"
      helm_args+=(--set-string "name=${new_name}")
      name=${new_name}
    fi
  fi
  if [[ -n "${old_name}" && "${name}" != "${old_name}" ]]; then
    if [[ -z "$(jq -r '.registration.previousName // ""' <<<"${values}")" ]]; then
      helm_args+=(--set-string "registration.previousName=${old_name}")
    fi
    bundle=$(jq -r '.bundle.secretName // ""' <<<"${values}")
    [[ -n "${bundle}" ]] || bundle="${release}-bundle"
    kc get secret "${bundle}" >/dev/null 2>&1 ||
      die "the name changes from ${old_name} to ${name}: create the bundle Secret ${bundle} with certificates for ${name} first (tools/foreman-proxy-bundle.sh), and add ${name} to httpd.trustedProxyClientNames of the foreman release"
    log "The Smart Proxy records ${old_name} and ${old_name}-pulp will be renamed to ${name} and ${name}-pulp"
    values=$(computed_values)
  fi
fi

for secret in "${secrets[@]}"; do
  if kc get secret "${secret}" >/dev/null 2>&1; then
    log "Keeping existing Secret ${secret}"
  else
    kc create --filename "${backup_dir}/secrets/${secret}.json" >/dev/null
    log "Created Secret ${secret}"
  fi
done

claim=
if [[ "${pulp_content}" == true ]]; then
  claim=$(jq -r '.pulp.storage.existingClaim // ""' <<<"${values}")
  if [[ -z "${claim}" ]]; then
    # Create the claim the chart would create, marked as belonging to the
    # release so that helm install adopts it, and fill it before Pulp starts.
    manifest=$(hm template "${release_args[@]}" ${helm_args[@]+"${helm_args[@]}"} \
      --show-only templates/pulp-storage.yaml)
    claim=$(sed -n 's/^  name: //p' <<<"${manifest}" | head -n 1)
    if ! kc get persistentvolumeclaim "${claim}" >/dev/null 2>&1; then
      kubectl annotate --local --filename=- --output yaml \
        "meta.helm.sh/release-name=${release}" "meta.helm.sh/release-namespace=${namespace}" \
        <<<"${manifest}" | kc create --filename=- >/dev/null
      log "Created Pulp content claim ${claim}"
    fi
  fi
fi

helper_start "${values}" "${claim}" false
trap helper_stop EXIT

for database in "${databases[@]}"; do
  tables=$(helper_db "${database}" psql --no-psqlrc --tuples-only --no-align --command \
    "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema')")
  if [[ "${tables}" != 0 && "${force}" != true ]]; then
    die "the ${database} database has ${tables} tables; restore into empty databases or use --force"
  fi
done
for database in "${databases[@]}"; do
  log "Restoring the ${database} database"
  helper_upload "${backup_dir}/${database}.dump" "/work/${database}.dump"
  # Objects become owned by the chart's database user; ownership and grants
  # of the old server are not carried over.
  helper_db "${database}" pg_restore --no-owner --no-privileges --clean --if-exists \
    --single-transaction --exit-on-error "/work/${database}.dump"
  helper_sh 'rm -f "/work/$1.dump"' "${database}"
done

if [[ -n "${claim}" ]]; then
  log "Restoring Pulp content into claim ${claim}"
  if [[ "${force}" != true ]] &&
    [[ -n "$(helper_sh 'ls -A /var/lib/pulp/media 2>/dev/null || true')" ]]; then
    die "the Pulp claim ${claim} already has content; use --force to replace it"
  fi
  # The archive goes to the claim itself, which has room for its contents.
  helper_upload "${backup_dir}/pulp-content.tar.gz" /var/lib/pulp/restore-content.tar.gz
  helper_sh '
    rm -rf /var/lib/pulp/media
    tar -C /var/lib/pulp -xzf /var/lib/pulp/restore-content.tar.gz
    rm -f /var/lib/pulp/restore-content.tar.gz
  '
fi
helper_stop
trap - EXIT

log "Installing release ${release}"
hm install "${release_args[@]}" --wait --wait-for-jobs --timeout 30m ${helm_args[@]+"${helm_args[@]}"}
log "Restore complete"
