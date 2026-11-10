#!/usr/bin/env bash
# Back up a release of the foreman Helm chart to a local directory.
#
# Usage: tools/backup.sh [OPTIONS] DESTINATION
#
# Creates DESTINATION/foreman-backup-<UTC timestamp>/ with the foreman,
# candlepin and pulp database dumps (pg_dump custom format), the Pulp content
# (media directory of the Pulp volume), the Secrets the release reads, the
# user-supplied chart values and metadata.json.
#
# Options:
#   -n, --namespace NAME  release namespace (default foreman)
#   -r, --release NAME    Helm release name (default foreman)
#   --context NAME        kubeconfig context for kubectl and helm
#   --image IMAGE         helper image with pg_dump and tar; its PostgreSQL
#                         major version must match the server
#                         (default docker.io/library/postgres:16-alpine,
#                         pinned by digest)
#   --online              keep the release running; the copies of the three
#                         databases and the content are then not taken at the
#                         same instant
#
# Without --online the script scales every Deployment of the release to zero,
# takes the copies and scales them back. It refuses to start while Foreman or
# Pulp tasks are running. The backup contains secrets: keep it private.
set -euo pipefail

# shellcheck source=tools/backup-common.sh
. "$(dirname "${BASH_SOURCE[0]}")/backup-common.sh"

usage() {
  sed -n '4,24p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

online=false
destination=
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n | --namespace) namespace=$2; shift 2 ;;
    -r | --release) release=$2; shift 2 ;;
    --context) set_context "$2"; shift 2 ;;
    --image) helper_image=$2; shift 2 ;;
    --online) online=true; shift ;;
    -h | --help) usage ;;
    -*) usage ;;
    *) [[ -z "${destination}" ]] || usage; destination=$1; shift ;;
  esac
done
[[ -n "${destination}" ]] || usage
require_commands kubectl helm jq

values=$(hm get values "${release}" --all --output json) ||
  die "release ${release} not found in namespace ${namespace}"
pulp_enabled=$(jq -r '.pulp.enabled' <<<"${values}")
candlepin_enabled=$(jq -r '.candlepin.enabled' <<<"${values}")
databases=(foreman)
[[ "${candlepin_enabled}" != true ]] || databases+=(candlepin)
[[ "${pulp_enabled}" != true ]] || databases+=(pulp)
claim=
if [[ "${pulp_enabled}" == true ]]; then
  claim=$(jq -r '.pulp.storage.existingClaim // ""' <<<"${values}")
  if [[ -z "${claim}" ]]; then
    claim=$(hm get manifest "${release}" |
      sed -n '/^kind: PersistentVolumeClaim$/,/^---/s/^  name: //p' | head -n 1)
  fi
  [[ -n "${claim}" ]] || die "cannot find the Pulp content claim of ${release}"
fi

umask 077
mkdir -p "${destination}"
backup_dir="${destination}/foreman-backup-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir "${backup_dir}" "${backup_dir}/secrets"
log "Backing up release ${release} in namespace ${namespace} to ${backup_dir}"

hm get values "${release}" --output yaml \
  >"${backup_dir}/values.yaml"

# Every Secret that a workload of the release reads: env and envFrom
# references, volumes, projected volumes and image pull Secrets.
workloads=$(kc get deployments,jobs --selector "app.kubernetes.io/instance=${release}" --output json)
secrets=()
while IFS= read -r secret; do
  secrets+=("${secret}")
done < <(jq -r '
  [.items[].spec.template.spec | .. | objects |
    (.secretKeyRef?.name, .secretRef?.name, .secretName?, (.secret? | objects | .name),
     (.imagePullSecrets? // [] | .[].name))
   | strings] | unique | .[]' <<<"${workloads}")
[[ ${#secrets[@]} -gt 0 ]] || die "no Secrets found for the workloads of ${release}"
for secret in "${secrets[@]}"; do
  kc get secret "${secret}" --output json | jq '{
      apiVersion, kind, type, data,
      metadata: ({name: .metadata.name}
        + (if .metadata.labels then {labels: .metadata.labels} else {} end))
    }' >"${backup_dir}/secrets/${secret}.json"
done
log "Saved ${#secrets[@]} Secrets: ${secrets[*]}"

images=$(kc get pods --selector "app.kubernetes.io/instance=${release}" --output json |
  jq '[.items[].status.containerStatuses[]? | {image, imageID}] | unique')
chart=$(hm list --filter "^${release}\$" --output json |
  jq '.[0] | {chart, app_version, revision}')

helper_start "${values}" "${claim}" true
replicas=()
cleanup() {
  local status=$? entry
  helper_stop
  if [[ ${#replicas[@]} -gt 0 ]]; then
    log "Scaling the release back up"
    for entry in "${replicas[@]}"; do
      kc scale deployment "${entry%% *}" --replicas "${entry##* }" >/dev/null || status=1
    done
    for entry in "${replicas[@]}"; do
      kc rollout status deployment "${entry%% *}" --timeout=20m >/dev/null || status=1
    done
  fi
  if [[ ${status} -ne 0 ]]; then
    log "Backup failed; partial files are in ${backup_dir}"
  fi
  exit "${status}"
}
trap cleanup EXIT

# Same check as foremanctl: no backup while tasks change the data.
running=$(helper_db foreman psql --no-psqlrc --tuples-only --no-align --command \
  "SELECT count(*) FROM foreman_tasks_tasks WHERE state NOT IN ('stopped', 'paused', 'scheduled')" \
  2>/dev/null || echo 0)
[[ "${running}" == 0 ]] || die "${running} Foreman task(s) are running; wait for them and retry"
if [[ "${pulp_enabled}" == true ]]; then
  running=$(helper_db pulp psql --no-psqlrc --tuples-only --no-align --command \
    "SELECT count(*) FROM core_task WHERE state IN ('running', 'waiting')")
  [[ "${running}" == 0 ]] || die "${running} Pulp task(s) are running; wait for them and retry"
fi

if [[ "${online}" != true ]]; then
  while IFS= read -r entry; do
    replicas+=("${entry}")
  done < <(kc get deployments --selector "app.kubernetes.io/instance=${release}" --output json |
    jq -r '.items[] | "\(.metadata.name) \(.spec.replicas)"')
  log "Stopping ${#replicas[@]} Deployments of ${release}"
  for entry in "${replicas[@]}"; do
    kc scale deployment "${entry%% *}" --replicas 0 >/dev/null
  done
  deadline=$((SECONDS + 600))
  until [[ "$(kc get pods --selector "app.kubernetes.io/instance=${release}" --output json |
    jq '[.items[] | select(.status.phase == "Running" or .status.phase == "Pending")] | length')" == 0 ]]; do
    ((SECONDS < deadline)) || die "pods of ${release} did not stop"
    sleep 3
  done
fi

for database in "${databases[@]}"; do
  log "Dumping the ${database} database"
  helper_db "${database}" pg_dump --format=custom --file="/work/${database}.dump"
  # shellcheck disable=SC2016
  size=$(helper_sh 'wc -c <"$1"' "/work/${database}.dump" | tr -d ' ')
  # shellcheck disable=SC2016
  helper_sh 'cat "$1" && rm -f "$1"' "/work/${database}.dump" >"${backup_dir}/${database}.dump"
  [[ "$(wc -c <"${backup_dir}/${database}.dump" | tr -d ' ')" == "${size}" ]] ||
    die "the ${database} dump arrived incomplete"
done

if [[ -n "${claim}" ]]; then
  log "Archiving Pulp content from claim ${claim}"
  # media holds the artifacts and published files. The keys foremanctl keeps
  # next to it are Secret keys here.
  helper_sh 'tar -C /var/lib/pulp -czf - media' >"${backup_dir}/pulp-content.tar.gz"
  gzip --test "${backup_dir}/pulp-content.tar.gz" || die "the Pulp content archive is incomplete"
fi

jq --null-input \
  --arg created "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg namespace "${namespace}" \
  --arg release "${release}" \
  --arg mode "$([[ "${online}" == true ]] && echo online || echo offline)" \
  --arg claim "${claim}" \
  --argjson chart "${chart}" \
  --argjson images "${images}" \
  --args '{
    created: $created, namespace: $namespace, release: $release, mode: $mode,
    chart: $chart, images: $images,
    databases: ($ARGS.positional[0] | split(" ")),
    secrets: ($ARGS.positional[1] | split(" ") | map(select(length > 0))),
    pulpContent: ($claim != "")
  }' "${databases[*]}" "${secrets[*]}" >"${backup_dir}/metadata.json"

log "Backup complete: ${backup_dir}"
printf '%s\n' "${backup_dir}"
