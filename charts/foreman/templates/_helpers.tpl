{{- define "foreman.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "foreman.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "foreman.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Name of a component resource: <fullname>-<component>, with the fullname
shortened so that the result fits in 63 characters and names of different
components stay distinct. Takes (list $context "component").
*/}}
{{- define "foreman.componentName" -}}
{{- $context := index . 0 }}
{{- $component := index . 1 }}
{{- $fullname := include "foreman.fullname" $context | trunc (int (sub 62 (len $component))) | trimSuffix "-" }}
{{- printf "%s-%s" $fullname $component }}
{{- end }}

{{- define "foreman.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "foreman.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "foreman.selectorLabels" -}}
app.kubernetes.io/name: {{ include "foreman.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "foreman.image" -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag }}
{{- end }}

{{- define "foreman.configName" -}}
{{- printf "%s-config" (include "foreman.fullname" .) }}
{{- end }}

{{- define "foreman.migrationConfigName" -}}
{{- printf "%s-migration-config" (include "foreman.fullname" .) }}
{{- end }}

{{/*
ConfigMap data for Foreman. Takes a dict: context is the root context, web is
true for the web pods, which also get the TLS client settings.
*/}}
{{- define "foreman.configurationData" -}}
{{- $context := .context -}}
database.yml: |
  production:
    url: <%= ENV.fetch('DATABASE_URL') %>
    pool: <%= Integer(ENV.fetch('FOREMAN_DATABASE_POOL')) %>
settings.yaml: |
  ---
  :fqdn: {{ $context.Values.foreman.fqdn | quote }}
  :foreman_url: {{ $context.Values.foreman.externalUrl | quote }}
  :hosts:
    - {{ $context.Values.foreman.fqdn | quote }}
  :require_ssl: false
  {{- if and .web $context.Values.certificates.enabled }}
  :ssl_ca_file: {{ include "foreman.certificatesPath" $context }}/ca.crt
  :ssl_certificate: {{ include "foreman.certificatesPath" $context }}/client.crt
  :ssl_priv_key: {{ include "foreman.certificatesPath" $context }}/client.key
  {{- end }}
{{- end }}

{{- define "foreman.certificatesPath" -}}
/etc/foreman/certs
{{- end }}

{{- define "foreman.caSecretName" -}}
{{- .Values.certificates.ca.secretName | default (printf "%s-ca" .Release.Name) }}
{{- end }}

{{- define "foreman.clientTlsSecretName" -}}
{{- .Values.certificates.client.secretName | default (printf "%s-client-tls" .Release.Name) }}
{{- end }}

{{/*
Projected volume with the CA bundle and the client certificate and key.
Files: ca.crt, client.crt, client.key.
*/}}
{{- define "foreman.certificatesVolume" -}}
- name: certificates
  projected:
    defaultMode: 0440
    sources:
      - secret:
          name: {{ include "foreman.caSecretName" . }}
          items:
            - key: {{ .Values.certificates.ca.key }}
              path: ca.crt
      - secret:
          name: {{ include "foreman.clientTlsSecretName" . }}
          items:
            - key: tls.crt
              path: client.crt
            - key: tls.key
              path: client.key
{{- end }}

{{- define "foreman.certificatesVolumeMount" -}}
- name: certificates
  mountPath: {{ include "foreman.certificatesPath" . }}
  readOnly: true
{{- end }}

{{- define "foreman.env" -}}
- name: RAILS_ENV
  value: production
- name: RAILS_LOG_TO_STDOUT
  value: "true"
- name: FOREMAN_BIND
  value: 0.0.0.0
- name: RAILS_SERVE_STATIC_FILES
  value: "true"
- name: FOREMAN_ENABLED_PLUGINS
  value: {{ include "foreman.enabledPlugins" . | quote }}
- name: FOREMAN_PUMA_WORKERS
  value: {{ .Values.foreman.puma.workers | quote }}
- name: FOREMAN_PUMA_THREADS_MIN
  value: {{ .Values.foreman.puma.threadsMin | quote }}
- name: FOREMAN_PUMA_THREADS_MAX
  value: {{ .Values.foreman.puma.threadsMax | quote }}
- name: FOREMAN_DATABASE_POOL
  value: {{ .databasePool | default .Values.foreman.puma.threadsMax | quote }}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.foreman.existingSecret }}
      key: {{ .Values.foreman.databaseUrlSecretKey }}
- name: PGSSLMODE
  value: {{ .Values.foreman.database.sslMode | quote }}
- name: ENCRYPTION_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.foreman.existingSecret }}
      key: {{ .Values.foreman.encryptionKeySecretKey }}
- name: SECRET_KEY_BASE
  valueFrom:
    secretKeyRef:
      name: {{ .Values.foreman.existingSecret }}
      key: {{ .Values.foreman.secretKeyBaseSecretKey }}
{{- if .Values.dynflow.enabled }}
{{ include "dynflow.cacheEnv" . }}
{{- end }}
{{- end }}

{{/*
Space separated plugin list for FOREMAN_ENABLED_PLUGINS. The image loads a
plugin only when its name is in this list.
*/}}
{{- define "foreman.enabledPlugins" -}}
{{- $plugins := list -}}
{{- if .Values.dynflow.enabled }}
{{- $plugins = append $plugins "foreman-tasks" -}}
{{- end }}
{{- join " " $plugins -}}
{{- end }}


{{/*
Valkey URL from the existing Secret and the Rails cache settings that use it.
Every Foreman pod shares the cache, so Dynflow and web see the same values.
*/}}
{{- define "dynflow.cacheEnv" -}}
- name: FOREMAN_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.foreman.existingSecret }}
      key: {{ .Values.foreman.redisUrlSecretKey }}
- name: FOREMAN_RAILS_CACHE_STORE_TYPE
  value: redis
- name: FOREMAN_RAILS_CACHE_STORE_URLS
  value: $(FOREMAN_REDIS_URL)/4
- name: FOREMAN_RAILS_CACHE_STORE_OPTIONS_COMPRESS
  value: "true"
- name: FOREMAN_RAILS_CACHE_STORE_OPTIONS_NAMESPACE
  value: foreman
{{- end }}

{{- define "foreman.volumeMounts" -}}
- name: configuration
  mountPath: /usr/share/foreman/config/database.yml
  subPath: database.yml
  readOnly: true
- name: configuration
  mountPath: /etc/foreman/settings.yaml
  subPath: settings.yaml
  readOnly: true
- name: temporary
  mountPath: /usr/share/foreman/tmp
{{- if .Values.certificates.enabled }}
{{ include "foreman.certificatesVolumeMount" . }}
{{- end }}
{{- end }}

{{- define "foreman.migrationVolumeMounts" -}}
- name: configuration
  mountPath: /usr/share/foreman/config/database.yml
  subPath: database.yml
  readOnly: true
- name: configuration
  mountPath: /etc/foreman/settings.yaml
  subPath: settings.yaml
  readOnly: true
- name: temporary
  mountPath: /usr/share/foreman/tmp
{{- end }}
