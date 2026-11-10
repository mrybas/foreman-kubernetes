# Minimal Foreman quick start

This experimental, unsupported milestone installs a single core Foreman web pod
backed by PostgreSQL. It does not enable Katello, Foreman Tasks, Candlepin, Pulp,
Smart Proxy features, high availability, or production ingress.

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

`test` installs disposable PostgreSQL, creates test credentials and test
certificates, and installs the chart. Helm waits for the migration and seed Job
and the ready Foreman Deployment.
Then [pytest-helm-charts](https://github.com/giantswarm/pytest-helm-charts) connects
to that cluster and runs five tests: the web Deployment is available, the API
reports an active database, the seeded administrator can query the users API,
the web pod has the CA bundle from its Secret, and the `foreman` user in the
web pod can read the client key and verify the client certificate against the
CA bundle.
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
