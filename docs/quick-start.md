# Minimal Foreman quick start

This experimental, unsupported milestone installs a single core Foreman web pod
backed by PostgreSQL. Optionally it adds Dynflow with the Foreman Tasks plugin
for background tasks, Pulp, Candlepin, an HTTPS front door, the Katello
plugin, and a Smart Proxy for Remote Execution and Ansible. A second chart,
`foreman-proxy`, adds a [Smart Proxy with content](#smart-proxy-with-content).
Neither enables high availability or production ingress.

## Dependencies

Use a native Linux AMD64 host with at least 4 CPUs, 8 GiB memory, and 15 GiB free
disk space. The published Foreman image used here requires AMD64; this quick
start does not build images or configure emulation.

Install Podman, Kind 0.33, kubectl 1.34, Helm 4, and Python 3.10 or newer.
Podman is the default Kind provider. Run the cluster commands with the privileges
required by your Podman installation; CI uses rootful Podman. Rootless Podman
requires the host configuration described in the
[Kind rootless guide](https://kind.sigs.k8s.io/docs/user/rootless/).

## Install and test

From the repository root:

```console
make setup-test-env
make lint
make setup-kind
make test
```

`setup-kind` creates a dedicated cluster and writes its kubeconfig under
`artifacts/`, leaving the default kubeconfig unchanged. It refuses to reuse an
existing cluster with the same name. Override `KIND_CLUSTER_NAME` consistently
across commands if that name is already in use. To use Docker explicitly, set
`KIND_EXPERIMENTAL_PROVIDER=docker` on each cluster command.

`test` installs disposable PostgreSQL and Valkey, creates test credentials,
test certificates and a test SSH key, and installs the chart with the HTTPS
front door, Dynflow, Pulp, Candlepin, Katello and the Smart Proxy enabled.
Helm waits for the migration Jobs, the ready Deployments and the Jobs that
register Pulp and the Smart Proxy in Foreman.
Then [pytest-helm-charts](https://github.com/giantswarm/pytest-helm-charts) connects
to that cluster and runs the 68 tests in `tests/integration` that are not
marked `backup` (see [Back up and restore](#back-up-and-restore)) or
`content_proxy` (see [Smart Proxy with content](#smart-proxy-with-content)); the 6 tests
for [Pulp content in S3](#store-pulp-content-in-s3) are skipped unless
`PULP_STORAGE=s3`. For Foreman:
the web Deployment is available, the API reports an active database, the seeded
administrator can query the users API, the web pod has the CA bundle from its
Secret, and the `foreman` user in the web pod can read the client key and verify
the client certificate against the CA bundle. The Dynflow tests are described in
[Run background tasks](#run-background-tasks). For Pulp: the api, content and
worker Deployments are available on one node, the status API reports online
api, content and worker processes with database and Valkey connected, the API
answers only with a known `REMOTE-USER`, only pods allowed by the NetworkPolicy
reach the API, and the content app answers. For Candlepin: its Deployment is
available, `/candlepin/status` reports mode `NORMAL` over HTTPS verified against
the CA bundle for each name in the server certificate, another name is
rejected, and the log contains no credentials. For the front door: HTTPS with
the server certificate verified for the FQDN and the in-cluster Service name,
the redirect from HTTP to HTTPS, the CA download under `/pub` over both
protocols, that client-supplied `REMOTE-USER` and `SSL-CLIENT-*` headers in any
spelling do not log anyone in, that client-supplied `X-Forwarded-*` headers
neither change the client address Foreman sees nor make plain HTTP count as
HTTPS, that a client certificate from the CA is accepted and a self-signed one
is rejected, that only the httpd pods of the release can connect to the
Foreman web port, and that the Pulp API through httpd accepts the Foreman
client certificate as the Pulp administrator and refuses clients without it.
The Katello and Smart Proxy tests are described in
[Enable Katello](#enable-katello) and
[Run remote execution jobs](#run-remote-execution-jobs).
Pytest prints the test count and writes `artifacts/junit.xml` for CI reporting.
The optional in-cluster smoke test can also be run with `helm test`.

The cluster stays running after the test. Inspect a failed installation with:

```console
make diagnostics
```

## Provide certificates

The `foreman` Helm chart reads TLS certificates from Secrets that you create in
the release namespace. It does not create certificates itself. Any tool that
writes these Secrets works, for example cert-manager or `openssl` with
`kubectl create secret`. The names below are the defaults for a release called
`foreman`; the `certificates` section of `charts/foreman/values.yaml` lets you
change them.

| Secret | Type and keys | Contents |
| --- | --- | --- |
| `foreman-ca` | `ca.crt` | CA bundle Foreman trusts for outgoing HTTPS connections |
| `foreman-client-tls` | `kubernetes.io/tls`: `tls.crt`, `tls.key` | Certificate Foreman presents as a TLS client. Its common name (CN) should be `foreman.fqdn` |
| `foreman-server-tls` | `kubernetes.io/tls`: `tls.crt`, `tls.key`, `ca.crt` | HTTPS server certificate of the front door. Its subject alternative names (SANs) must include `foreman.fqdn` and `foreman-foreman-httpd.<namespace>.svc` |

With `certificates.enabled=true` the web pod mounts the CA bundle and the client
certificate read-only under `/etc/foreman/certs`. Foreman uses them, through the
`ssl_ca_file`, `ssl_certificate` and `ssl_priv_key` settings, for HTTPS
connections to Smart Proxies. The migration Job does not mount them.
`certificates.enabled` is `false` by default.

`make test` creates the Secrets with `tests/kind/certificates.sh` and installs
the chart with `certificates.enabled=true`. To create test certificates in
another cluster, pass the namespace, the release name, the client CN and the
server certificate names:

```console
tests/kind/certificates.sh foreman foreman foreman.example.test \
  foreman.example.test foreman-foreman-httpd foreman-foreman-httpd.foreman.svc
```

The script keeps the generated keys under `artifacts/certificates/` and reuses
them on the next run. Use it only for tests.

## Run background tasks

Foreman runs background tasks with Dynflow in Sidekiq processes. Set
`dynflow.enabled=true` to add three Deployments that use the Foreman image:

| Deployment | Queues | Replicas |
| --- | --- | --- |
| `<release>-foreman-dynflow-orchestrator` | `dynflow_orchestrator` | always 1 |
| `<release>-foreman-dynflow-worker` | `dynflow.worker.queues` | `dynflow.worker.replicas` |
| `<release>-foreman-dynflow-worker-hosts-queue` | `hosts_queue` | `dynflow.hostsQueue.replicas` |

The orchestrator assigns tasks to the workers, and only one can be active at a
time, so it always has one replica and is replaced, not rolled, on upgrade.
Enabling Dynflow also enables the Foreman Tasks plugin (`foreman-tasks`) in
every Foreman pod.

Dynflow needs Valkey or Redis. Add its URL without a database number, for
example `redis://valkey:6379`, to the runtime Secret under the key `REDIS_URL`
(`foreman.redisUrlSecretKey`). Dynflow uses database 6. Every Foreman pod then
also keeps the Rails cache in database 4. The chart does not install Valkey.

On a fresh database the three processes start at the same time and one of
them can fail once with `PG::TRSerializationFailure` while scheduling the first
recurring tasks. Kubernetes restarts it and it then runs normally.

`make test` checks that the three Deployments are available, that the
`foreman-tasks` plugin is loaded, that the API ping reports the Valkey cache as
`ok`, and that a scheduled report runs as a task that ends in state `stopped`
with result `success`.

## Run remote execution jobs

Set `proxy.enabled=true` to add a Smart Proxy (`foreman-proxy`) that runs
Remote Execution jobs over SSH and Ansible. It needs `certificates.enabled` and
`dynflow.enabled`. It also enables the `foreman_remote_execution` and
`foreman_ansible` plugins in every Foreman pod (`proxy.ansible.enabled=false`
leaves out Ansible).

The proxy runs as one pod, because it keeps running jobs in memory and loses
them on restart. It listens on HTTPS port 8443. Foreman connects to it with its
client certificate, and the proxy accepts only the names in
`proxy.trustedHosts` (default `foreman.fqdn`). Create more Secrets:

| Secret | Type and keys | Contents |
| --- | --- | --- |
| `foreman-proxy-tls` | `kubernetes.io/tls`: `tls.crt`, `tls.key` | Proxy server certificate. Its subject alternative names (SANs) must include the host name of `proxy.url` |
| `foreman-proxy-client-tls` | `kubernetes.io/tls`: `tls.crt`, `tls.key` | Proxy client certificate, needed when `proxy.foremanUrl` is `https://`. Its CN or a SAN must be the host name of `proxy.url`, and its issuer must be in the `certificates.ca` bundle |
| `foreman-proxy-ssh` | `id_rsa_foreman_proxy`, `id_rsa_foreman_proxy.pub` | SSH key pair the proxy logs in to hosts with |

By default the proxy URL is
`https://proxy.<release>-foreman-proxy.<namespace>.svc.cluster.local:8443`, the
DNS name of the proxy pod behind its headless Service. The proxy sends job
results back to Foreman at `proxy.foremanUrl`. Foreman accepts these calls only
from a registered proxy:

- With the [HTTPS front door](#https-front-door) (`httpd.enabled=true`) the
  default is `https://<fullname>-httpd.<namespace>.svc`. The proxy presents its
  client certificate, httpd verifies it and passes it on, and Foreman matches
  its CN or SANs with the host name of the proxy URL. The proxy verifies httpd
  against the `certificates.ca` bundle.
- Without it the default is the Foreman web Service over plain HTTP, and
  Foreman accepts the calls because reverse DNS of the proxy pod IP gives the
  host name of the proxy URL. Plain HTTP inside the cluster is meant for
  tests.

The host name of `proxy.foremanUrl` is added to Foreman's allowed host names.

After every install and upgrade a Job registers the proxy in Foreman, or
updates its URL, and then refreshes it, which stores the proxy's features and
its SSH public key. Global Registration installs that key on hosts. The Job
runs `bin/rails runner` in the Foreman image against the database, as the
Katello Pulp registration does, so it needs no Foreman credentials. It adds
the proxy to every organization and location that exists at that time and
waits until Foreman lists the Ansible, Dynflow, Logs and Script features and
the public key. Organizations created later need the proxy
assigned by hand (or another `helm upgrade`), because Remote Execution uses
only proxies in the host's organization and location. With Katello this proxy
is separate from the Pulp Smart Proxy that Katello registers.

Hosts must trust the public key, for root or for the user in the
`remote_execution_ssh_user` host parameter. For hosts that Foreman cannot
resolve by name, set the host parameter `remote_execution_connect_by_ip` to
`true`.

`make test` checks that the proxy is registered with those features and
without Pulp, that Foreman stores the public key from the Secret, that it calls Foreman through httpd with a client certificate
named after its URL host, that a feature refresh works, that the proxy serves
the public key from the Secret,
and that a `Run Command - Script Default` job with `uname -a` succeeds on an
SSH server fixture. To run the same job against another machine that trusts
the key, set `REX_TARGET_IP` (and optionally `REX_TARGET_PORT`,
`REX_TARGET_USER`, `REX_TARGET_NAME`) when running pytest.

## Enable Pulp

Pulp is off by default. With `pulp.enabled=true` the chart adds a migration Job
that runs before every install and upgrade, Deployments for the Pulp API
(Service port 24817), the content app (24816) and the tasking worker, and a
PersistentVolumeClaim mounted at `/var/lib/pulp` by all of them. Content can
also live in S3 instead, see [Store Pulp content in S3](#store-pulp-content-in-s3).

Pulp needs a PostgreSQL database, Valkey or Redis, and a Secret, default
`pulp-runtime`, with these keys:

| Key | Contents |
| --- | --- |
| `DATABASE_PASSWORD` | Password of the `pulp.database.user` role |
| `SECRET_KEY` | Django secret key |
| `REDIS_URL` | For example `redis://valkey:6379/8` |
| `database_fields.symmetric.key` | Key for encrypted database fields, from `openssl rand -base64 32 \| tr '+/' '-_'` |

Keep the database field key. Pulp cannot read fields encrypted with a lost key.
Pulp can share one Valkey with Dynflow: Pulp uses the database number in its
URL, 8 in the example, Dynflow uses 6 and the Rails cache 4.
The database host and name, the content and API URLs Pulp advertises, and the
enabled Pulp plugins are values under `pulp`.

The content volume is ReadWriteOnce by default, so the content and worker pods
are scheduled on the node of a Pulp API pod. All Pulp pods therefore run on one
node; set `pulp.storage.colocate=false` only with ReadWriteMany storage.

`pulp.extraInitContainers`, `pulp.extraVolumes`, `pulp.extraVolumeMounts` and
`pulp.extraEnv` are added to the API, content and worker pods and to the
migration Job.

The Pulp API trusts the user name in the `REMOTE-USER` request header and
accepts no passwords. It is meant to sit behind the
[HTTPS front door](#https-front-door), which sets this header from a verified
client certificate. A NetworkPolicy lets only pods labelled
`app.kubernetes.io/component: httpd` of the same release connect to the API.
Anyone who can create pods in the namespace can give a pod these labels. On
clusters whose network plugin ignores NetworkPolicy, any pod that reaches
the API Service can act as any Pulp user.

### Store Pulp content in S3

With `pulp.storage.type=s3` Pulp keeps artifacts and published files in a
bucket in Amazon S3 or an S3-compatible object store, using the
`storages.backends.s3.S3Storage` backend as in the
[Pulp storage documentation](https://pulpproject.org/pulpcore/docs/admin/guides/configure-pulp/configure-storages/).
The chart then creates no PersistentVolumeClaim and drops the node affinity, so
the Pulp pods can run on any node. Each Pulp pod gets an emptyDir at
`/var/lib/pulp` for temporary files, for example packages a sync downloads
before Pulp uploads them.

```yaml
pulp:
  storage:
    type: s3
    s3:
      bucket: pulp
      endpointUrl: https://s3.example.com
      region: us-east-1
      addressingStyle: path
      existingSecret: pulp-s3
```

`existingSecret` names a Secret with the keys `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` (`accessKeySecretKey` and `secretKeySecretKey` change
the key names). The pods receive them as environment variables, and the
settings file contains no credentials. Leave it empty when the pods get
credentials another way, for example from workload identity. For an endpoint
certificate from a private CA, put the CA bundle in a Secret and set
`pulp.storage.s3.caSecret.name`. `pulp.storage.s3.options` passes further
[django-storages S3 options](https://django-storages.readthedocs.io/en/latest/backends/amazon-S3.html),
for example `location` for a key prefix.

By default Pulp's content app serves files from the bucket itself, through
httpd like filesystem content, so the object store needs no client access.
With `pulp.storage.s3.redirectToObjectStorage=true` Pulp answers with a
redirect to a signed URL of the object; hosts then download from the object
store directly and must reach and trust its endpoint.

The chart does not copy content between storage types. `tools/backup.sh` does
not back up the bucket; use the object store's own versioning or replication.

The published Pulp image does not contain `django-storages` and `boto3` yet
([pulp-oci-images#117](https://github.com/theforeman/pulp-oci-images/pull/117)),
so with the image as it is Pulp fails on the first storage access with `No
module named 'storages'`. Until a Pulp image with these packages is released,
the tests use a temporary workaround in
`tests/kind/values-pulp-s3-workaround.yaml`: an init container that installs
the packages, pinned by hash, from PyPI into an emptyDir, and `PYTHONPATH`
pointing at it. It is a test fixture that needs access to PyPI from the pods,
not a supported configuration, and it is removed when the image ships the
packages.

`make test PULP_STORAGE=s3` installs a disposable MinIO with a bucket `pulp`
in the test namespace and installs the chart with
`tests/kind/values-pulp-s3.yaml` and the workaround. It runs the whole
integration suite, and in addition checks that the Pulp pods have no claim and
no node affinity, that Django's default storage is the S3 backend and can
write, read and delete an object, that a synced repository's artifacts appear
in the bucket, that the status API reports the stored size, and that the
content app serves a package with the right checksum. `make test-backup` is
not supported in this mode, because it deletes the namespace that holds MinIO.

## Enable Candlepin

Candlepin manages subscriptions and content access for Katello. Set
`candlepin.enabled=true` to run it as a Deployment with a Service named
`<fullname>-candlepin` on HTTPS port 23443, where `<fullname>` is
`fullnameOverride` or `<release>-foreman`. Candlepin is limited to a single
replica because of its embedded messaging broker.

Candlepin needs its own PostgreSQL database and these Secrets (default names
for a release called `foreman`):

| Secret | Type and keys | Contents |
| --- | --- | --- |
| `candlepin-runtime` | `DATABASE_URL`, `DATABASE_USERNAME`, `DATABASE_PASSWORD`, `OAUTH_SECRET` | JDBC URL such as `jdbc:postgresql://postgresql:5432/candlepin?sslmode=disable`, database credentials, and the OAuth secret Katello will use |
| `foreman-candlepin-ca` | `kubernetes.io/tls`: `tls.crt`, `tls.key` | CA certificate and private key Candlepin uses to sign consumer certificates |
| `foreman-candlepin-tls` | `kubernetes.io/tls`: `tls.crt`, `tls.key` | Tomcat HTTPS server certificate. Its SANs must include `foreman-foreman-candlepin` and `localhost`, and `foreman-ca` must contain its issuer |

The `candlepin` and `certificates.candlepin` sections of
`charts/foreman/values.yaml` change these names, the image, the Java heap and
the resources. A pre-install and pre-upgrade Job applies the database schema
with Liquibase before Foreman's migration. Candlepin then refuses to start if
the schema is not current. `tests/kind/certificates.sh` also creates the two
Candlepin certificate Secrets, signed by the test CA.

Check Candlepin from your workstation through a port forward, verifying its
certificate against the test CA:

```console
KUBECONFIG="$PWD/artifacts/kubeconfig" kubectl --namespace foreman port-forward service/foreman-foreman-candlepin 23443:23443
curl --cacert artifacts/certificates/foreman-foreman/ca.crt https://localhost:23443/candlepin/status
```

## HTTPS front door

With `httpd.enabled=true` the chart runs Apache httpd in front of Foreman, with
the configuration that [foremanctl](https://github.com/theforeman/foremanctl)
uses. The `<fullname>-httpd` Service (`foreman-foreman-httpd` for a release
called `foreman`) listens on port 443 and on port 80, which serves `/pub` and
redirects everything else to HTTPS. Clients may present a certificate signed by
the `foreman-ca` bundle, and httpd passes it to Foreman. Before httpd sets its
own values, it removes every spelling, with `-` or `_`, of the headers that
Foreman, Katello and Pulp trust: `REMOTE-USER`, `SSL-CLIENT-*`,
`X-RHSM-SSL-CLIENT-CERT`, `X-CLIENT-CERT`, `X-Forwarded-*`, `Forwarded` and
`Client-IP`. Foreman then requires HTTPS, and a NetworkPolicy lets only the
httpd pods of the release (and the `helm test` pod) connect to the Foreman web
port, so other pods cannot skip httpd. This needs a network plugin that
enforces NetworkPolicy. Without httpd the chart adds no such policy, because
Foreman then trusts none of these headers.
The server CA is published at `/pub/katello-server-ca.crt`.

The Service is `ClusterIP` by default. Set `httpd.service.type=LoadBalancer`,
and `httpd.service.externalTrafficPolicy=Local` to keep client addresses, to
expose it outside the cluster. Set `foreman.trustedProxies` to the pod network
range so Foreman logs the client address that httpd forwards; httpd replaces
any `X-Forwarded-For` the client sends. With Pulp
enabled, httpd also routes `/pulp/api/v3`, `/pulp/content` and the other Pulp
paths, and maps a client certificate whose CN is `foreman.fqdn` to the Pulp
administrator. Set `pulp.settings.smartProxyPulpUrl` to the in-cluster name of
the front door, `https://<fullname>-httpd.<namespace>.svc`, because `foreman.fqdn`
may not resolve inside the cluster; `pulp.settings.contentOrigin` stays
`https://<foreman.fqdn>`. `make test` sets both. Candlepin is never exposed;
Foreman answers `/rhsm` itself.

## Enable Katello

Katello is a Foreman plugin for content and subscription management. Set
`katello.enabled=true` together with `dynflow.enabled`, `pulp.enabled`,
`candlepin.enabled`, `certificates.enabled` and `httpd.enabled`, and use an
`https://` `foreman.externalUrl`. Rendering fails otherwise. Every Foreman pod
then loads the `katello` and `foreman_remote_execution` plugins and reads
`/etc/foreman/plugins/katello.yaml`.

Katello connects to Candlepin at `https://<fullname>-candlepin:23443/candlepin`
with the OAuth secret from the Candlepin Secret (`OAUTH_SECRET`). Foreman reads
plugin settings as plain YAML, so an init container writes the file with the
secret into a memory-backed volume. Katello verifies Candlepin against the
`foreman-ca` bundle. Pulp checks client entitlement certificates against the
same bundle, so it must also contain the CA certificate from
`foreman-candlepin-ca`.

Katello finds Pulp through a Smart Proxy with the Pulpcore feature. After every
install and upgrade a Job registers Pulp under the name `<foreman.fqdn>-pulp`
with the URL `https://<fullname>-httpd.<namespace>.svc/pulp/api/v3/smart_proxy`
(`katello.pulpSmartProxy`). Foreman fetches the Pulp features through httpd
with its client certificate. Katello then calls Pulp at
`pulp.settings.smartProxyPulpUrl` and hosts download content from
`pulp.settings.contentOrigin`.

The Katello event daemon runs inside the Foreman web process. The web container
removes the daemon's PID file before it starts, because a file left by a
restarted container stops the daemon from starting.

`make test` checks that `/katello/api/ping` reports Candlepin, Candlepin
authentication, Foreman Tasks, the event daemon, the Pulp API and the Pulp
content app as `ok`, and that Pulp is registered as a Smart Proxy. It then
creates an organization with a product and a yum repository, syncs it,
checks that the repository metadata and a package are served only with an
entitlement certificate, publishes a content view, creates an activation key, registers a
consumer with it, and deletes the organization. A self-signed certificate for
that consumer, sent as an `X-RHSM-SSL-CLIENT-CERT` header in any `-`/`_`
spelling by a client without a valid client certificate, must not
authenticate as the consumer. The repository is a copy of the Pulp project's small
`rpm-unsigned` fixture. `tests/kind/rpm-repository.sh` downloads it on the
machine that runs the tests and serves it inside the cluster at
`http://rpm-repository/`, so the cluster needs no outbound access for it.

## Prepare for Smart Proxies with content

A Smart Proxy with content (a capsule) runs its own Pulp, syncs content from
this server and forwards the `/rhsm` requests of its hosts here. The
`foreman-proxy` Helm chart deploys one, see
[Smart Proxy with content](#smart-proxy-with-content). The `foreman` Helm
chart has the settings a capsule needs:

- `httpd.trustedProxyClientNames`: client certificate CNs of capsules. A
  capsule forwards a host's certificate as `X_RHSM_SSL_CLIENT_CERT`, which is
  how puppet-foreman_proxy_content and foremanctl capsules send it, and its
  own name as `X-Forwarded-Host`, which Katello uses to pick the host's content
  source. httpd keeps exactly these two headers, and only the last
  `X-Forwarded-Host` value, when the request carries a certificate that it
  verified against the CA and whose CN is in the list. From every other
  client it removes both, in every spelling, as before. A listed capsule can
  act as any host, so list only capsules you run.
- `foreman.oauth.existingSecret`: a Secret with `OAUTH_CONSUMER_KEY` and
  `OAUTH_CONSUMER_SECRET` (key names configurable). An init container of the
  web pods writes them, with `oauth_active: true` and `oauth_map_users: false`,
  into a settings file in a memory-backed volume. Requests signed with this
  pair act as an administrator, as with foremanctl, so treat the Secret like
  the admin password.
- `foreman.hostAliases`: `/etc/hosts` entries for the web and Dynflow pods and
  the registration Jobs. Foreman connects to every Smart Proxy by the host name
  in its URL, so capsule names must resolve inside the cluster, through DNS or
  these entries.
- `httpd.service.clusterIP`: a fixed address for the httpd Service, so that
  `foreman.hostAliases` can point `foreman.fqdn` at it.

A capsule syncs from the Pulp URL that Pulp advertises,
`pulp.settings.smartProxyPulpUrl`, the same URL Katello uses itself. A capsule
outside the cluster needs it to be `https://<foreman.fqdn>`, which then has to
resolve to httpd inside the cluster too: set `httpd.service.clusterIP`, map
`foreman.fqdn` to it in `foreman.hostAliases` and set
`katello.pulpSmartProxy.url` to `https://<foreman.fqdn>/pulp/api/v3/smart_proxy`.
The CA bundle must contain the CA of the httpd server certificate, because the
capsule verifies this server with it.

`make test` lists `capsule.test` in `httpd.trustedProxyClientNames`, issues a
client certificate for it from the test CA, sets an OAuth consumer and maps
`foreman.test` to a fixed httpd address. It checks that a request with that
certificate and a host's certificate in `X_RHSM_SSL_CLIENT_CERT` authenticates
as the host, while the same header in another spelling, from a CA-signed
certificate that is not listed, or without a certificate does not. It checks
that a host registered through the capsule certificate records the capsule as
`registered_through` and that a client without it cannot choose that value.
For OAuth it checks that the settings are read-only, that Foreman hides the
key and secret, that a signed request is accepted and unsigned or wrongly
signed ones are refused, and that the web pod logs do not contain the secret.
It checks that the web and Dynflow pods resolve `foreman.test` to httpd and
that the Pulp Smart Proxy can be registered under
`https://foreman.test/pulp/api/v3/smart_proxy`.

## Replicas and disruptions

Workloads without local state can run several replicas. The others always run
one, so a node failure or drain interrupts them until Kubernetes starts a
replacement pod.

| Component | Replicas | When its node is drained or lost |
| --- | --- | --- |
| Foreman web | `replicaCount` | The other replicas keep serving. Sessions are stored in the database |
| httpd | `httpd.replicas` | The other replicas keep serving |
| Dynflow workers | `dynflow.worker.replicas`, `dynflow.hostsQueue.replicas` | The other replicas keep running tasks. Sidekiq waits up to 25 seconds for running jobs |
| Dynflow orchestrator | always 1 | No new task starts until the replacement is ready |
| Candlepin | always 1 | Candlepin is limited to a single replica because of its embedded messaging broker. Requests that need it wait or fail until the replacement is ready, about 30 to 50 seconds in Kind |
| Smart Proxy | always 1 | It keeps running jobs in memory. No job runs until the replacement is ready |
| Pulp api, content, worker | `pulp.api.replicas`, `pulp.content.replicas`, `pulp.worker.replicas` | With ReadWriteOnce storage all Pulp pods run on one node. Content and the Pulp API are unavailable until a pod can mount the volume again; with storage that is local to the node, until the node is back |

For every workload with more than one replica the chart adds a
PodDisruptionBudget (PDB) that lets `kubectl drain` and other voluntary
evictions stop one pod at a time, and spreads the replicas across nodes when
the cluster has enough of them (`podDisruptionBudget` and `topologySpread` in
`charts/foreman/values.yaml`). Pulp gets neither while its storage is a
colocated volume (filesystem storage with `pulp.storage.colocate` true),
because its pods must share one node. A drain waits until the replacement
pod is ready; on a single-node cluster with more than one replica it cannot
finish. There, set `podDisruptionBudget.enabled=false` or run one replica. Web
and httpd pods wait 10 seconds after they are asked to stop, so that no new
request reaches them, and then finish the running requests. A web pod is
ready when it serves the login page. The API ping is not used for this,
because with Katello it waits for Candlepin and Pulp, so their restart would
remove every web pod from the Service. Each web process keeps 4 database
connections more than it has Puma threads, as foremanctl does, for threads
such as the Katello event daemon. With Katello, every web pod runs its own
event daemon; whether several daemons handle an event twice has not been
tested.

Every pod of the chart, including the hook Jobs, their init containers and the
`helm test` pod, meets the `restricted`
[Pod Security Standard](https://kubernetes.io/docs/concepts/security/pod-security-standards/).
`make test` installs the chart and the test fixtures into a namespace
labelled `pod-security.kubernetes.io/enforce=restricted`.

`make test-disruption` drains a node under load. It needs a Kind cluster with
a control-plane node and two workers:

```console
make setup-kind KIND_CONFIG=tests/kind/cluster-disruption.yaml
make test-disruption
make teardown-kind
```

It installs the chart as `make test` does, with two web, httpd and Dynflow
worker replicas (`DISRUPTION_HELM_ARGS`), and keeps PostgreSQL and Valkey on the
control-plane node. A pod on that node requests `/api/v2/status` as the
administrator through the httpd Service, about four times a second, while
`kubectl drain` empties the worker that runs Pulp. Every request must succeed
until the node is uncordoned and every Deployment is available again. Without
Katello the pod also requests `/api/v2/ping`, which must succeed too. With
Katello it requests the Pulp status instead, and then deletes the Candlepin
pod while a second pod requests `/api/v2/ping` and `/rhsm/status`. The Pulp and
Candlepin results are written to `artifacts/disruption-report.json`, not
asserted.

## Smart Proxy with content

The `foreman-proxy` Helm chart in `charts/foreman-proxy` deploys a Smart Proxy
with content, also called a capsule, as its own release. It serves content and
registration to hosts for a Foreman server with the Katello plugin that the
`foreman` Helm chart installed. A release runs:

- Pulp in mirror mode: API, content app and worker Deployments and a migration
  Job, with the same `pulp.storage` choices as in [Enable Pulp](#enable-pulp)
  (a volume or an S3 bucket)
- httpd on ports 80 and 443: content under `/pulp/content`, the Foreman
  server's CA under `/pub`, the Pulp API for Katello, and `/rhsm`,
  `/api/registration_commands` and three more paths forwarded to the Foreman
  server
- foreman-proxy on port 8443 with the Registration, Templates, Dynflow and
  Logs modules, and optionally Remote Execution and Ansible. It does not load
  the container gateway

One Service carries ports 80, 443 and 8443, so that all three answer under one
host name, the `name` value. Katello pairs the two Smart Proxy records of a
capsule by the host name of their URLs. Hosts and the Foreman server must
resolve `name` to that Service.

Pulp needs its own PostgreSQL database, Valkey or Redis, and a Secret, default
`pulp-runtime`, with the keys from [Enable Pulp](#enable-pulp). Pulp reports
its Valkey connection in its status, and Katello treats a Pulp without one as
unavailable.

### Identity bundle

The release reads its identity from one Secret, default `<release>-bundle`. It
is the Kubernetes form of the auth bundle that `foremanctl` builds for a
Smart Proxy:

| Key | Contents |
| --- | --- |
| `ca.crt` | CA bundle of the Foreman server; httpd and foreman-proxy verify client certificates with it |
| `server-ca.crt` | CA of the Foreman server's HTTPS certificate; httpd and foreman-proxy verify the Foreman server with it, and hosts download it from `/pub/katello-server-ca.crt` |
| `tls.crt`, `tls.key` | Server certificate of the capsule; its SANs must include `name` and `serverAliases`, and `server-ca.crt` must sign it |
| `client.crt`, `client.key` | Client certificate of the capsule; its CN must be `name`, and the Foreman server's CA bundle must sign it |
| `oauth-consumer-key`, `oauth-consumer-secret` | OAuth consumer of the Foreman server, read only by the registration Job |

`tools/foreman-proxy-bundle.sh` reads the CA bundle, the server CA and the
OAuth consumer from a `foreman` release, takes the capsule certificates from
files, checks that they fit together and creates the Secret. It never prints
the Secret:

```console
tools/foreman-proxy-bundle.sh --namespace foreman --release foreman \
  --server-cert capsule.crt --server-key capsule.key \
  --client-cert capsule-client.crt --client-key capsule-client.key \
  --secret capsule-bundle --apply capsule capsule.example.test
```

Use `--context` and `--apply-context` when the two releases are in different
clusters, or `--output FILE` to write the manifest instead.

The `foreman` release must have the settings from
[Prepare for Smart Proxies with content](#prepare-for-smart-proxies-with-content):
`foreman.oauth.existingSecret`, `name` in `httpd.trustedProxyClientNames`, and
`name` resolvable from its pods. The capsule's Pulp downloads content from the
Pulp URL that the Foreman server's Pulp advertises
(`pulp.settings.smartProxyPulpUrl` of the `foreman` release), so that URL must
work from the capsule's pods. `hostAliases` adds `/etc/hosts` entries to every
pod of the capsule release when names are not in DNS.

### Install

```console
helm install capsule charts/foreman-proxy --namespace capsule \
  --set name=capsule.example.test --set foreman.url=https://foreman.example.test \
  --set 'registration.lifecycleEnvironments={Library}'
```

After every install and upgrade a Job registers two Smart Proxy records in
Foreman with requests signed by the OAuth consumer, as `foremanctl` does:
`<name>` with the URL `https://<name>:8443` and `<name>-pulp` with the URL
`https://<name>/pulp/api/v3/smart_proxy`. It creates or updates both, adds the
organizations and locations in `registration.organizations` and
`registration.locations` (all that exist, when empty) to the ones already
assigned, refreshes their features and waits until Foreman lists Registration,
Templates, Dynflow and Logs (plus the optional modules below), and Pulpcore.
Then it adds the lifecycle environments named in
`registration.lifecycleEnvironments` to `<name>-pulp` in each of its
organizations, and sets `registration.downloadPolicy` when it is not empty. Organizations created later need another `helm upgrade` or an
assignment in Foreman.

### Content and hosts

Sync the assigned lifecycle environments to the capsule in Foreman, or with
`POST /katello/api/capsules/<id>/content/sync`, where `<id>` is the
`<name>-pulp` record. With the `on_demand` download policy, the default, a sync
copies only metadata. The first request for a package makes the capsule's
Pulp fetch it from the Foreman server, send it to the host and keep it; later
requests are served from the capsule's storage.

To register a host through the capsule, select the `<name>` Smart Proxy in
Global Registration. The host fetches the registration script from
`https://<name>:8443/register`, which foreman-proxy forwards to the Foreman
server with its client certificate. The script points subscription-manager at
`https://<name>/rhsm` and the content at `https://<name>/pulp/content/`.

httpd forwards `/rhsm` to `foreman.url` with the capsule's client
certificate. It sends the host's certificate as `X_RHSM_SSL_CLIENT_CERT` and
`name` as `X-Forwarded-Host`, from which the Foreman server records the
capsule as the host's registration proxy and content source. Before that,
httpd removes every `-`/`_` spelling of these headers that a host sent, and of
`REMOTE-USER`, `SSL-CLIENT-*`, `X-CLIENT-CERT`, `X-Forwarded-*`, `Forwarded`
and `Client-IP`. It does not pass on the `Host` header the host chose.

The Foreman server calls foreman-proxy on port 8443 and the Pulp API on port
443 with its client certificate. foreman-proxy accepts and httpd maps to the
Pulp administrator only the names in `foreman.clientNames`, by default the
host name of `foreman.url`. As in the `foreman` chart, a NetworkPolicy lets
only the httpd pods of the release connect to the Pulp API.

### Remote Execution and Ansible

`proxy.remoteExecutionSsh.enabled=true` turns on Remote Execution over SSH
(feature Script) and, unless `proxy.ansible.enabled=false`, Ansible. Create a
Secret, default `<release>-ssh`, with the SSH key pair in the keys
`id_rsa_foreman_proxy` and `id_rsa_foreman_proxy.pub`, as for the `foreman`
chart in [Run remote execution jobs](#run-remote-execution-jobs). The
registration Job then also waits until Foreman has stored the public key,
which Global Registration installs on hosts. The `foreman` release loads
`foreman_remote_execution` with Katello, and `foreman_ansible` only with its
own `proxy.enabled=true` (and `proxy.ansible.enabled`).

The capsule runs a job only if Foreman picks it for the host. Foreman tries,
in order, the Remote Execution proxies of the host's subnet, then any proxy in
the host's organization and location; with the Foreman setting
`remote_execution_prefer_registered_through_proxy` on, it tries the Smart
Proxy the host registered through first. The capsule connects to the host
directly, and sends the job's results to `foreman.url` with its client
certificate.

foreman-proxy keeps running jobs in memory, so a job running when its pod
restarts is lost. Ansible runs on the capsule as the foreman-proxy user. A user
who may write Ansible job templates can read every file and environment
variable of that pod, including the bundle's keys.

`make test-content-proxy`, after `make test`, issues the capsule certificates
from the test CA, builds the bundle, installs the chart as `capsule.test` in
the namespace `capsule` of the same Kind cluster, with the restricted Pod
Security profile enforced, with its own PostgreSQL and Valkey, Remote
Execution on, and an SSH server fixture that trusts only the capsule's key.
It runs the tests in `tests/integration` marked `content_proxy`. They check
that the Service sends each port only to its pods, that both records exist
with their features and Default Organization's Library, that `/pub` serves
the server CA and the consumer script, and that a
second `helm upgrade` adds a new organization and its Library without
duplicate records. With a repository synced to the capsule, they fetch the
registration script through port 8443 and check that it points at the
capsule, register a host through the capsule's `/rhsm` while sending a
forged `X-Forwarded-Host`, and check that Foreman records the capsule as the
host's registration proxy and content source. The host's certificate
authenticates it through the capsule, while the same certificate sent as a
header in any spelling does not. A package downloads from the capsule with the
host's entitlement certificate and not without it, matches the copy on the
Foreman server, is fetched from the Foreman server once and stays in the
capsule's Pulp, and still downloads while the Foreman server's httpd is scaled
to zero. For a host in a subnet whose Remote Execution proxy is the capsule, a
`Run Command - Script Default` job with `uname -a` and an Ansible job (the
`raw` module, because the fixture has no Python) succeed, Foreman's job
details name the capsule as the proxy that ran them, and the Foreman server's
httpd logs the result callbacks from the capsule's pod.

## Upgrade

Tags such as `nightly` move. To know which image runs before and after an
upgrade, pin each image by digest. Every image value (`image`, `pulp.image`,
`candlepin.image`, `proxy.image`, `httpd.image`, `test.image`) takes a
`digest`, which takes precedence over `tag`, for example
`--set-string image.digest=sha256:6e3772a9...`. The chart rejects a digest
that is not `sha256:` followed by 64 hex digits. The Foreman image digest
applies to the web, Dynflow, migration and Katello Job pods together.

Upgrade with the same command and values as the installation, changing only
the digests:

```console
helm upgrade foreman charts/foreman --namespace foreman --values my-values.yaml \
  --set-string image.digest=sha256:<new digest> --wait --wait-for-jobs --timeout 30m
```

Helm runs the migration Jobs before it changes anything else, one after the
other: Candlepin, then Foreman, then Pulp. Only when all of them succeed does
it update the Deployments. With `--wait` it then waits for the new pods to
be ready, and runs the Jobs that register Pulp and the Smart Proxy in Foreman.

A change of values that only changes configuration also replaces pods. Every
workload that reads a chart ConfigMap (Foreman web, the Dynflow Deployments,
Candlepin, the Pulp Deployments, httpd and the Smart Proxy) has a
`checksum/config` pod annotation with the sha256 of that ConfigMap data, so
`helm upgrade` restarts exactly the workloads whose configuration changed.
Credentials are not part of the checksum: after changing an existing Secret,
restart the pods that read it with `kubectl rollout restart`.

If a migration Job fails, Helm stops there:

- `helm upgrade` exits with an error naming the Job, for example
  `pre-upgrade hooks failed: resource Job/foreman/foreman-foreman-migrate-2 not ready. status: Failed`.
  Helm reports this as soon as the Job reaches its `backoffLimit`, without
  waiting for `--timeout`.
- The new revision is marked `failed` in `helm history`. Deployments,
  ConfigMaps and Services keep the previous revision, and the running pods
  keep serving with the old image.
- The failed Job and its pod stay for inspection with `kubectl logs job/<name>`.
  Helm deletes only succeeded migration Jobs. Delete failed ones by hand when
  you no longer need them.
- Migrations that ran before the failed one stay applied. For example, when
  the Pulp migration fails, the Foreman database already has the schema of the
  new image while the old Foreman pods run on it. Database migrations cannot be
  undone by Helm.

To recover, roll forward: fix the cause (credentials, database access, image)
and run the same `helm upgrade` again. Helm upgrades from a failed revision
without extra flags, and every migration Job runs again under a new name,
because the name contains the revision. Do not use `helm rollback` to undo
migrations: it reapplies the old manifests but does not run migrations or
restore the databases. `--rollback-on-failure` does the same automatically:
after a failed migration it adds a `Rollback to <n>` revision and leaves the
running pods alone. Restore the databases from a backup if you need the old
schema (see [Back up and restore](#back-up-and-restore)).

`make test-upgrade`, run in a cluster created by `make setup-kind`, installs
the chart with the Foreman 5.0.1 release image (`UPGRADE_FROM_IMAGE_DIGEST`)
and runs the tests in `tests/upgrade`. They upgrade to the image that the
`nightly` tag points to when they start (`UPGRADE_TO_IMAGE_TAG`), or to
`UPGRADE_TO_IMAGE_DIGEST` when it is set, and write both digests to
`artifacts/upgrade/images.txt`. Old nightly digests are removed from the
registry after a while, release tags stay. After a new release, set
`UPGRADE_FROM_IMAGE_DIGEST` in the `Makefile` to the digest of its tag, for
example with
`skopeo inspect --format '{{.Digest}}' docker://quay.io/foreman/foreman:5.0.1`. For the Candlepin, Foreman and Pulp migration
in turn, they change the database password in the Secret that the Job reads.
Running pods keep the credentials they started with, so only the new Job
fails. They check that `helm upgrade` to the new image fails on
that Job, that no Deployment, ReplicaSet or pod changes, and that the Foreman
and Katello pings succeed during and after the attempt. Then they restore the
Secret and upgrade again, which must succeed, finish every migration Job
before the first new ReplicaSet is created and keep data created before the
upgrade. Then they change `foreman.externalUrl` only, which Foreman reads
from `settings.yaml` as the `foreman_url` setting: the Foreman web and Dynflow
pods must be replaced and report the new value, every other pod must stay,
and a second upgrade restores the value. Finally the tests in
`tests/integration` that are not marked `backup` run against the upgraded
release. Helm output of each upgrade is written to `artifacts/upgrade/`.
CI runs `make test-upgrade` as a separate job in a new cluster.

## Back up and restore

`tools/backup.sh` copies a release to a directory on the machine that runs it.
`tools/restore.sh` installs that copy into a namespace that does not have the
release, for example after the namespace or the cluster was lost. The scripts
follow `foremanctl backup` and `foremanctl restore`. They need kubectl, Helm 4
and jq, and run their database and file commands in a short-lived helper Job in
the release namespace.

A backup is a directory `foreman-backup-<UTC time>` with:

| File | Contents | Needed to restore |
| --- | --- | --- |
| `foreman.dump`, `candlepin.dump`, `pulp.dump` | `pg_dump` custom format dumps of the three databases | Yes |
| `pulp-content.tar.gz` | The `media` directory of the Pulp volume: artifacts and published files. Not created with S3 storage | Yes, unless content is synced again |
| `secrets/*.json` | Every Secret the release's workloads read | See below |
| `values.yaml` | The user-supplied chart values | Yes |
| `metadata.json` | Namespace, release, chart version, image digests, file list | Yes |

The Secrets matter for different reasons:

| Secret (default name) | Why it is kept |
| --- | --- |
| `foreman-runtime` | `ENCRYPTION_KEY` decrypts passwords stored in the Foreman database. Also the database URL and the session key |
| `pulp-runtime` | `database_fields.symmetric.key` decrypts Pulp database fields. Also the Django key and the database password |
| `candlepin-runtime` | Database credentials and the OAuth secret Katello and Candlepin share |
| `foreman-candlepin-ca` | Signs the certificates registered hosts hold. A new CA locks those hosts out |
| `foreman-ca` | The CA bundle; it contains the Candlepin CA |
| `foreman-proxy-ssh` | The key hosts trust for Remote Execution |
| `foreman-client-tls`, `foreman-server-tls`, `foreman-candlepin-tls`, `foreman-proxy-tls`, `foreman-proxy-client-tls` | Can be issued again from the same CA. The in-cluster names in them include the namespace |

The backup holds every credential of the release. Store it encrypted and
readable only by administrators.

The directory is local rather than a PersistentVolumeClaim in the cluster
because a claim in the release namespace is deleted with it, and a
`local-path` volume is lost with its node. Copy the directory to separate
storage. Everything passes through the Kubernetes API with `kubectl exec` and
`kubectl cp`, which is slow for large Pulp volumes. The scripts have no option
to skip the content.

### Back up

```console
tools/backup.sh --namespace foreman --release foreman /srv/backups
```

The script refuses to start while Foreman or Pulp tasks are running. It scales
every Deployment of the release to zero, dumps the databases, archives the Pulp
content and scales the Deployments back, so Foreman is unavailable for the
duration. With `--online` it leaves the release running. The three dumps and the
content are then copied one after another while the application can change
them. `--image` selects the helper image, default `postgres:16-alpine`
pinned by digest; its PostgreSQL major version must match the database server. Valkey is not backed
up: it holds caches and the task queue.

### Restore

Prepare the target namespace as for a new installation: empty `foreman`,
`candlepin` and `pulp` databases owned by the same users with the same
passwords, and Valkey. The release must not exist there. Then run:

```console
tools/restore.sh --namespace foreman /srv/backups/foreman-backup-20261110T120000Z
```

The script creates the backed-up Secrets that do not exist yet, restores the
databases with `pg_restore`, creates the Pulp claim and unpacks the content into
it, then runs `helm install` with the backed-up values. Helm adopts the claim.
The migration Jobs find current schemas with nothing to migrate, and the Pulp and
Smart Proxy registration Jobs update the Smart Proxy URLs. Arguments after
`--` go to `helm install`. Use the chart version that made the backup
(`--chart`). The script stops if a database already has tables, and
`--force` replaces them, so after a failed restore run it again with
`--force`.

To restore into a namespace with another name, the in-cluster names change:

- Create the server certificates for the new Service names before the restore,
  signed by the same CA. The script keeps Secrets that already exist.
- Pass `--set-string pulp.settings.smartProxyPulpUrl=https://<release>-foreman-httpd.<new namespace>.svc`
  and any other value that names the namespace.
- Pass `--set-string proxy.name=<old name>`, by default
  `proxy.<release>-foreman-proxy.<old namespace>.svc.cluster.local`, so that
  the registration Job updates the existing Smart Proxy instead of adding a
  second one. Hosts keep their Remote Execution proxy.

`make test-backup`, run after `make test`, creates an organization, a product,
a synced repository and a host registered with an activation key, backs the
release up, deletes the `foreman` namespace and restores into
`foreman-restore` with new server certificates from the test CA. It then
checks that Katello's ping is `ok`, that the organization, product, repository
and host exist, that a package downloads through `/pulp/content` with the
entitlement certificate issued before the backup and matches the original
bytes, that the host's consumer certificate still works against `/rhsm`, and
that both Smart Proxies point at the new namespace. Run `make diagnostics
KIND_NAMESPACE=foreman-restore` after a failure.

## Access and cleanup

Forward the front door Service using the test kubeconfig:

```console
KUBECONFIG="$PWD/artifacts/kubeconfig" kubectl --namespace foreman port-forward service/foreman-foreman-httpd 8443:443
```

Open `https://foreman.test:8443` after mapping `foreman.test` to `127.0.0.1`
and trusting the test CA, or use curl:

```console
curl --cacert artifacts/certificates/foreman-foreman/ca.crt \
  --resolve foreman.test:8443:127.0.0.1 https://foreman.test:8443/api/v2/ping
```

The credentials `admin` / `foreman-test` are only suitable for this disposable
environment. PostgreSQL data is ephemeral. Delete the test cluster when finished:

```console
make teardown-kind
```

CI runs the same setup, test, diagnostics, and teardown targets, always removes
its cluster, and uploads the JUnit report. Remove `.venv` and `artifacts` locally
when the test dependencies and reports are no longer needed.
