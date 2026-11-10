{{/*
Workloads that may run more than one replica, as dicts: name (Deployment and
PodDisruptionBudget name), component (app.kubernetes.io/component) and
replicas. Pulp is listed with S3 storage or with pulp.storage.colocate=false:
Pulp pods colocated on filesystem storage must all run on the node of the
content volume, so they are neither spread nor given a budget. The Dynflow orchestrator, Candlepin and the Smart
Proxy always run one replica and are not listed.
*/}}
{{- define "foreman.replicatedWorkloads" -}}
{{- $workloads := list (dict "name" (include "foreman.fullname" .) "component" "web" "replicas" .Values.replicaCount) }}
{{- if .Values.httpd.enabled }}
{{- $workloads = append $workloads (dict "name" (include "httpd.fullname" .) "component" "httpd" "replicas" .Values.httpd.replicas) }}
{{- end }}
{{- if .Values.dynflow.enabled }}
{{- $workloads = append $workloads (dict "name" (include "foreman.componentName" (list . "dynflow-worker")) "component" "dynflow-worker" "replicas" .Values.dynflow.worker.replicas) }}
{{- $workloads = append $workloads (dict "name" (include "foreman.componentName" (list . "dynflow-worker-hosts-queue")) "component" "dynflow-worker-hosts-queue" "replicas" .Values.dynflow.hostsQueue.replicas) }}
{{- end }}
{{- if and .Values.pulp.enabled (include "pulp.colocated" . | eq "false") }}
{{- range list "api" "content" "worker" }}
{{- $workloads = append $workloads (dict "name" (printf "%s-%s" (include "pulp.fullname" $) .) "component" (printf "pulp-%s" .) "replicas" (index $.Values.pulp . "replicas")) }}
{{- end }}
{{- end }}
{{- toJson (dict "workloads" $workloads) }}
{{- end }}

{{/*
topologySpreadConstraints for a pod of a replicated workload, empty for one
replica. Takes (dict "context" $ "component" "<component>" "replicas" N).
pod-template-hash keeps the old and new pods of a rolling update apart.
*/}}
{{- define "foreman.topologySpread" -}}
{{- $spread := .context.Values.topologySpread }}
{{- if and $spread.enabled (gt (int .replicas) 1) -}}
topologySpreadConstraints:
  - maxSkew: {{ $spread.maxSkew }}
    topologyKey: {{ $spread.topologyKey }}
    whenUnsatisfiable: {{ $spread.whenUnsatisfiable }}
    labelSelector:
      matchLabels:
        {{- include "foreman.selectorLabels" .context | nindent 8 }}
        app.kubernetes.io/component: {{ .component }}
    matchLabelKeys:
      - pod-template-hash
{{- end }}
{{- end }}

{{/*
"true" when the Pulp pods must run on the node of the content volume:
filesystem storage with pulp.storage.colocate. S3 storage has no volume to
follow, so its pods can be spread and given a budget.
*/}}
{{- define "pulp.colocated" -}}
{{- and (eq (include "pulp.storageType" .) "filesystem") .Values.pulp.storage.colocate | ternary "true" "false" }}
{{- end }}
