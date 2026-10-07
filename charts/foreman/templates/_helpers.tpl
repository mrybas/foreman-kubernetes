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

{{- define "foreman.configurationData" -}}
database.yml: |
  production:
    url: <%= ENV.fetch('DATABASE_URL') %>
    pool: <%= Integer(ENV.fetch('FOREMAN_DATABASE_POOL')) %>
settings.yaml: |
  ---
  :fqdn: {{ .Values.foreman.fqdn | quote }}
  :foreman_url: {{ .Values.foreman.externalUrl | quote }}
  :hosts:
    - {{ .Values.foreman.fqdn | quote }}
  :require_ssl: false
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
