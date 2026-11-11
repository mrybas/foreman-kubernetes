# Shared by tools/backup.sh and tools/restore.sh. Not meant to be run.
# shellcheck shell=bash

# Image with pg_dump, pg_restore, psql and tar. Its PostgreSQL major version
# must match the database server.
helper_image=docker.io/library/postgres:16-alpine@sha256:aa6cb33cf4c19c765cefcf48d96176258800870a9a1e930ad126200f9d825139
# The helper Job removes itself after this many seconds even if the script
# that started it dies.
helper_deadline=21600
namespace=foreman
release=foreman
# foreman or foreman-proxy: the chart of the release.
chart_name=foreman
kube_args=()
helm_kube_args=()

log() {
  printf '>> %s\n' "$*" >&2
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_commands() {
  local command
  for command in "$@"; do
    command -v "${command}" >/dev/null || die "${command} is required"
  done
}

# set_context CONTEXT: use a kubeconfig context for kubectl and helm.
# Arrays are expanded as ${a[@]+"${a[@]}"} because bash 3.2 (macOS) treats an
# empty array as unset under set -u.
set_context() {
  kube_args+=(--context "$1")
  helm_kube_args+=(--kube-context "$1")
}

hm() {
  helm ${helm_kube_args[@]+"${helm_kube_args[@]}"} --namespace "${namespace}" "$@"
}

kc() {
  kubectl ${kube_args[@]+"${kube_args[@]}"} --namespace "${namespace}" "$@"
}

helper_name() {
  printf '%s-backup-helper' "${release}"
}

# release_chart_name: name of the chart of the release, without its version.
release_chart_name() {
  hm list --filter "^${release}\$" --output json |
    jq -r '.[0].chart // empty | sub("-v?[0-9]+\\.[0-9]+\\.[0-9]+.*$"; "")'
}

# proxy_name_for_namespace NAME OLD NEW: when NAME, the host name of a
# foreman-proxy release, is an in-cluster Service name in namespace OLD
# (<service>.OLD.svc or <service>.OLD.svc.cluster.local), print the same name
# in namespace NEW. Print nothing for other names.
proxy_name_for_namespace() {
  local name=$1 old=$2 new=$3 service suffix
  case "${name}" in
    *."${old}".svc) suffix=svc ;;
    *."${old}".svc.cluster.local) suffix=svc.cluster.local ;;
    *) return 0 ;;
  esac
  service=${name%."${old}.${suffix}"}
  [[ -n "${service}" && "${service}" != *.* ]] || return 0
  printf '%s.%s.%s\n' "${service}" "${new}" "${suffix}"
}

# Script run inside the helper pod: db.sh COMPONENT COMMAND [ARGS...] runs a
# PostgreSQL client against the database of COMPONENT (foreman, candlepin,
# pulp or container_gateway) with the credentials the chart gives that
# component. Credentials come
# from Secret references in the pod environment and never leave the pod.
# shellcheck disable=SC2016
helper_db_script='
set -eu
param() {
  case "$2" in
    *"?$1="*|*"&$1="*) value=${2#*[?&]"$1"=}; printf "%s" "${value%%&*}" ;;
  esac
}
component=$1
shift
case "${component}" in
  foreman)
    url=${FOREMAN_DATABASE_URL}
    mode=$(param sslmode "${url}")
    export PGSSLMODE="${mode:-${FOREMAN_PGSSLMODE}}"
    database=${url%%\?*}
    ;;
  candlepin)
    url=${CANDLEPIN_JDBC_URL#jdbc:}
    mode=$(param sslmode "${url}")
    export PGSSLMODE="${mode:-prefer}" PGUSER="${CANDLEPIN_PGUSER}" PGPASSWORD="${CANDLEPIN_PGPASSWORD}"
    database=${url%%\?*}
    ;;
  pulp)
    export PGHOST="${PULP_PGHOST}" PGPORT="${PULP_PGPORT}" PGUSER="${PULP_PGUSER}" \
      PGPASSWORD="${PULP_PGPASSWORD}" PGSSLMODE="${PULP_PGSSLMODE}"
    database=${PULP_PGDATABASE}
    ;;
  container_gateway)
    export PGHOST="${GATEWAY_PGHOST}" PGPORT="${GATEWAY_PGPORT}" PGUSER="${GATEWAY_PGUSER}" \
      PGPASSWORD="${GATEWAY_PGPASSWORD}" PGSSLMODE="${GATEWAY_PGSSLMODE}"
    database=${GATEWAY_PGDATABASE}
    ;;
  *)
    echo "unknown component ${component}" >&2
    exit 2
    ;;
esac
command=$1
shift
exec "${command}" --dbname="${database}" "$@"
'

# helper_db COMPONENT COMMAND [ARGS...]: see helper_db_script.
helper_db() {
  kc exec --stdin "job/$(helper_name)" -- sh -c "${helper_db_script}" db.sh "$@"
}

# helper_sh SCRIPT [ARGS...]: run a shell script in the helper pod.
helper_sh() {
  local script=$1
  shift
  kc exec --stdin "job/$(helper_name)" -- sh -c "${script}" sh "$@"
}

# helper_start VALUES_JSON [CLAIM] [READ_ONLY]: start the helper Job with the
# database credentials of the release described by the computed chart values
# of chart_name, and the Pulp content claim mounted at /var/lib/pulp when
# CLAIM is set.
helper_start() {
  local values=$1 claim=${2:-} read_only=${3:-false} name
  name=$(helper_name)
  kc delete job "${name}" --ignore-not-found --wait=true >/dev/null
  jq --null-input \
    --argjson values "${values}" \
    --arg name "${name}" \
    --arg release "${release}" \
    --arg chart "${chart_name}" \
    --arg image "${helper_image}" \
    --arg claim "${claim}" \
    --argjson readOnly "${read_only}" \
    --argjson deadline "${helper_deadline}" '
    def ref($secret; $key): {valueFrom: {secretKeyRef: {name: $secret, key: $key}}};
    $values as $v |
    ($chart == "foreman") as $foreman |
    {
      apiVersion: "batch/v1",
      kind: "Job",
      metadata: {
        name: $name,
        labels: {
          "app.kubernetes.io/name": "foreman-backup",
          "app.kubernetes.io/part-of": $release
        }
      },
      spec: {
        backoffLimit: 0,
        activeDeadlineSeconds: $deadline,
        ttlSecondsAfterFinished: 600,
        template: {
          metadata: {labels: {"app.kubernetes.io/name": "foreman-backup", "app.kubernetes.io/part-of": $release}},
          spec: {
            restartPolicy: "Never",
            automountServiceAccountToken: false,
            enableServiceLinks: false,
            imagePullSecrets: ($v.imagePullSecrets // []),
            securityContext: {
              runAsNonRoot: true, runAsUser: 700, runAsGroup: 700, fsGroup: 700,
              fsGroupChangePolicy: "OnRootMismatch", seccompProfile: {type: "RuntimeDefault"}
            },
            affinity: (if $claim == "" then null else {
              podAffinity: {preferredDuringSchedulingIgnoredDuringExecution: [{
                weight: 100,
                podAffinityTerm: {
                  topologyKey: "kubernetes.io/hostname",
                  labelSelector: {matchLabels: {
                    "app.kubernetes.io/instance": $release,
                    "app.kubernetes.io/component": "pulp-api"
                  }}
                }
              }]}
            } end),
            containers: [{
              name: "helper",
              image: $image,
              command: ["sleep", ($deadline | tostring)],
              workingDir: "/work",
              securityContext: {
                allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}, runAsNonRoot: true
              },
              env: ([
                {name: "HOME", value: "/work"}
              ] + (if $foreman then [
                ({name: "FOREMAN_DATABASE_URL"} + ref($v.foreman.existingSecret; $v.foreman.databaseUrlSecretKey)),
                {name: "FOREMAN_PGSSLMODE", value: ($v.foreman.database.sslMode | tostring)}
              ] else [] end) + (if $foreman and $v.candlepin.enabled then [
                ({name: "CANDLEPIN_JDBC_URL"} + ref($v.candlepin.existingSecret; $v.candlepin.databaseUrlSecretKey)),
                ({name: "CANDLEPIN_PGUSER"} + ref($v.candlepin.existingSecret; $v.candlepin.databaseUsernameSecretKey)),
                ({name: "CANDLEPIN_PGPASSWORD"} + ref($v.candlepin.existingSecret; $v.candlepin.databasePasswordSecretKey))
              ] else [] end) + (if ($foreman | not) or $v.pulp.enabled then [
                {name: "PULP_PGHOST", value: ($v.pulp.database.host | tostring)},
                {name: "PULP_PGPORT", value: ($v.pulp.database.port | tostring)},
                {name: "PULP_PGDATABASE", value: ($v.pulp.database.name | tostring)},
                {name: "PULP_PGUSER", value: ($v.pulp.database.user | tostring)},
                {name: "PULP_PGSSLMODE", value: ($v.pulp.database.sslMode | tostring)},
                ({name: "PULP_PGPASSWORD"} + ref($v.pulp.existingSecret; $v.pulp.databasePasswordSecretKey))
              ] else [] end) + (if ($foreman | not) and $v.proxy.containerGateway.enabled then
                ($v.proxy.containerGateway.database as $d | [
                  {name: "GATEWAY_PGHOST", value: ($d.host | tostring)},
                  {name: "GATEWAY_PGPORT", value: ($d.port | tostring)},
                  {name: "GATEWAY_PGDATABASE", value: ($d.name | tostring)},
                  {name: "GATEWAY_PGUSER", value: ($d.user | tostring)},
                  {name: "GATEWAY_PGSSLMODE", value: ($d.sslMode | tostring)},
                  ({name: "GATEWAY_PGPASSWORD"} + ref($d.existingSecret; $d.passwordSecretKey))
                ]) else [] end)),
              resources: {requests: {cpu: "50m", memory: "128Mi"}, limits: {memory: "1Gi"}},
              volumeMounts: ([{name: "work", mountPath: "/work"}] +
                (if $claim == "" then [] else [{name: "pulp", mountPath: "/var/lib/pulp", readOnly: $readOnly}] end))
            }],
            volumes: ([{name: "work", emptyDir: {}}] +
              (if $claim == "" then [] else [{name: "pulp", persistentVolumeClaim: {claimName: $claim}}] end))
          }
        }
      }
    } | del(.spec.template.spec.affinity | nulls)' |
    kc apply --filename=- >/dev/null
  log "Waiting for the helper pod ${name}"
  local deadline=$((SECONDS + 600))
  until kc get pods --selector "job-name=${name}" --output name 2>/dev/null | grep -q .; do
    ((SECONDS < deadline)) || die "helper pod for ${name} was not created"
    sleep 2
  done
  kc wait pod --selector "job-name=${name}" --for=condition=Ready \
    --timeout="$((deadline - SECONDS))s" >/dev/null ||
    die "helper pod ${name} is not ready; see: kubectl --namespace ${namespace} describe pods --selector job-name=${name}"
}

# helper_upload FILE PATH: copy a local file into the helper pod and check its
# size. kubectl cp is used because data piped to kubectl exec can arrive
# truncated.
helper_upload() {
  local file=$1 path=$2 pod size
  pod=$(kc get pods --selector "job-name=$(helper_name)" --output jsonpath='{.items[0].metadata.name}')
  kc cp --container helper "${file}" "${pod}:${path}" >/dev/null
  size=$(wc -c <"${file}" | tr -d ' ')
  # shellcheck disable=SC2016
  [[ "$(helper_sh 'wc -c <"$1"' "${path}" | tr -d ' ')" == "${size}" ]] ||
    die "copying ${file} into the helper pod was incomplete"
}

helper_stop() {
  kc delete job "$(helper_name)" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  kc wait pod --selector "job-name=$(helper_name)" --for=delete --timeout=120s >/dev/null 2>&1 || true
}
