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
  value: ""
- name: FOREMAN_PUMA_WORKERS
  value: {{ .Values.foreman.puma.workers | quote }}
- name: FOREMAN_PUMA_THREADS_MIN
  value: {{ .Values.foreman.puma.threadsMin | quote }}
- name: FOREMAN_PUMA_THREADS_MAX
  value: {{ .Values.foreman.puma.threadsMax | quote }}
- name: FOREMAN_DATABASE_POOL
  value: {{ .Values.foreman.puma.threadsMax | quote }}
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
