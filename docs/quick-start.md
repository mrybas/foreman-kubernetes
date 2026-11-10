# Minimal Foreman quick start

This experimental, unsupported milestone installs a single core Foreman web pod
backed by PostgreSQL. Optionally it adds Dynflow with the Foreman Tasks plugin
for background tasks, Pulp, Candlepin, an HTTPS front door, the Katello
plugin, and a Smart Proxy for Remote Execution and Ansible. It does not enable
other Smart Proxy features, high availability, or production ingress.

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
to that cluster and runs the 55 tests in `tests/integration`. For Foreman: the web
Deployment is available, the API reports an active database, the seeded
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
PersistentVolumeClaim mounted at `/var/lib/pulp` by all of them.

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

The Pulp API trusts the user name in the `REMOTE-USER` request header and
accepts no passwords. It is meant to sit behind the
[HTTPS front door](#https-front-door), which sets this header from a verified
client certificate. A NetworkPolicy lets only pods labelled
`app.kubernetes.io/component: httpd` of the same release connect to the API.
Anyone who can create pods in the namespace can give a pod these labels. On
clusters whose network plugin ignores NetworkPolicy, any pod that reaches
the API Service can act as any Pulp user.

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
checks that the repository metadata is served only with an entitlement
certificate, publishes a content view, creates an activation key, registers a
consumer with it, and deletes the organization. A self-signed certificate for
that consumer, sent as an `X-RHSM-SSL-CLIENT-CERT` header in any `-`/`_`
spelling by a client without a valid client certificate, must not
authenticate as the consumer. The repository is a copy of the Pulp project's small
`rpm-unsigned` fixture. `tests/kind/rpm-repository.sh` downloads it on the
machine that runs the tests and serves it inside the cluster at
`http://rpm-repository/`, so the cluster needs no outbound access for it.

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
