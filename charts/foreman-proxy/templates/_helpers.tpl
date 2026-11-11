{{- define "foreman-proxy.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "foreman-proxy.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "foreman-proxy.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Name of a component resource: <fullname>-<component>, with the fullname
shortened so that the result fits in 63 characters. Takes
(list $context "component").
*/}}
{{- define "foreman-proxy.componentName" -}}
{{- $context := index . 0 }}
{{- $component := index . 1 }}
{{- $fullname := include "foreman-proxy.fullname" $context | trunc (int (sub 62 (len $component))) | trimSuffix "-" }}
{{- printf "%s-%s" $fullname $component }}
{{- end }}

{{- define "foreman-proxy.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "foreman-proxy.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "foreman-proxy.selectorLabels" -}}
app.kubernetes.io/name: {{ include "foreman-proxy.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Labels of a component object. Call with (dict "context" $ "component" "httpd").
*/}}
{{- define "foreman-proxy.componentLabels" -}}
{{ include "foreman-proxy.labels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "foreman-proxy.componentSelectorLabels" -}}
{{ include "foreman-proxy.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Image reference from an image value (repository, tag, digest). A digest wins
over the tag and must be sha256:<64 hex digits>.
*/}}
{{- define "foreman-proxy.imageReference" -}}
{{- if .digest }}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" .digest) }}
{{- fail (printf "image digest of %s must be sha256:<64 hex digits>, got %q" .repository .digest) }}
{{- end }}
{{- printf "%s@%s" .repository .digest }}
{{- else }}
{{- printf "%s:%s" .repository .tag }}
{{- end }}
{{- end }}

{{/*
Pod annotation with the sha256 of the ConfigMap data a pod reads, so that
helm upgrade replaces the pods when that data changes. Takes the data as a
string.
*/}}
{{- define "foreman-proxy.configChecksum" -}}
checksum/config: {{ sha256sum . }}
{{- end }}

{{/*
Data of a ConfigMap rendered by a template of this chart, as YAML. Takes
(list $context "file.yaml"); the file must render exactly one ConfigMap.
*/}}
{{- define "foreman-proxy.configMapData" -}}
{{- $context := index . 0 }}
{{- $rendered := include (print $context.Template.BasePath "/" (index . 1)) $context | fromYaml }}
{{- if not $rendered.data }}
{{- fail (printf "%s did not render a ConfigMap with data" (index . 1)) }}
{{- end }}
{{- toYaml $rendered.data }}
{{- end }}

{{/* A host name check for names that end up in httpd and proxy settings. */}}
{{- define "foreman-proxy.checkHostName" -}}
{{- if not (regexMatch "^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$" .value) }}
{{- fail (printf "%s: %q is not a host name" .what .value) }}
{{- end }}
{{- end }}

{{- define "foreman-proxy.validate" -}}
{{- if and .Values.proxy.containerGateway.enabled (not .Values.proxy.containerGateway.database.existingSecret) }}
{{- fail "proxy.containerGateway.database.existingSecret is required with the container gateway" }}
{{- end }}
{{- if and .Values.proxy.containerGateway.pulpEndpoint (not (hasPrefix "https://" .Values.proxy.containerGateway.pulpEndpoint)) }}
{{- fail "proxy.containerGateway.pulpEndpoint must start with https://" }}
{{- end }}
{{- if not .Values.name }}
{{- fail "name is required: the host name of this Smart Proxy" }}
{{- end }}
{{- include "foreman-proxy.checkHostName" (dict "what" "name" "value" .Values.name) }}
{{- range .Values.serverAliases }}
{{- include "foreman-proxy.checkHostName" (dict "what" "serverAliases" "value" .) }}
{{- end }}
{{- with .Values.registration.previousName }}
{{- include "foreman-proxy.checkHostName" (dict "what" "registration.previousName" "value" .) }}
{{- end }}
{{- if not (hasPrefix "https://" .Values.foreman.url) }}
{{- fail "foreman.url is required and must start with https://" }}
{{- end }}
{{- range (include "foreman-proxy.foremanClientNames" . | fromJsonArray) }}
{{- include "foreman-proxy.checkHostName" (dict "what" "foreman.clientNames" "value" .) }}
{{- end }}
{{- end }}

{{- define "foreman-proxy.foremanUrl" -}}
{{- .Values.foreman.url | trimSuffix "/" }}
{{- end }}

{{/* foreman.clientNames, default the host name of foreman.url, as JSON. */}}
{{- define "foreman-proxy.foremanClientNames" -}}
{{- .Values.foreman.clientNames | default (list (urlParse .Values.foreman.url).hostname) | toJson }}
{{- end }}

{{- define "foreman-proxy.bundleSecretName" -}}
{{- .Values.bundle.secretName | default (printf "%s-bundle" .Release.Name) }}
{{- end }}

{{/* Pod hostAliases, if any. */}}
{{- define "foreman-proxy.hostAliases" -}}
{{- with .Values.hostAliases }}
hostAliases:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{/* Pulp. Copied from the foreman chart, with the mirror settings. */}}
{{- define "pulp.fullname" -}}
{{- printf "%s-pulp" (include "foreman-proxy.fullname" .) | trunc 52 | trimSuffix "-" }}
{{- end }}

{{- define "pulp.image" -}}
{{- include "foreman-proxy.imageReference" .Values.pulp.image }}
{{- end }}

{{/* Labels for a Pulp object. Call with (dict "context" $ "component" "pulp-api"). */}}
{{- define "pulp.labels" -}}
{{ include "foreman-proxy.componentLabels" . }}
{{- end }}

{{- define "pulp.selectorLabels" -}}
{{ include "foreman-proxy.componentSelectorLabels" . }}
{{- end }}

{{- define "pulp.claimName" -}}
{{- .Values.pulp.storage.existingClaim | default (printf "%s-storage" (include "pulp.fullname" .)) }}
{{- end }}

{{/*
/etc/pulp/settings.py. The foremanctl Pulp environment for a Smart Proxy with
content (src/roles/pulp, pulp_mirror true): the smart_proxy plugin reports
mirror mode and the RHSM URL to Foreman, and there are no import or export
paths. Values are rendered as JSON literals, which Python reads as the same
strings and lists. Secrets are added by pulp.env.
*/}}
{{- define "pulp.settings" -}}
{{- $s := .Values.pulp.settings -}}
{{- $origin := $s.contentOrigin | default (printf "https://%s" .Values.name) -}}
settings.py: |
  # Rendered by the foreman-proxy Helm chart.
  DATABASES__default__ENGINE = "django.db.backends.postgresql"
  DATABASES__default__HOST = {{ .Values.pulp.database.host | toJson }}
  DATABASES__default__PORT = {{ .Values.pulp.database.port | int }}
  DATABASES__default__NAME = {{ .Values.pulp.database.name | toJson }}
  DATABASES__default__USER = {{ .Values.pulp.database.user | toJson }}
  DATABASES__default__OPTIONS__sslmode = {{ .Values.pulp.database.sslMode | toJson }}
  ENABLED_PLUGINS = {{ $s.enabledPlugins | toJson }}
  CONTENT_ORIGIN = {{ $origin | toJson }}
  SMART_PROXY_PULP_URL = {{ $s.smartProxyPulpUrl | default (printf "https://%s" .Values.name) | toJson }}
  SMART_PROXY_MIRROR = True
  SMART_PROXY_RHSM_URL = {{ $s.rhsmUrl | default (printf "https://%s/rhsm" .Values.name) | toJson }}
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
  ALLOWED_IMPORT_PATHS = []
  ALLOWED_EXPORT_PATHS = []
  LOGGING__handlers__console__level = {{ $s.logLevel | upper | toJson }}
  LOGGING__loggers____level = {{ $s.logLevel | upper | toJson }}
  {{- if eq (include "pulp.storageType" .) "s3" }}
  {{- $s3 := .Values.pulp.storage.s3 }}
  {{- $options := dict "bucket_name" $s3.bucket "signature_version" $s3.signatureVersion }}
  {{- with $s3.endpointUrl }}{{ $_ := set $options "endpoint_url" . }}{{ end }}
  {{- with $s3.region }}{{ $_ := set $options "region_name" . }}{{ end }}
  {{- with $s3.addressingStyle }}{{ $_ := set $options "addressing_style" . }}{{ end }}
  {{- if $s3.caSecret.name }}{{ $_ := set $options "verify" (include "pulp.s3CaPath" .) }}
  {{- else if not $s3.verify }}{{ $_ := set $options "verify" false }}{{ end }}
  {{- $options = merge (deepCopy $s3.options) $options }}
  # Content in S3 or an S3-compatible object store, as in the Pulp storage
  # documentation. Credentials come from AWS_* environment variables.
  MEDIA_ROOT = ""
  STORAGES = {
      "default": {
          "BACKEND": "storages.backends.s3.S3Storage",
          "OPTIONS": {{ include "pulp.pythonLiteral" $options }},
      },
      "staticfiles": {
          "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
      },
  }
  REDIRECT_TO_OBJECT_STORAGE = {{ include "pulp.pythonLiteral" $s3.redirectToObjectStorage }}
  {{- end }}
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
{{- $s3 := .Values.pulp.storage.s3 }}
{{- if and (eq (include "pulp.storageType" .) "s3") $s3.existingSecret }}
- name: AWS_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ $s3.existingSecret }}
      key: {{ $s3.accessKeySecretKey }}
- name: AWS_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $s3.existingSecret }}
      key: {{ $s3.secretKeySecretKey }}
{{- end }}
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
      {{- $ca := .context.Values.pulp.storage.s3.caSecret }}
      {{- if and (eq (include "pulp.storageType" .context) "s3") $ca.name }}
      - secret:
          name: {{ $ca.name }}
          items:
            - key: {{ $ca.key }}
              path: certs/s3-ca.crt
      {{- end }}
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
/var/lib/pulp/tmp /var/lib/pulp/assets /var/lib/pulp/media
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
{{- include "foreman-proxy.hostAliases" . }}
{{- if and (eq (include "pulp.storageType" .) "filesystem") .Values.pulp.storage.colocate }}
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
{{- with .Values.pulp.extraInitContainers }}
{{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{- define "pulp.podVolumes" -}}
{{ include "pulp.configVolume" (dict "context" . "configMap" (printf "%s-config" (include "pulp.fullname" .))) }}
- name: storage
{{- if eq (include "pulp.storageType" .) "s3" }}
  emptyDir: {}
{{- else }}
  persistentVolumeClaim:
    claimName: {{ include "pulp.claimName" . }}
{{- end }}
{{- with .Values.pulp.extraVolumes }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/* pulp.storage.type, checked: filesystem or s3. */}}
{{- define "pulp.storageType" -}}
{{- $storage := .Values.pulp.storage }}
{{- if not (has $storage.type (list "filesystem" "s3")) }}
{{- fail (printf "pulp.storage.type must be filesystem or s3, not %v" $storage.type) }}
{{- end }}
{{- if and (eq $storage.type "s3") (not $storage.s3.bucket) }}
{{- fail "pulp.storage.s3.bucket is required with pulp.storage.type s3" }}
{{- end }}
{{- if and (eq $storage.type "s3") $storage.s3.caSecret.name (not $storage.s3.verify) }}
{{- fail "pulp.storage.s3.caSecret and pulp.storage.s3.verify=false exclude each other" }}
{{- end }}
{{- range $key := list "access_key" "secret_key" "security_token" "session_token" "session_profile" }}
{{- if hasKey ($storage.s3.options | default dict) $key }}
{{- fail (printf "pulp.storage.s3.options.%s would be rendered into a ConfigMap; put S3 credentials in the Secret named by pulp.storage.s3.existingSecret" $key) }}
{{- end }}
{{- end }}
{{- $storage.type }}
{{- end }}

{{- define "pulp.s3CaPath" -}}
/etc/pulp/certs/s3-ca.crt
{{- end }}

{{/*
A value as a Python literal for settings.py: JSON, except for booleans and
null. Maps are written with sorted keys.
*/}}
{{- define "pulp.pythonLiteral" -}}
{{- if kindIs "bool" . }}{{ ternary "True" "False" . }}
{{- else if kindIs "invalid" . }}None
{{- else if kindIs "map" . }}{
{{- range $i, $key := keys . | sortAlpha }}{{ if $i }}, {{ end }}{{ $key | toJson }}: {{ include "pulp.pythonLiteral" (get $ $key) }}{{ end -}}
}
{{- else if kindIs "slice" . }}[
{{- range $i, $item := . }}{{ if $i }}, {{ end }}{{ include "pulp.pythonLiteral" $item }}{{ end -}}
]
{{- else }}{{ toJson . }}
{{- end }}
{{- end }}

{{/* httpd. */}}
{{- define "httpd.fullname" -}}
{{- include "foreman-proxy.componentName" (list . "httpd") }}
{{- end }}

{{- define "httpd.image" -}}
{{- include "foreman-proxy.imageReference" .Values.httpd.image }}
{{- end }}

{{- define "httpd.pulpApiBackend" -}}
{{- printf "http://%s-api:24817" (include "pulp.fullname" .) }}
{{- end }}

{{- define "httpd.pulpContentBackend" -}}
{{- printf "http://%s-content:24816" (include "pulp.fullname" .) }}
{{- end }}

{{- define "httpd.serverNames" -}}
ServerName {{ .Values.name }}
{{- $service := include "foreman-proxy.fullname" . }}
ServerAlias {{ $service }} {{ $service }}.{{ .Release.Namespace }}.svc {{ $service }}.{{ .Release.Namespace }}.svc.cluster.local
{{- range .Values.serverAliases }}
ServerAlias {{ . }}
{{- end }}
{{- end }}

{{/*
RequestHeader unset for every spelling of a header name. Rack (Foreman,
Katello) and Django (Pulp) map "-" and "_" to the same variable, while
RequestHeader unset removes one exact name (in any case). Takes the name with
hyphens and emits one line per combination of "-" and "_". Copied from the
foreman chart.
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
Request headers that the Foreman server or Pulp trust when this httpd sends
them, removed in every spelling a client may use before httpd sets its own:
external login, the client certificate (the Foreman server keeps
X_RHSM_SSL_CLIENT_CERT from this Smart Proxy and authenticates a host with
it), and the forwarding chain (the Foreman server keeps X-Forwarded-Host from
this Smart Proxy and picks the host's content source by it).
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

{{/*
REMOTE-USER admin for Pulp API calls from the Foreman server. Takes a dict:
context, and names: more client certificate names (the container gateway's).
*/}}
{{- define "httpd.pulpRemoteUser" -}}
RequestHeader unset REMOTE_USER
RequestHeader unset REMOTE-USER
{{- range concat (include "foreman-proxy.foremanClientNames" .context | fromJsonArray) (.names | default list) }}
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

{{/*
/pub with the Foreman server CA, and the consumer RPM that the Foreman server
builds for this Smart Proxy. Takes a dict: context, scheme of the vhost.
*/}}
{{- define "httpd.pub" -}}
{{- $foreman := include "foreman-proxy.foremanUrl" .context }}
RewriteEngine On
RewriteRule ^/pub/katello-rhsm-consumer$ {{ $foreman }}/unattended/public/katello-rhsm-consumer?url={{ .scheme }}://{{ .context.Values.name }} [P]
ProxyPassReverse /pub/katello-rhsm-consumer {{ $foreman }}/unattended/public/katello-rhsm-consumer
Alias /pub /var/www/pub
<Location /pub>
  Options +FollowSymLinks +Indexes
  Require all granted
</Location>
{{- end }}

{{/*
TLS to the Foreman server: verify it against the server CA and present this
Smart Proxy's client certificate, as puppet-foreman_proxy_content does.
*/}}
{{- define "httpd.sslProxy" -}}
SSLProxyEngine On
SSLProxyCACertificateFile /etc/httpd-tls/server-ca.crt
SSLProxyMachineCertificateFile /run/httpd-client/client.pem
{{- end }}

{{/* foreman-proxy. */}}
{{- define "proxy.fullname" -}}
{{- include "foreman-proxy.componentName" (list . "proxy") }}
{{- end }}

{{- define "proxy.image" -}}
{{- include "foreman-proxy.imageReference" .Values.proxy.image }}
{{- end }}

{{- define "proxy.url" -}}
{{- printf "https://%s:%v" .Values.name .Values.proxy.port }}
{{- end }}

{{- define "proxy.tlsPath" -}}
/etc/foreman-proxy/tls
{{- end }}

{{/* foreman-proxy modules this release enables, as a JSON list. */}}
{{- define "proxy.modules" -}}
{{- $modules := list "logs" "dynflow" "registration" "templates" }}
{{- if .Values.proxy.remoteExecutionSsh.enabled }}
{{- $modules = append $modules "remote_execution_ssh" }}
{{- if .Values.proxy.ansible.enabled }}
{{- $modules = append $modules "ansible" }}
{{- end }}
{{- end }}
{{- if .Values.proxy.containerGateway.enabled }}
{{- $modules = append $modules "container_gateway" }}
{{- end }}
{{- toJson $modules }}
{{- end }}

{{- define "proxy.containerGatewayPulpEndpoint" -}}
{{- .Values.proxy.containerGateway.pulpEndpoint | default (printf "https://%s" .Values.name) | trimSuffix "/" }}
{{- end }}

{{/*
Optional plugin gems Bundler loads (FOREMAN_PROXY_ENABLED_PLUGINS). Empty
loads none.
*/}}
{{- define "proxy.plugins" -}}
{{- $modules := include "proxy.modules" . | fromJsonArray }}
{{- $plugins := list }}
{{- range list "remote_execution_ssh" "ansible" "container_gateway" }}
{{- if has . $modules }}
{{- $plugins = append $plugins . }}
{{- end }}
{{- end }}
{{- join " " $plugins }}
{{- end }}

{{/* Feature names Foreman shows for the enabled foreman-proxy modules. */}}
{{- define "proxy.features" -}}
{{- $names := dict "logs" "Logs" "dynflow" "Dynflow" "registration" "Registration" "templates" "Templates"
  "remote_execution_ssh" "Script" "ansible" "Ansible" "container_gateway" "Container_Gateway" }}
{{- $features := list }}
{{- range (include "proxy.modules" . | fromJsonArray) }}
{{- $features = append $features (get $names .) }}
{{- end }}
{{- $features | sortAlpha | join " " }}
{{- end }}

{{- define "proxy.sshSecretName" -}}
{{- .Values.proxy.remoteExecutionSsh.secretName | default (printf "%s-ssh" .Release.Name) }}
{{- end }}

{{- define "registration.fullname" -}}
{{- include "foreman-proxy.componentName" (list . "registration") }}
{{- end }}

{{/*
Gateway API routes to the httpd Service, and optionally the Gateway. Takes a
dict: context (root), gateway (the gateway values), prefix (their values path,
for messages), name (Gateway name), hosts, labels, tls (list of dicts with
name, listener and Service port, one TLSRoute each), http (listener and port
for the HTTPRoute, or empty) and listeners (the created Gateway's listeners:
name, port, protocol).
*/}}
{{- define "httpd.gatewayRoutes" -}}
{{- $ctx := .context }}
{{- $gw := .gateway }}
{{- $parents := $gw.parentRefs }}
{{- if $gw.create.enabled }}
{{- if $parents }}
{{- fail (printf "%s: set either parentRefs or create.enabled, not both" .prefix) }}
{{- end }}
{{- if not $gw.create.className }}
{{- fail (printf "%s.create.className is required with %s.create.enabled" .prefix .prefix) }}
{{- end }}
{{- $parents = list (dict "name" .name) }}
{{- else if not $parents }}
{{- fail (printf "%s.enabled needs parentRefs (an existing Gateway) or create.enabled" .prefix) }}
{{- end }}
{{- range $parents }}
{{- if hasKey . "sectionName" }}
{{- fail (printf "%s.parentRefs: set listener names in %s.listeners, not sectionName" $.prefix $.prefix) }}
{{- end }}
{{- end }}
{{- $apis := $ctx.Capabilities.APIVersions }}
{{- $tlsApi := "" }}
{{- if $apis.Has "gateway.networking.k8s.io/v1/TLSRoute" }}
{{- $tlsApi = "gateway.networking.k8s.io/v1" }}
{{- else if $apis.Has "gateway.networking.k8s.io/v1alpha2/TLSRoute" }}
{{- $tlsApi = "gateway.networking.k8s.io/v1alpha2" }}
{{- else }}
{{- fail (printf "%s.enabled needs the Gateway API TLSRoute (gateway.networking.k8s.io/v1 from Gateway API 1.5 or later, or v1alpha2 from the experimental channel) and an implementation that supports TLS passthrough; the cluster has neither version. For helm template pass --api-versions gateway.networking.k8s.io/v1/TLSRoute" .prefix) }}
{{- end }}
{{- $needed := list }}
{{- if .http }}{{ $needed = append $needed "HTTPRoute" }}{{ end }}
{{- if $gw.create.enabled }}{{ $needed = append $needed "Gateway" }}{{ end }}
{{- range $needed }}
{{- if not ($apis.Has (printf "gateway.networking.k8s.io/v1/%s" .)) }}
{{- fail (printf "%s needs the Gateway API %s (gateway.networking.k8s.io/v1), which the cluster does not have" $.prefix .) }}
{{- end }}
{{- end }}
{{- if $gw.create.enabled }}
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: {{ .name }}
  labels:
    {{- .labels | nindent 4 }}
  {{- with $gw.create.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  gatewayClassName: {{ $gw.create.className }}
  listeners:
    {{- range .listeners }}
    - name: {{ .name }}
      port: {{ .port }}
      protocol: {{ .protocol }}
      {{- if eq .protocol "TLS" }}
      tls:
        mode: Passthrough
      {{- end }}
      allowedRoutes:
        namespaces:
          from: Same
        kinds:
          - kind: {{ ternary "TLSRoute" "HTTPRoute" (eq .protocol "TLS") }}
    {{- end }}
{{- end }}
{{- range $route := .tls }}
---
apiVersion: {{ $tlsApi }}
kind: TLSRoute
metadata:
  name: {{ $route.name }}
  labels:
    {{- $.labels | nindent 4 }}
spec:
  parentRefs:
    {{- include "httpd.parentRefs" (list $parents $route.listener) | nindent 4 }}
  hostnames:
    {{- toYaml $.hosts | nindent 4 }}
  rules:
    - backendRefs:
        - name: {{ $.name }}
          port: {{ $route.port }}
{{- end }}
{{- with .http }}
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: {{ $.name }}
  labels:
    {{- $.labels | nindent 4 }}
spec:
  parentRefs:
    {{- include "httpd.parentRefs" (list $parents .listener) | nindent 4 }}
  hostnames:
    {{- toYaml $.hosts | nindent 4 }}
  rules:
    - backendRefs:
        - name: {{ $.name }}
          port: {{ .port }}
{{- end }}
{{- end }}

{{/* parentRefs as YAML: each parent with sectionName set to the listener, if any. */}}
{{- define "httpd.parentRefs" -}}
{{- $listener := index . 1 }}
{{- $refs := list }}
{{- range index . 0 }}
{{- $ref := deepCopy . }}
{{- if $listener }}{{ $_ := set $ref "sectionName" $listener }}{{ end }}
{{- $refs = append $refs $ref }}
{{- end }}
{{- toYaml $refs }}
{{- end }}
