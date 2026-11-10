# Minimal Foreman quick start

This experimental, unsupported milestone installs a single core Foreman web pod
backed by PostgreSQL. Optionally it adds Dynflow with the Foreman Tasks plugin
for background tasks, Pulp, and Candlepin. It does not enable Katello, Smart
Proxy features, high availability, or production ingress.

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

`test` installs disposable PostgreSQL and Valkey, creates test credentials and
test certificates, and installs the chart with Dynflow, Pulp and Candlepin
enabled. Helm waits for the migration Jobs and the ready Deployments.
Then [pytest-helm-charts](https://github.com/giantswarm/pytest-helm-charts) connects
to that cluster and runs the tests in `tests/integration`. For Foreman: the web
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
rejected, and the log contains no credentials.
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

With `certificates.enabled=true` the web pod mounts the CA bundle and the client
certificate read-only under `/etc/foreman/certs`. Foreman uses them, through the
`ssl_ca_file`, `ssl_certificate` and `ssl_priv_key` settings, for HTTPS
connections to Smart Proxies. The migration Job does not mount them.
`certificates.enabled` is `false` by default.

`make test` creates both Secrets with `tests/kind/certificates.sh` and installs
the chart with `certificates.enabled=true`. To create test certificates in
another cluster, pass the namespace, the release name and the client CN:

```console
tests/kind/certificates.sh foreman foreman foreman.example.test
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
accepts no passwords. It is meant to sit behind a front door that sets this
header from a verified client certificate. The chart does not include that
front door yet. A NetworkPolicy lets only pods labelled
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

## Access and cleanup

Forward the Service using the test kubeconfig:

```console
KUBECONFIG="$PWD/artifacts/kubeconfig" kubectl --namespace foreman port-forward service/foreman-foreman 3000:3000
```

Open `http://foreman.test:3000` after mapping `foreman.test` to `127.0.0.1`, or
send the host header explicitly:

```console
curl --header 'Host: foreman.test' http://127.0.0.1:3000/api/v2/ping
```

The credentials `admin` / `foreman-test` are only suitable for this disposable
environment. PostgreSQL data is ephemeral. Delete the test cluster when finished:

```console
make teardown-kind
```

CI runs the same setup, test, diagnostics, and teardown targets, always removes
its cluster, and uploads the JUnit report. Remove `.venv` and `artifacts` locally
when the test dependencies and reports are no longer needed.
