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

{{/*
Image reference from an image value (repository, tag, digest). A digest wins
over the tag and must be sha256:<64 hex digits>.
*/}}
{{- define "foreman.imageReference" -}}
{{- if .digest }}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" .digest) }}
{{- fail (printf "image digest of %s must be sha256:<64 hex digits>, got %q" .repository .digest) }}
{{- end }}
{{- printf "%s@%s" .repository .digest }}
{{- else }}
{{- printf "%s:%s" .repository .tag }}
{{- end }}
{{- end }}

{{- define "foreman.image" -}}
{{- include "foreman.imageReference" .Values.image }}
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
  {{- if $context.Values.proxy.enabled }}
  {{- $proxyForemanHost := (urlParse (include "proxy.foremanUrl" $context)).hostname }}
  {{- if ne $proxyForemanHost $context.Values.foreman.fqdn }}
    - {{ $proxyForemanHost | quote }}
  {{- end }}
  {{- end }}
  :require_ssl: {{ and .web $context.Values.httpd.enabled }}
  {{- if and .web (or $context.Values.httpd.enabled $context.Values.foreman.trustedProxies) }}
  :trusted_proxies:
    - 127.0.0.0/8
    - "::1"
    {{- range $context.Values.foreman.trustedProxies }}
    - {{ . | quote }}
    {{- end }}
  {{- end }}
  {{- if and .web $context.Values.httpd.enabled }}
  # Client certificate headers set by the httpd front door.
  :ssl_client_dn_env: HTTP_SSL_CLIENT_S_DN
  :ssl_client_verify_env: HTTP_SSL_CLIENT_VERIFY
  :ssl_client_cert_env: HTTP_SSL_CLIENT_CERT
  {{- end }}
  {{- if and .web $context.Values.certificates.enabled }}
  :ssl_ca_file: {{ include "foreman.certificatesPath" $context }}/ca.crt
  :ssl_certificate: {{ include "foreman.certificatesPath" $context }}/client.crt
  :ssl_priv_key: {{ include "foreman.certificatesPath" $context }}/client.key
  {{- end }}
{{- if $context.Values.katello.enabled }}
{{ include "katello.configuration" $context }}
{{- end }}
{{- end }}

{{- define "foreman.certificatesPath" -}}
/etc/foreman/certs
{{- end }}

{{- define "foreman.caSecretName" -}}
{{- .Values.certificates.ca.secretName | default (printf "%s-ca" .Release.Name) }}
{{- end }}

{{- define "foreman.serverTlsSecretName" -}}
{{- .Values.certificates.server.secretName | default (printf "%s-server-tls" .Release.Name) }}
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
{{- if or .Values.katello.enabled .Values.proxy.enabled }}
{{- /* Katello requires foreman_remote_execution and loads it either way. */}}
{{- $plugins = append $plugins "foreman_remote_execution" -}}
{{- end }}
{{- if and .Values.proxy.enabled .Values.proxy.ansible.enabled }}
{{- $plugins = append $plugins "foreman_ansible" -}}
{{- end }}
{{- if .Values.katello.enabled }}
{{- $plugins = append $plugins "katello" -}}
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
{{- if .Values.katello.enabled }}
{{ include "katello.volumeMount" . }}
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
{{- if .Values.katello.enabled }}
{{ include "katello.volumeMount" . }}
{{- end }}
{{- end }}

{{- define "pulp.fullname" -}}
{{- printf "%s-pulp" (include "foreman.fullname" .) | trunc 52 | trimSuffix "-" }}
{{- end }}

{{- define "pulp.image" -}}
{{- include "foreman.imageReference" .Values.pulp.image }}
{{- end }}

{{/* Labels for a Pulp object. Call with (dict "context" $ "component" "pulp-api"). */}}
{{- define "pulp.labels" -}}
{{ include "foreman.labels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "pulp.selectorLabels" -}}
{{ include "foreman.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "pulp.claimName" -}}
{{- .Values.pulp.storage.existingClaim | default (printf "%s-storage" (include "pulp.fullname" .)) }}
{{- end }}

{{/*
/etc/pulp/settings.py. Ported from the environment foremanctl sets in
src/roles/pulp/defaults/main.yaml. Values are rendered as JSON literals, which
Python reads as the same strings and lists. Secrets are added by pulp.env.
*/}}
{{- define "pulp.settings" -}}
{{- $s := .Values.pulp.settings -}}
{{- $origin := $s.contentOrigin | default (printf "https://%s" .Values.foreman.fqdn) -}}
settings.py: |
  # Rendered by the foreman Helm chart.
  DATABASES__default__ENGINE = "django.db.backends.postgresql"
  DATABASES__default__HOST = {{ .Values.pulp.database.host | toJson }}
  DATABASES__default__PORT = {{ .Values.pulp.database.port | int }}
  DATABASES__default__NAME = {{ .Values.pulp.database.name | toJson }}
  DATABASES__default__USER = {{ .Values.pulp.database.user | toJson }}
  DATABASES__default__OPTIONS__sslmode = {{ .Values.pulp.database.sslMode | toJson }}
  ENABLED_PLUGINS = {{ $s.enabledPlugins | toJson }}
  CONTENT_ORIGIN = {{ $origin | toJson }}
  SMART_PROXY_PULP_URL = {{ $s.smartProxyPulpUrl | default (printf "https://%s" .Values.foreman.fqdn) | toJson }}
  SMART_PROXY_MIRROR = False
  ANSIBLE_API_HOSTNAME = {{ $origin | toJson }}
  ANSIBLE_CONTENT_HOSTNAME = {{ printf "%s/pulp/content" $origin | toJson }}
  ANSIBLE_PERMISSION_CLASSES = []
  ANALYTICS = False
  CACHE_ENABLED = True
  TOKEN_AUTH_DISABLED = True
  FLATPAK_INDEX = True
  AUTHENTICATION_BACKENDS = ["pulpcore.app.authentication.PulpNoCreateRemoteUserBackend"]
  REMOTE_USER_ENVIRON_NAME = {{ $s.remoteUserEnvironName | toJson }}
  REST_FRAMEWORK__DEFAULT_AUTHENTICATION_CLASSES = [
      "rest_framework.authentication.SessionAuthentication",
      "pulpcore.app.authentication.PulpRemoteUserAuthentication",
  ]
  ALLOWED_CONTENT_CHECKSUMS = ["sha1", "sha224", "sha256", "sha384", "sha512"]
  ALLOWED_IMPORT_PATHS = ["/var/lib/pulp/sync_imports", "/var/lib/pulp/imports"]
  ALLOWED_EXPORT_PATHS = ["/var/lib/pulp/exports"]
  LOGGING__handlers__console__level = {{ $s.logLevel | upper | toJson }}
  LOGGING__loggers____level = {{ $s.logLevel | upper | toJson }}
  {{- with $s.extra }}
  {{- . | nindent 2 }}
  {{- end }}
{{- end }}

{{- define "pulp.env" -}}
- name: PULP_SETTINGS
  value: /etc/pulp/settings.py
- name: PULP_LOG_LEVEL
  value: {{ .Values.pulp.settings.logLevel | quote }}
- name: PULP_DATABASES__default__PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.pulp.existingSecret }}
      key: {{ .Values.pulp.databasePasswordSecretKey }}
- name: PULP_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.pulp.existingSecret }}
      key: {{ .Values.pulp.secretKeySecretKey }}
- name: PULP_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.pulp.existingSecret }}
      key: {{ .Values.pulp.redisUrlSecretKey }}
{{- end }}

{{- define "pulp.podSecurityContext" -}}
fsGroup: 700
fsGroupChangePolicy: OnRootMismatch
runAsGroup: 700
runAsNonRoot: true
runAsUser: 700
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "pulp.securityContext" -}}
allowPrivilegeEscalation: false
capabilities:
  drop:
    - ALL
runAsNonRoot: true
runAsGroup: 700
runAsUser: 700
{{- end }}

{{/*
/etc/pulp with settings.py from a ConfigMap and the database field key from
the Secret. Call with (dict "context" $ "configMap" "<name>").
*/}}
{{- define "pulp.configVolume" -}}
- name: pulp-config
  projected:
    defaultMode: 0440
    sources:
      - configMap:
          name: {{ .configMap }}
          items:
            - key: settings.py
              path: settings.py
      - secret:
          name: {{ .context.Values.pulp.existingSecret }}
          items:
            - key: {{ .context.Values.pulp.symmetricKeySecretKey }}
              path: certs/database_fields.symmetric.key
{{- end }}

{{- define "pulp.configVolumeMount" -}}
- name: pulp-config
  mountPath: /etc/pulp
  readOnly: true
{{- end }}

{{/*
Directories Pulp expects under /var/lib/pulp. Django checks that tmp exists
before migrate or any process starts.
*/}}
{{- define "pulp.storageDirectories" -}}
/var/lib/pulp/tmp /var/lib/pulp/assets /var/lib/pulp/media /var/lib/pulp/sync_imports /var/lib/pulp/imports /var/lib/pulp/exports
{{- end }}

{{/* Runtime pod spec parts shared by api, content and worker. */}}
{{- define "pulp.podSpec" -}}
automountServiceAccountToken: false
securityContext:
  {{- include "pulp.podSecurityContext" . | nindent 2 }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- if .Values.pulp.storage.colocate }}
# The content volume may be ReadWriteOnce, so every Pulp pod runs on the node
# of a Pulp API pod. The first API pod matches its own term and goes anywhere.
affinity:
  podAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      - topologyKey: kubernetes.io/hostname
        labelSelector:
          matchLabels:
            {{- include "pulp.selectorLabels" (dict "context" . "component" "pulp-api") | nindent 12 }}
{{- end }}
initContainers:
  - name: storage
    image: {{ include "pulp.image" . }}
    imagePullPolicy: {{ .Values.pulp.image.pullPolicy }}
    command:
      - /bin/bash
      - -ec
      - mkdir -p {{ include "pulp.storageDirectories" . }}
    securityContext:
      {{- include "pulp.securityContext" . | nindent 6 }}
    volumeMounts:
      - name: storage
        mountPath: /var/lib/pulp
    resources:
      requests:
        cpu: 10m
        memory: 32Mi
      limits:
        memory: 64Mi
{{- end }}

{{- define "pulp.podVolumes" -}}
{{ include "pulp.configVolume" (dict "context" . "configMap" (printf "%s-config" (include "pulp.fullname" .))) }}
- name: storage
  persistentVolumeClaim:
    claimName: {{ include "pulp.claimName" . }}
{{- end }}

{{/* Candlepin. */}}
{{- define "candlepin.fullname" -}}
{{- include "foreman.componentName" (list . "candlepin") }}
{{- end }}

{{- define "candlepin.labels" -}}
{{ include "foreman.labels" . }}
app.kubernetes.io/component: candlepin
{{- end }}

{{- define "candlepin.selectorLabels" -}}
{{ include "foreman.selectorLabels" . }}
app.kubernetes.io/component: candlepin
{{- end }}

{{- define "candlepin.image" -}}
{{- include "foreman.imageReference" .Values.candlepin.image }}
{{- end }}

{{- define "candlepin.caSecretName" -}}
{{- .Values.certificates.candlepin.ca.secretName | default (printf "%s-candlepin-ca" .Release.Name) }}
{{- end }}

{{- define "candlepin.serverTlsSecretName" -}}
{{- .Values.certificates.candlepin.server.secretName | default (printf "%s-candlepin-tls" .Release.Name) }}
{{- end }}

{{- define "candlepin.certificatesPath" -}}
/etc/candlepin/tls
{{- end }}

{{/*
Pod and container security context. The image user is the name tomcat
(53:53), so the numeric IDs are needed for runAsNonRoot.
*/}}
{{- define "candlepin.podSecurityContext" -}}
fsGroup: 53
fsGroupChangePolicy: OnRootMismatch
runAsGroup: 53
runAsNonRoot: true
runAsUser: 53
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "candlepin.containerSecurityContext" -}}
allowPrivilegeEscalation: false
capabilities:
  drop:
    - ALL
runAsNonRoot: true
runAsGroup: 53
runAsUser: 53
{{- end }}

{{/*
Database credentials as environment variables. candlepin.conf refers to them
as ${NAME}; Candlepin resolves these from the environment at startup.
*/}}
{{- define "candlepin.databaseEnv" -}}
- name: JDBC_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.candlepin.existingSecret }}
      key: {{ .Values.candlepin.databaseUrlSecretKey }}
- name: JDBC_USERNAME
  valueFrom:
    secretKeyRef:
      name: {{ .Values.candlepin.existingSecret }}
      key: {{ .Values.candlepin.databaseUsernameSecretKey }}
- name: JDBC_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.candlepin.existingSecret }}
      key: {{ .Values.candlepin.databasePasswordSecretKey }}
{{- end }}

{{/* httpd front door. */}}
{{- define "httpd.fullname" -}}
{{- include "foreman.componentName" (list . "httpd") }}
{{- end }}

{{- define "httpd.labels" -}}
{{ include "foreman.labels" . }}
app.kubernetes.io/component: httpd
{{- end }}

{{- define "httpd.selectorLabels" -}}
{{ include "foreman.selectorLabels" . }}
app.kubernetes.io/component: httpd
{{- end }}

{{- define "httpd.image" -}}
{{- include "foreman.imageReference" .Values.httpd.image }}
{{- end }}

{{/* Backends, as in-cluster Service URLs. */}}
{{- define "httpd.foremanBackend" -}}
{{- printf "http://%s:%v" (include "foreman.fullname" .) .Values.service.port }}
{{- end }}

{{- define "httpd.pulpApiBackend" -}}
{{- printf "http://%s-api:24817" (include "pulp.fullname" .) }}
{{- end }}

{{- define "httpd.pulpContentBackend" -}}
{{- printf "http://%s-content:24816" (include "pulp.fullname" .) }}
{{- end }}

{{- define "httpd.pulpEnabled" -}}
{{- if (.Values.pulp).enabled }}true{{ end }}
{{- end }}

{{- define "httpd.candlepinEnabled" -}}
{{- if (.Values.candlepin).enabled }}true{{ end }}
{{- end }}

{{- define "httpd.serverNames" -}}
ServerName {{ .Values.foreman.fqdn }}
{{- $service := include "httpd.fullname" . }}
ServerAlias {{ $service }} {{ $service }}.{{ .Release.Namespace }}.svc {{ $service }}.{{ .Release.Namespace }}.svc.cluster.local
{{- range .Values.httpd.serverAliases }}
ServerAlias {{ . }}
{{- end }}
{{- end }}

{{/*
RequestHeader unset for every spelling of a header name. Rack (Foreman,
Katello) and Django (Pulp) map "-" and "_" to the same variable, so
X-RHSM_SSL-CLIENT-CERT arrives as HTTP_X_RHSM_SSL_CLIENT_CERT, while
RequestHeader unset removes one exact name (in any case). Takes the name with
hyphens and emits one line per combination of "-" and "_".
*/}}
{{- define "httpd.unsetEverySpelling" -}}
{{- $parts := splitList "-" . }}
{{- $names := list (first $parts) }}
{{- range rest $parts }}
{{- $part := . }}
{{- $next := list }}
{{- range $names }}
{{- $next = append $next (printf "%s-%s" . $part) }}
{{- $next = append $next (printf "%s_%s" . $part) }}
{{- end }}
{{- $names = $next }}
{{- end }}
{{- range $names }}
RequestHeader unset {{ . }}
{{- end }}
{{- end }}

{{/*
Request headers that Foreman, Katello or Pulp trust when httpd sends them:
external login (REMOTE-USER, as foremanctl removes it), the client certificate
(Katello reads X-RHSM-SSL-CLIENT-CERT before SSL-CLIENT-CERT and does not
check its signature), and the forwarding chain (client address, host and
scheme; Rack reads X-Forwarded-Ssl and Forwarded before X-Forwarded-Proto).
httpd removes every spelling a client sends, before it sets its own values.
mod_proxy then adds X-Forwarded-For, X-Forwarded-Host and X-Forwarded-Server
for the real peer.
*/}}
{{- define "httpd.unsetClientHeaders" -}}
{{- range list
  "REMOTE-USER" "REMOTE-USER-EMAIL" "REMOTE-USER-FIRSTNAME" "REMOTE-USER-LASTNAME" "REMOTE-USER-GROUPS"
  "SSL-CLIENT-S-DN" "SSL-CLIENT-CERT" "SSL-CLIENT-VERIFY" "X-RHSM-SSL-CLIENT-CERT" "X-CLIENT-CERT"
  "X-FORWARDED-FOR" "X-FORWARDED-HOST" "X-FORWARDED-PORT" "X-FORWARDED-PROTO" "X-FORWARDED-SCHEME"
  "X-FORWARDED-SSL" "X-FORWARDED-SERVER" "FORWARDED" "CLIENT-IP" }}
{{- include "httpd.unsetEverySpelling" . }}
{{- end }}
{{- end }}

{{/* REMOTE-USER admin for Pulp API calls with a trusted client certificate. */}}
{{- define "httpd.pulpRemoteUser" -}}
RequestHeader unset REMOTE_USER
RequestHeader unset REMOTE-USER
{{- range prepend .Values.httpd.pulpTrustedClientNames .Values.foreman.fqdn }}
RequestHeader set REMOTE-USER "admin" "expr=%{SSL_CLIENT_VERIFY} == 'SUCCESS' && %{SSL_CLIENT_S_DN_CN} == '{{ . }}'"
{{- end }}
{{- end }}

{{/* /pulp/content and /pulp/isos, the same on both ports. */}}
{{- define "httpd.pulpContent" -}}
{{- $backend := include "httpd.pulpContentBackend" . }}
{{- range list "/pulp/content" "/pulp/isos" }}
<Location "{{ . }}">
  RequestHeader set X-CLIENT-CERT "%{SSL_CLIENT_CERT}s" env=SSL_CLIENT_CERT
  RequestHeader set X-FORWARDED-PROTO expr=%{REQUEST_SCHEME}
  ProxyPass {{ $backend }}/pulp/content disablereuse=on timeout=600
  ProxyPassReverse {{ $backend }}/pulp/content
</Location>
{{- end }}
{{- end }}

{{/* /pub with the server CA, and the registration script that Katello serves. */}}
{{- define "httpd.pub" -}}
{{- if or (include "httpd.pulpEnabled" .) (include "httpd.candlepinEnabled" .) }}
RewriteEngine On
RewriteRule ^/pub/katello-rhsm-consumer$ {{ include "httpd.foremanBackend" . }}/unattended/public/katello-rhsm-consumer [P]
ProxyPassReverse /pub/katello-rhsm-consumer {{ include "httpd.foremanBackend" . }}/unattended/public/katello-rhsm-consumer
{{- end }}
Alias /pub /var/www/pub
<Location /pub>
  Options +FollowSymLinks +Indexes
  Require all granted
</Location>
{{- end }}

{{/* Everything else goes to Foreman. */}}
{{- define "httpd.foremanProxy" -}}
ProxyRequests Off
ProxyPreserveHost On
ProxyAddHeaders On
ProxyPass /pulp !
ProxyPass /pub !
ProxyPass /icons !
ProxyPass /images !
ProxyPass /server-status !
ProxyPass / {{ include "httpd.foremanBackend" . }}/ retry=0 timeout=900 upgrade=websocket
ProxyPassReverse / {{ include "httpd.foremanBackend" . }}/
{{- end }}


{{/* Katello. */}}
{{- define "katello.validate" -}}
{{- range list "dynflow" "pulp" "candlepin" "certificates" "httpd" }}
{{- if not (index $.Values . "enabled") }}
{{- fail (printf "katello.enabled requires %s.enabled" .) }}
{{- end }}
{{- end }}
{{- if not (hasPrefix "https://" .Values.foreman.externalUrl) }}
{{- fail "katello.enabled requires an https foreman.externalUrl: hosts reach /rhsm through it" }}
{{- end }}
{{- end }}

{{- define "katello.candlepinUrl" -}}
{{- printf "https://%s:23443/candlepin" (include "candlepin.fullname" .) }}
{{- end }}

{{- define "katello.pulpSmartProxyName" -}}
{{- .Values.katello.pulpSmartProxy.name | default (printf "%s-pulp" .Values.foreman.fqdn) }}
{{- end }}

{{- define "katello.pulpSmartProxyUrl" -}}
{{- .Values.katello.pulpSmartProxy.url | default (printf "https://%s.%s.svc/pulp/api/v3/smart_proxy" (include "httpd.fullname" .) .Release.Namespace) }}
{{- end }}

{{/*
/etc/foreman/plugins/katello.yaml without the Candlepin OAuth secret, which
the katello-config init container adds. Foreman reads plugin settings as
plain YAML, without ERB or environment variables.
*/}}
{{- define "katello.configuration" -}}
katello.yaml: |
  ---
  :katello:
    :rest_client_timeout: {{ .Values.katello.restClientTimeout | int }}
    :candlepin:
      :url: {{ include "katello.candlepinUrl" . | quote }}
      :oauth_key: katello
      :ca_cert_file: {{ include "foreman.certificatesPath" . }}/ca.crt
{{- end }}

{{/*
Init container that writes katello.yaml with the OAuth secret into the
katello-config volume. Every pod that loads Katello runs it.
*/}}
{{- define "katello.initContainer" -}}
- name: katello-config
  image: {{ include "foreman.image" . }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command:
    - ruby
    - -ryaml
    - -e
    - |
      settings = YAML.safe_load_file("/run/foreman-config/katello.yaml", permitted_classes: [Symbol])
      settings[:katello][:candlepin][:oauth_secret] = ENV.fetch("KATELLO_CANDLEPIN_OAUTH_SECRET")
      File.write("/run/katello/katello.yaml", YAML.dump(settings), perm: 0o600)
  env:
    - name: KATELLO_CANDLEPIN_OAUTH_SECRET
      valueFrom:
        secretKeyRef:
          name: {{ .Values.candlepin.existingSecret }}
          key: {{ .Values.candlepin.oauthSecretSecretKey }}
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL
    runAsNonRoot: true
    runAsGroup: 994
    runAsUser: 994
  volumeMounts:
    - name: configuration
      mountPath: /run/foreman-config
      readOnly: true
    - name: katello-config
      mountPath: /run/katello
  resources:
    requests:
      cpu: 10m
      memory: 32Mi
    limits:
      memory: 128Mi
{{- end }}

{{- define "katello.volume" -}}
- name: katello-config
  emptyDir:
    medium: Memory
    sizeLimit: 1Mi
{{- end }}

{{- define "katello.volumeMount" -}}
- name: katello-config
  mountPath: /etc/foreman/plugins/katello.yaml
  subPath: katello.yaml
  readOnly: true
{{- end }}

{{- define "proxy.image" -}}
{{- include "foreman.imageReference" .Values.proxy.image }}
{{- end }}

{{- define "proxy.fullname" -}}
{{- printf "%s-proxy" (include "foreman.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Default proxy URL: the DNS name of the proxy pod behind its headless Service.
Reverse DNS of the pod IP returns the same name, which is how Foreman
recognizes the proxy on plain HTTP.
*/}}
{{- define "proxy.url" -}}
{{- .Values.proxy.url | default (printf "https://proxy.%s.%s.svc.%s:%v" (include "proxy.fullname" .) .Release.Namespace .Values.proxy.clusterDomain .Values.proxy.service.port) }}
{{- end }}

{{- define "proxy.name" -}}
{{- .Values.proxy.name | default (urlParse (include "proxy.url" .)).hostname }}
{{- end }}

{{/* Foreman URL for the proxy: the httpd front door when it is enabled. */}}
{{- define "proxy.foremanUrl" -}}
{{- if .Values.proxy.foremanUrl }}
{{- .Values.proxy.foremanUrl }}
{{- else if .Values.httpd.enabled }}
{{- printf "https://%s.%s.svc" (include "httpd.fullname" .) .Release.Namespace }}
{{- else }}
{{- printf "http://%s.%s.svc:%v" (include "foreman.fullname" .) .Release.Namespace .Values.service.port }}
{{- end }}
{{- end }}

{{- define "proxy.clientTlsSecretName" -}}
{{- .Values.proxy.clientTls.secretName | default (printf "%s-proxy-client-tls" .Release.Name) }}
{{- end }}

{{- define "proxy.tlsSecretName" -}}
{{- .Values.proxy.tls.secretName | default (printf "%s-proxy-tls" .Release.Name) }}
{{- end }}

{{- define "proxy.sshSecretName" -}}
{{- .Values.proxy.remoteExecutionSsh.secretName | default (printf "%s-proxy-ssh" .Release.Name) }}
{{- end }}

{{/*
Feature names Foreman shows for the enabled proxy modules. The registration
Job waits until the proxy reports all of them.
*/}}
{{- define "proxy.features" -}}
{{- $features := list "Dynflow" "Logs" "Script" -}}
{{- if .Values.proxy.ansible.enabled }}
{{- $features = append $features "Ansible" -}}
{{- end }}
{{- join " " $features -}}
{{- end }}

{{- define "proxy.tlsPath" -}}
/etc/foreman-proxy/tls
{{- end }}

{{- define "proxy.validate" -}}
{{- if .Values.proxy.enabled }}
{{- if not .Values.certificates.enabled }}
{{- fail "proxy.enabled requires certificates.enabled: Foreman calls the proxy over HTTPS with its client certificate" }}
{{- end }}
{{- if not .Values.dynflow.enabled }}
{{- fail "proxy.enabled requires dynflow.enabled: remote execution runs as Foreman tasks" }}
{{- end }}
{{- end }}
{{- end }}
