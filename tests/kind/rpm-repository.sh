#!/usr/bin/env bash
# Copy a small yum repository into the namespace and serve it in the cluster
# at http://rpm-repository/ for the Katello sync test. The files are fetched
# on the machine that runs the tests, so the cluster needs no outbound access.
#
# Usage: rpm-repository.sh NAMESPACE [SOURCE_URL]
# Default SOURCE_URL: https://fixtures.pulpproject.org/rpm-unsigned/ (35 small
# packages, about 90 KB). Set KUBE_CONTEXT to choose a kubectl context.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  sed -n '6p' "$0" | sed 's/^# //' >&2
  exit 2
fi
namespace=$1
source_url=${2:-https://fixtures.pulpproject.org/rpm-unsigned/}
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
kubectl_args=(--namespace "${namespace}")
if [[ -n "${KUBE_CONTEXT:-}" ]]; then
  kubectl_args+=(--context "${KUBE_CONTEXT}")
fi

dir="$(mktemp -d)"
trap 'rm -rf "${dir}"' EXIT
# repomd.xml, the metadata it lists and the packages listed in primary, all
# into one flat directory: ConfigMap keys cannot contain slashes.
python3 - "${source_url}" "${dir}" <<'PY'
import gzip, re, sys, urllib.request
from pathlib import PurePosixPath

base, target = sys.argv[1].rstrip("/") + "/", sys.argv[2]

def fetch(path):
    with urllib.request.urlopen(base + path, timeout=60) as response:
        data = response.read()
    with open(f"{target}/{PurePosixPath(path).name}", "wb") as out:
        out.write(data)
    return data

repomd = fetch("repodata/repomd.xml").decode()
for href in re.findall(r'href="([^"]+)"', repomd):
    data = fetch(href)
    if "primary.xml" in href:
        for package in re.findall(r'<location href="([^"]+\.rpm)"', gzip.decompress(data).decode()):
            fetch(package)
PY
kubectl "${kubectl_args[@]}" create configmap rpm-repository --from-file="${dir}" \
  --dry-run=client --output=yaml | kubectl "${kubectl_args[@]}" apply --filename=-
kubectl "${kubectl_args[@]}" apply --filename "${here}/dependencies-rpm-repository.yaml"
kubectl "${kubectl_args[@]}" rollout restart deployment/rpm-repository
kubectl "${kubectl_args[@]}" rollout status deployment/rpm-repository --timeout=300s
